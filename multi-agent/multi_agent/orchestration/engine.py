from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from multi_agent.domain.errors import (
    ApprovalStateError,
    InvalidOutputSchemaError,
    OrchestrationError,
    ProviderCapabilityError,
    ProviderExecutionError,
)
from multi_agent.domain.json_schema import validate_codex_output_schema
from multi_agent.domain.models import (
    AccessMode,
    ApprovalStatus,
    EventKind,
    ExecutionRequest,
    FailurePolicy,
    ProviderEvent,
    ProviderSessionRef,
    TaskInstanceStatus,
    SessionMode,
    TaskSpec,
    TERMINAL_TASK_INSTANCE_STATUSES,
    WorkflowDefinition,
    WorkflowInstanceStatus,
)
from multi_agent.providers.base import AgentProvider, ExecutionHandle
from multi_agent.providers.registry import ProviderRegistry
from multi_agent.providers.utils import redact_payload
from multi_agent.storage.sqlite import SQLiteStore
from multi_agent.workspaces.manager import WorkspaceManager

_OUTPUT_TOKEN = re.compile(r"\{\{tasks\.([A-Za-z0-9_.-]+)\.output\}\}")


class WorkflowEngine:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        providers: ProviderRegistry,
        workspaces: WorkspaceManager,
        max_concurrency: int = 8,
        provider_concurrency: dict[str, int] | None = None,
    ) -> None:
        self.store = store
        self.providers = providers
        self.workspaces = workspaces
        self._global_semaphore = asyncio.Semaphore(max_concurrency)
        self._provider_limits = provider_concurrency or {}
        self._provider_semaphores: dict[str, asyncio.Semaphore] = {}
        self._session_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._session_lock_users: dict[tuple[str, str], int] = {}
        self._instance_tasks: dict[str, asyncio.Task[None]] = {}
        self._active_handles: dict[tuple[str, str], tuple[AgentProvider, ExecutionHandle]] = {}
        self._approval_waiters: dict[str, asyncio.Future[bool]] = {}
        self._started = False
        self._closing = False

    async def start(self) -> dict[str, int]:
        if self._started:
            return {"instances": 0, "task_instances": 0, "attempts": 0}
        self.store.initialize()
        recovered = self.store.recover_stale()
        self._closing = False
        self._started = True
        return recovered

    async def close(self) -> None:
        if not self._started:
            return
        self._closing = True
        tasks = [task for task in self._instance_tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self.providers.close()
        finally:
            self._instance_tasks.clear()
            self._active_handles.clear()
            self._approval_waiters.clear()
            self._session_locks.clear()
            self._session_lock_users.clear()
            self._started = False

    def _validate_task_capabilities(self, task: TaskSpec) -> None:
        provider = self.providers.get(task.provider)
        capabilities = provider.capabilities()
        if task.session_mode == SessionMode.resume and not capabilities.resume_session:
            raise ProviderCapabilityError(
                f"provider {task.provider!r} cannot resume sessions"
            )
        if task.access == AccessMode.read_only and not capabilities.read_only_mode:
            raise ProviderCapabilityError(
                f"provider {task.provider!r} cannot enforce read-only access"
            )
        if task.access == AccessMode.workspace_write and not capabilities.workspace_write_mode:
            raise ProviderCapabilityError(
                f"provider {task.provider!r} cannot enforce workspace-write access"
            )
        if task.output_schema and not capabilities.structured_output:
            raise ProviderCapabilityError(
                f"provider {task.provider!r} does not support structured output"
            )
        if task.provider == "codex" and task.output_schema is not None:
            try:
                validate_codex_output_schema(task.output_schema)
            except ValueError as exc:
                raise InvalidOutputSchemaError(
                    f"Codex task {task.id!r} has an invalid output_schema: {exc}"
                ) from exc

    def validate_workflow(self, workflow: WorkflowDefinition) -> None:
        for task in workflow.tasks:
            self.workspaces.resolve(task.workspace_id)
            self._validate_task_capabilities(task)

    async def submit(
        self,
        workflow: WorkflowDefinition,
        *,
        template_id: str | None = None,
        template_version: int | None = None,
    ) -> str:
        if not self._started:
            await self.start()
        self.validate_workflow(workflow)
        instance_id = self.store.create_instance(
            workflow,
            template_id=template_id,
            template_version=template_version,
        )
        self.store.append_event(
            instance_id=instance_id,
            event=ProviderEvent(
                kind=EventKind.started,
                summary="Workflow queued",
                payload={
                    "template_id": template_id,
                    "template_version": template_version,
                    "source": "template" if template_id else "ad_hoc",
                },
                raw_event_type="orchestrator.instance_queued",
            ),
        )
        task = asyncio.create_task(self._execute_instance(instance_id), name=f"multi-agent-instance-{instance_id}")
        self._instance_tasks[instance_id] = task
        return instance_id

    async def wait(self, instance_id: str) -> dict[str, Any]:
        task = self._instance_tasks.get(instance_id)
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
        return self.store.get_instance(instance_id)

    async def cancel_instance(self, instance_id: str) -> dict[str, Any]:
        instance = self.store.get_instance(instance_id)
        if instance["status"] in {
            WorkflowInstanceStatus.succeeded.value,
            WorkflowInstanceStatus.failed.value,
            WorkflowInstanceStatus.cancelled.value,
            WorkflowInstanceStatus.interrupted.value,
        }:
            return instance
        task = self._instance_tasks.get(instance_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return self.store.get_instance(instance_id)

    async def resolve_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        decided_by: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        waiter = self._approval_waiters.get(approval_id)
        if waiter is None:
            approval = self.store.get_approval(approval_id)
            raise ApprovalStateError(
                f"approval {approval_id} has no active execution handle; "
                f"current status is {approval['status']}"
            )
        status = ApprovalStatus.approved if approved else ApprovalStatus.rejected
        result = self.store.resolve_approval(
            approval_id,
            status,
            decided_by=decided_by,
            reason=reason,
        )
        if not waiter.done():
            waiter.set_result(approved)
        return result

    async def _execute_instance(self, instance_id: str) -> None:
        workflow = self.store.get_instance_definition(instance_id)
        active: dict[str, asyncio.Task[None]] = {}
        instance_semaphore = asyncio.Semaphore(workflow.max_concurrency)
        try:
            self.store.set_instance_status(instance_id, WorkflowInstanceStatus.running)
            while True:
                rows = {
                    row["task_id"]: row
                    for row in self.store.list_task_instances(instance_id)
                }
                failed_exists = any(
                    row["status"] == TaskInstanceStatus.failed.value
                    for row in rows.values()
                )

                for task_spec in workflow.tasks:
                    row = rows[task_spec.id]
                    if row["status"] != TaskInstanceStatus.pending.value:
                        continue
                    dependency_statuses = [rows[item]["status"] for item in task_spec.depends_on]
                    if any(
                        status in {
                            TaskInstanceStatus.failed.value,
                            TaskInstanceStatus.cancelled.value,
                            TaskInstanceStatus.interrupted.value,
                            TaskInstanceStatus.blocked.value,
                        }
                        for status in dependency_statuses
                    ):
                        self.store.set_task_status(
                            instance_id,
                            task_spec.id,
                            TaskInstanceStatus.blocked,
                            error_code="dependency_failed",
                            error_message="one or more dependencies did not succeed",
                        )
                    elif all(
                        status == TaskInstanceStatus.succeeded.value
                        for status in dependency_statuses
                    ):
                        if workflow.failure_policy == FailurePolicy.fail_fast and failed_exists:
                            self.store.set_task_status(
                                instance_id,
                                task_spec.id,
                                TaskInstanceStatus.blocked,
                                error_code="fail_fast",
                                error_message="workflow stopped scheduling after a failure",
                            )
                        else:
                            self.store.set_task_status(
                                instance_id,
                                task_spec.id,
                                TaskInstanceStatus.ready,
                            )

                rows = {
                    row["task_id"]: row
                    for row in self.store.list_task_instances(instance_id)
                }
                for task_spec in workflow.tasks:
                    if rows[task_spec.id]["status"] != TaskInstanceStatus.ready.value:
                        continue
                    if task_spec.id in active:
                        continue
                    await instance_semaphore.acquire()
                    child = asyncio.create_task(
                        self._execute_task(instance_id, task_spec),
                        name=f"multi-agent-task-{instance_id}-{task_spec.id}",
                    )
                    child.add_done_callback(lambda _task, sem=instance_semaphore: sem.release())
                    active[task_spec.id] = child

                done_ids = [task_id for task_id, task in active.items() if task.done()]
                for task_id in done_ids:
                    task = active.pop(task_id)
                    try:
                        task.result()
                    except asyncio.CancelledError:
                        raise
                    except BaseException:
                        # _execute_task records stable failure details. This only consumes
                        # an unexpected exception so it cannot become an unobserved task.
                        pass

                rows = self.store.list_task_instances(instance_id)
                if all(
                    TaskInstanceStatus(row["status"])
                    in TERMINAL_TASK_INSTANCE_STATUSES
                    for row in rows
                ):
                    break
                if active:
                    await asyncio.wait(active.values(), return_when=asyncio.FIRST_COMPLETED)
                else:
                    await asyncio.sleep(0)

            rows = self.store.list_task_instances(instance_id)
            statuses = {TaskInstanceStatus(row["status"]) for row in rows}
            if (
                TaskInstanceStatus.failed in statuses
                or TaskInstanceStatus.blocked in statuses
            ):
                final_status = WorkflowInstanceStatus.failed
            elif TaskInstanceStatus.interrupted in statuses:
                final_status = WorkflowInstanceStatus.interrupted
            elif TaskInstanceStatus.cancelled in statuses:
                final_status = WorkflowInstanceStatus.cancelled
            else:
                final_status = WorkflowInstanceStatus.succeeded
            self.store.set_instance_status(instance_id, final_status)
            self.store.append_event(
                instance_id=instance_id,
                event=ProviderEvent(
                    kind=(
                        EventKind.completed
                        if final_status == WorkflowInstanceStatus.succeeded
                        else EventKind.failed
                    ),
                    summary=f"Workflow {final_status.value}",
                    payload={"status": final_status.value},
                    raw_event_type="orchestrator.instance_finished",
                ),
            )
        except asyncio.CancelledError:
            children = list(active.values())
            for child in children:
                child.cancel()
            if children:
                await asyncio.gather(*children, return_exceptions=True)
            terminal_status = (
                TaskInstanceStatus.interrupted
                if self._closing
                else TaskInstanceStatus.cancelled
            )
            instance_status = (
                WorkflowInstanceStatus.interrupted
                if self._closing
                else WorkflowInstanceStatus.cancelled
            )
            for row in self.store.list_task_instances(instance_id):
                if (
                    TaskInstanceStatus(row["status"])
                    not in TERMINAL_TASK_INSTANCE_STATUSES
                ):
                    self.store.set_task_status(instance_id, row["task_id"], terminal_status)
            self.store.set_instance_status(instance_id, instance_status)
            self.store.append_event(
                instance_id=instance_id,
                event=ProviderEvent(
                    kind=EventKind.cancelled,
                    summary=f"Workflow {instance_status.value}",
                    payload={"status": instance_status.value},
                    raw_event_type="orchestrator.instance_cancelled",
                ),
            )
            raise
        finally:
            self._instance_tasks.pop(instance_id, None)

    def _provider_semaphore(self, provider_name: str) -> asyncio.Semaphore:
        if provider_name not in self._provider_semaphores:
            limit = self._provider_limits.get(provider_name, 4)
            self._provider_semaphores[provider_name] = asyncio.Semaphore(limit)
        return self._provider_semaphores[provider_name]

    @asynccontextmanager
    async def _session_guard(
        self,
        provider: str,
        session_id: str | None,
    ) -> AsyncIterator[None]:
        if session_id is None:
            yield
            return
        key = (provider, session_id)
        lock = self._session_locks.setdefault(key, asyncio.Lock())
        self._session_lock_users[key] = self._session_lock_users.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            remaining = self._session_lock_users[key] - 1
            if remaining == 0:
                self._session_lock_users.pop(key, None)
                self._session_locks.pop(key, None)
            else:
                self._session_lock_users[key] = remaining

    @staticmethod
    async def _cancel_provider_handle(
        provider: AgentProvider,
        handle: ExecutionHandle | None,
    ) -> None:
        if handle is None or not provider.capabilities().cancel_running_turn:
            return
        try:
            async with asyncio.timeout(5):
                await provider.cancel(handle)
        except BaseException:
            pass

    def _render_prompt(self, instance_id: str, task: TaskSpec) -> str:
        dependencies: dict[str, str] = {}
        for dependency_id in task.depends_on:
            output = (
                self.store.get_task_instance(instance_id, dependency_id)["final_output"]
                or ""
            )
            dependencies[dependency_id] = output
        prompt = task.prompt_template.replace(
            "{{dependencies}}",
            "\n\n".join(
                f"[{task_id}]\n{output}" for task_id, output in dependencies.items()
            ),
        )

        def replace(match: re.Match[str]) -> str:
            task_id = match.group(1)
            if task_id not in dependencies:
                raise ProviderExecutionError(
                    f"prompt references non-dependency task output: {task_id}",
                    code="invalid_prompt_template",
                )
            return dependencies[task_id]

        return _OUTPUT_TOKEN.sub(replace, prompt)

    async def _execute_task(self, instance_id: str, task: TaskSpec) -> None:
        provider = self.providers.get(task.provider)
        retry_policy = task.retry_policy
        async with self._global_semaphore:
            async with self._provider_semaphore(task.provider):
                async with self.workspaces.access(task.workspace_id, task.access) as workspace:
                    session_id = (
                        task.provider_session_id
                        if task.session_mode == SessionMode.resume
                        else None
                    )
                    async with self._session_guard(task.provider, session_id):
                        for attempt_index in range(1, retry_policy.max_attempts + 1):
                            attempt_id, _attempt_number = self.store.start_attempt(instance_id, task.id)
                            self.store.set_task_status(
                                instance_id,
                                task.id,
                                TaskInstanceStatus.running,
                            )
                            self.store.append_event(
                                instance_id=instance_id,
                                task_instance_id=self.store.task_instance_id(
                                    instance_id, task.id
                                ),
                                attempt_id=attempt_id,
                                provider=task.provider,
                                event=ProviderEvent(
                                    kind=EventKind.started,
                                    summary=f"Task {task.id} started",
                                    payload={"attempt": attempt_index},
                                    raw_event_type="orchestrator.task_started",
                                ),
                            )
                            handle: ExecutionHandle | None = None
                            try:
                                provider = await self.providers.ensure_started(task.provider)
                                request = ExecutionRequest(
                                    workflow_instance_id=instance_id,
                                    task_instance_id=self.store.task_instance_id(
                                        instance_id, task.id
                                    ),
                                    task_id=task.id,
                                    prompt=self._render_prompt(instance_id, task),
                                    role=task.role,
                                    workspace=workspace,
                                    access=task.access,
                                    output_schema=task.output_schema,
                                    provider_options=task.provider_options,
                                )
                                session = (
                                    ProviderSessionRef(
                                        provider=task.provider,
                                        session_id=session_id,
                                    )
                                    if session_id
                                    else None
                                )
                                async with asyncio.timeout(task.timeout_seconds):
                                    handle = await provider.start_execution(request, session)
                                    self._active_handles[(instance_id, task.id)] = (provider, handle)
                                    self.store.set_task_session(
                                        instance_id, task.id, handle.session.session_id
                                    )
                                    self.store.set_attempt_session(
                                        attempt_id, handle.session.session_id
                                    )
                                    output = await self._consume_events(
                                        instance_id=instance_id,
                                        task=task,
                                        attempt_id=attempt_id,
                                        provider=provider,
                                        handle=handle,
                                    )
                                self.store.set_task_output(instance_id, task.id, output)
                                self.store.set_task_status(
                                    instance_id, task.id, TaskInstanceStatus.succeeded
                                )
                                self.store.finish_attempt(attempt_id, "succeeded")
                                return
                            except asyncio.CancelledError:
                                await self._cancel_provider_handle(provider, handle)
                                status = (
                                    TaskInstanceStatus.interrupted
                                    if self._closing
                                    else TaskInstanceStatus.cancelled
                                )
                                self.store.set_task_status(instance_id, task.id, status)
                                self.store.finish_attempt(attempt_id, status.value)
                                self.store.append_event(
                                    instance_id=instance_id,
                                    task_instance_id=self.store.task_instance_id(
                                        instance_id, task.id
                                    ),
                                    attempt_id=attempt_id,
                                    provider=task.provider,
                                    event=ProviderEvent(
                                        kind=EventKind.cancelled,
                                        summary=f"Task {task.id} {status.value}",
                                        payload={"status": status.value},
                                        raw_event_type="orchestrator.task_cancelled",
                                    ),
                                )
                                raise
                            except TimeoutError:
                                await self._cancel_provider_handle(provider, handle)
                                error = ProviderExecutionError(
                                    f"task exceeded {task.timeout_seconds} seconds",
                                    code="timeout",
                                    retryable=True,
                                )
                            except ProviderExecutionError as exc:
                                error = exc
                            except OrchestrationError as exc:
                                error = ProviderExecutionError(
                                    str(exc), code=getattr(exc, "code", "orchestration_error")
                                )
                            except BaseException as exc:
                                error = ProviderExecutionError(str(exc))
                            finally:
                                self._active_handles.pop((instance_id, task.id), None)

                            can_retry = (
                                attempt_index < retry_policy.max_attempts
                                and error.retryable
                                and (
                                    task.access == AccessMode.read_only
                                    or retry_policy.idempotent
                                )
                            )
                            self.store.finish_attempt(
                                attempt_id,
                                "failed",
                                error_code=error.code,
                                error_message=str(error),
                            )
                            self.store.append_event(
                                instance_id=instance_id,
                                task_instance_id=self.store.task_instance_id(
                                    instance_id, task.id
                                ),
                                attempt_id=attempt_id,
                                provider=task.provider,
                                event=ProviderEvent(
                                    kind=EventKind.failed,
                                    summary=str(error),
                                    payload={
                                        "code": error.code,
                                        "retryable": error.retryable,
                                        "will_retry": can_retry,
                                    },
                                    raw_event_type="orchestrator.attempt_failed",
                                ),
                            )
                            if not can_retry:
                                self.store.set_task_status(
                                    instance_id,
                                    task.id,
                                    TaskInstanceStatus.failed,
                                    error_code=error.code,
                                    error_message=str(error),
                                )
                                return

    async def _consume_events(
        self,
        *,
        instance_id: str,
        task: TaskSpec,
        attempt_id: str,
        provider: AgentProvider,
        handle: ExecutionHandle,
    ) -> str | None:
        output: str | None = None
        terminal = False
        async for raw_event in provider.stream(handle):
            event = raw_event.model_copy(update={"payload": redact_payload(raw_event.payload)})
            if event.kind == EventKind.approval_required:
                if not provider.capabilities().approval_callback:
                    raise ProviderCapabilityError(
                        f"provider {provider.name!r} emitted an approval without callback support"
                    )
                request_id = str(event.payload.get("request_id", ""))
                if not request_id:
                    raise ProviderExecutionError(
                        "approval event is missing request_id",
                        code="invalid_provider_event",
                    )
                approval = self.store.create_approval(
                    instance_id=instance_id,
                    task_id=task.id,
                    attempt_id=attempt_id,
                    provider=task.provider,
                    provider_request_id=request_id,
                    request=event.payload,
                )
                event = event.model_copy(
                    update={"payload": {**event.payload, "approval_id": approval["id"]}}
                )
                self.store.append_event(
                    instance_id=instance_id,
                    task_instance_id=self.store.task_instance_id(instance_id, task.id),
                    attempt_id=attempt_id,
                    provider=task.provider,
                    event=event,
                )
                self.store.set_task_status(
                    instance_id, task.id, TaskInstanceStatus.awaiting_approval
                )
                waiter = asyncio.get_running_loop().create_future()
                self._approval_waiters[approval["id"]] = waiter
                try:
                    approved = await waiter
                    await provider.resolve_approval(handle, request_id, approved)
                finally:
                    self._approval_waiters.pop(approval["id"], None)
                self.store.set_task_status(
                    instance_id,
                    task.id,
                    TaskInstanceStatus.running,
                )
                continue

            self.store.append_event(
                instance_id=instance_id,
                task_instance_id=self.store.task_instance_id(instance_id, task.id),
                attempt_id=attempt_id,
                provider=task.provider,
                event=event,
            )
            if event.kind == EventKind.message_completed:
                candidate = event.payload.get("content") or event.summary
                if candidate:
                    output = self._serialize_output(candidate)
            elif event.kind == EventKind.completed:
                terminal = True
                candidate = event.payload.get("final_output")
                if candidate is not None:
                    output = self._serialize_output(candidate)
            elif event.kind == EventKind.failed:
                terminal = True
                raise ProviderExecutionError(
                    event.summary or "provider reported failure",
                    code=str(event.payload.get("code", "provider_reported_failure")),
                    retryable=bool(event.payload.get("retryable", False)),
                )
        if not terminal:
            raise ProviderExecutionError(
                "provider stream ended without a terminal event",
                code="incomplete_provider_stream",
            )
        return output

    @staticmethod
    def _serialize_output(value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

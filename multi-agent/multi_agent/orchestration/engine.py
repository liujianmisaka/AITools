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
    RunStatus,
    SessionMode,
    TaskSpec,
    TaskStatus,
    TERMINAL_TASK_STATUSES,
    WorkflowDefinition,
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
        self._run_tasks: dict[str, asyncio.Task[None]] = {}
        self._active_handles: dict[tuple[str, str], tuple[AgentProvider, ExecutionHandle]] = {}
        self._approval_waiters: dict[str, asyncio.Future[bool]] = {}
        self._started = False
        self._closing = False

    async def start(self) -> dict[str, int]:
        if self._started:
            return {"runs": 0, "tasks": 0, "attempts": 0}
        self.store.initialize()
        recovered = self.store.recover_stale()
        self._closing = False
        self._started = True
        return recovered

    async def close(self) -> None:
        if not self._started:
            return
        self._closing = True
        tasks = [task for task in self._run_tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self.providers.close()
        finally:
            self._run_tasks.clear()
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

    async def submit(self, workflow: WorkflowDefinition) -> str:
        if not self._started:
            await self.start()
        self.validate_workflow(workflow)
        run_id = self.store.create_run(workflow)
        self.store.append_event(
            run_id=run_id,
            event=ProviderEvent(
                kind=EventKind.started,
                summary="Workflow queued",
                payload={"workflow_id": workflow.id, "version": workflow.version},
                raw_event_type="orchestrator.run_queued",
            ),
        )
        task = asyncio.create_task(self._execute_run(run_id), name=f"multi-agent-run-{run_id}")
        self._run_tasks[run_id] = task
        return run_id

    async def wait(self, run_id: str) -> dict[str, Any]:
        task = self._run_tasks.get(run_id)
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
        return self.store.get_run(run_id)

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run["status"] in {
            RunStatus.succeeded.value,
            RunStatus.failed.value,
            RunStatus.cancelled.value,
            RunStatus.interrupted.value,
        }:
            return run
        task = self._run_tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return self.store.get_run(run_id)

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

    async def _execute_run(self, run_id: str) -> None:
        workflow = self.store.get_workflow(run_id)
        active: dict[str, asyncio.Task[None]] = {}
        run_semaphore = asyncio.Semaphore(workflow.max_concurrency)
        try:
            self.store.set_run_status(run_id, RunStatus.running)
            while True:
                rows = {row["task_id"]: row for row in self.store.list_task_runs(run_id)}
                failed_exists = any(row["status"] == TaskStatus.failed.value for row in rows.values())

                for task_spec in workflow.tasks:
                    row = rows[task_spec.id]
                    if row["status"] != TaskStatus.pending.value:
                        continue
                    dependency_statuses = [rows[item]["status"] for item in task_spec.depends_on]
                    if any(
                        status in {
                            TaskStatus.failed.value,
                            TaskStatus.cancelled.value,
                            TaskStatus.interrupted.value,
                            TaskStatus.blocked.value,
                        }
                        for status in dependency_statuses
                    ):
                        self.store.set_task_status(
                            run_id,
                            task_spec.id,
                            TaskStatus.blocked,
                            error_code="dependency_failed",
                            error_message="one or more dependencies did not succeed",
                        )
                    elif all(status == TaskStatus.succeeded.value for status in dependency_statuses):
                        if workflow.failure_policy == FailurePolicy.fail_fast and failed_exists:
                            self.store.set_task_status(
                                run_id,
                                task_spec.id,
                                TaskStatus.blocked,
                                error_code="fail_fast",
                                error_message="workflow stopped scheduling after a failure",
                            )
                        else:
                            self.store.set_task_status(run_id, task_spec.id, TaskStatus.ready)

                rows = {row["task_id"]: row for row in self.store.list_task_runs(run_id)}
                for task_spec in workflow.tasks:
                    if rows[task_spec.id]["status"] != TaskStatus.ready.value:
                        continue
                    if task_spec.id in active:
                        continue
                    await run_semaphore.acquire()
                    child = asyncio.create_task(
                        self._execute_task(run_id, task_spec),
                        name=f"multi-agent-task-{run_id}-{task_spec.id}",
                    )
                    child.add_done_callback(lambda _task, sem=run_semaphore: sem.release())
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

                rows = self.store.list_task_runs(run_id)
                if all(TaskStatus(row["status"]) in TERMINAL_TASK_STATUSES for row in rows):
                    break
                if active:
                    await asyncio.wait(active.values(), return_when=asyncio.FIRST_COMPLETED)
                else:
                    await asyncio.sleep(0)

            rows = self.store.list_task_runs(run_id)
            statuses = {TaskStatus(row["status"]) for row in rows}
            if TaskStatus.failed in statuses or TaskStatus.blocked in statuses:
                final_status = RunStatus.failed
            elif TaskStatus.interrupted in statuses:
                final_status = RunStatus.interrupted
            elif TaskStatus.cancelled in statuses:
                final_status = RunStatus.cancelled
            else:
                final_status = RunStatus.succeeded
            self.store.set_run_status(run_id, final_status)
            self.store.append_event(
                run_id=run_id,
                event=ProviderEvent(
                    kind=EventKind.completed if final_status == RunStatus.succeeded else EventKind.failed,
                    summary=f"Workflow {final_status.value}",
                    payload={"status": final_status.value},
                    raw_event_type="orchestrator.run_finished",
                ),
            )
        except asyncio.CancelledError:
            children = list(active.values())
            for child in children:
                child.cancel()
            if children:
                await asyncio.gather(*children, return_exceptions=True)
            terminal_status = TaskStatus.interrupted if self._closing else TaskStatus.cancelled
            run_status = RunStatus.interrupted if self._closing else RunStatus.cancelled
            for row in self.store.list_task_runs(run_id):
                if TaskStatus(row["status"]) not in TERMINAL_TASK_STATUSES:
                    self.store.set_task_status(run_id, row["task_id"], terminal_status)
            self.store.set_run_status(run_id, run_status)
            self.store.append_event(
                run_id=run_id,
                event=ProviderEvent(
                    kind=EventKind.cancelled,
                    summary=f"Workflow {run_status.value}",
                    payload={"status": run_status.value},
                    raw_event_type="orchestrator.run_cancelled",
                ),
            )
            raise
        finally:
            self._run_tasks.pop(run_id, None)

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

    def _render_prompt(self, run_id: str, task: TaskSpec) -> str:
        dependencies: dict[str, str] = {}
        for dependency_id in task.depends_on:
            output = self.store.get_task_run(run_id, dependency_id)["final_output"] or ""
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

    async def _execute_task(self, run_id: str, task: TaskSpec) -> None:
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
                            attempt_id, _attempt_number = self.store.start_attempt(run_id, task.id)
                            self.store.set_task_status(run_id, task.id, TaskStatus.running)
                            self.store.append_event(
                                run_id=run_id,
                                task_run_id=self.store.task_run_id(run_id, task.id),
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
                                    run_id=run_id,
                                    task_run_id=self.store.task_run_id(run_id, task.id),
                                    task_id=task.id,
                                    prompt=self._render_prompt(run_id, task),
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
                                    self._active_handles[(run_id, task.id)] = (provider, handle)
                                    self.store.set_task_session(
                                        run_id, task.id, handle.session.session_id
                                    )
                                    self.store.set_attempt_session(
                                        attempt_id, handle.session.session_id
                                    )
                                    output = await self._consume_events(
                                        run_id=run_id,
                                        task=task,
                                        attempt_id=attempt_id,
                                        provider=provider,
                                        handle=handle,
                                    )
                                self.store.set_task_output(run_id, task.id, output)
                                self.store.set_task_status(
                                    run_id, task.id, TaskStatus.succeeded
                                )
                                self.store.finish_attempt(attempt_id, "succeeded")
                                return
                            except asyncio.CancelledError:
                                await self._cancel_provider_handle(provider, handle)
                                status = (
                                    TaskStatus.interrupted
                                    if self._closing
                                    else TaskStatus.cancelled
                                )
                                self.store.set_task_status(run_id, task.id, status)
                                self.store.finish_attempt(attempt_id, status.value)
                                self.store.append_event(
                                    run_id=run_id,
                                    task_run_id=self.store.task_run_id(run_id, task.id),
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
                                self._active_handles.pop((run_id, task.id), None)

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
                                run_id=run_id,
                                task_run_id=self.store.task_run_id(run_id, task.id),
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
                                    run_id,
                                    task.id,
                                    TaskStatus.failed,
                                    error_code=error.code,
                                    error_message=str(error),
                                )
                                return

    async def _consume_events(
        self,
        *,
        run_id: str,
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
                    run_id=run_id,
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
                    run_id=run_id,
                    task_run_id=self.store.task_run_id(run_id, task.id),
                    attempt_id=attempt_id,
                    provider=task.provider,
                    event=event,
                )
                self.store.set_task_status(
                    run_id, task.id, TaskStatus.awaiting_approval
                )
                waiter = asyncio.get_running_loop().create_future()
                self._approval_waiters[approval["id"]] = waiter
                try:
                    approved = await waiter
                    await provider.resolve_approval(handle, request_id, approved)
                finally:
                    self._approval_waiters.pop(approval["id"], None)
                self.store.set_task_status(run_id, task.id, TaskStatus.running)
                continue

            self.store.append_event(
                run_id=run_id,
                task_run_id=self.store.task_run_id(run_id, task.id),
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

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
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
    ProviderEvent,
    ProviderSessionRef,
    SessionMode,
    TaskInstanceStatus,
    TaskSpec,
)
from multi_agent.providers.base import AgentProvider, ExecutionHandle
from multi_agent.providers.registry import ProviderRegistry
from multi_agent.providers.utils import redact_payload
from multi_agent.storage.sqlite import SQLiteStore
from multi_agent.workspaces.manager import WorkspaceManager


_OUTPUT_TOKEN = re.compile(r"\{\{tasks\.([A-Za-z0-9_.-]+)\.output\}\}")
_INPUT_TOKEN = re.compile(r"\{\{input(?:\.([A-Za-z0-9_.-]+))?\}\}")


class AgentWorkExecutor:
    """Runs one agent work item without owning orchestration decisions."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        providers: ProviderRegistry,
        workspaces: WorkspaceManager,
        max_concurrency: int = 8,
        provider_concurrency: dict[str, int] | None = None,
        approval_hook: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.store = store
        self._approval_hook = approval_hook
        self.providers = providers
        self.workspaces = workspaces
        self._global_semaphore = asyncio.Semaphore(max_concurrency)
        self._provider_limits = provider_concurrency or {}
        self._provider_semaphores: dict[str, asyncio.Semaphore] = {}
        self._session_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._session_lock_users: dict[tuple[str, str], int] = {}
        self._active_handles: dict[
            tuple[str, str], tuple[AgentProvider, ExecutionHandle]
        ] = {}
        self._approval_waiters: dict[str, asyncio.Future[bool]] = {}
        self._closing = False

    @property
    def closing(self) -> bool:
        return self._closing

    def start(self) -> None:
        self._closing = False

    def begin_shutdown(self) -> None:
        self._closing = True

    def set_approval_hook(
        self,
        hook: Callable[[str], Awaitable[None]] | None,
    ) -> None:
        self._approval_hook = hook

    async def close(self) -> None:
        self.begin_shutdown()
        self._active_handles.clear()
        self._approval_waiters.clear()
        self._session_locks.clear()
        self._session_lock_users.clear()

    def validate_task(self, task: TaskSpec) -> None:
        self.workspaces.resolve(task.workspace_id)
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
        if (
            task.access == AccessMode.workspace_write
            and not capabilities.workspace_write_mode
        ):
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
        await self._notify_approval_changed(approval_id)
        return result

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
                self.store.get_work_item(instance_id, dependency_id)["final_output"]
                or ""
            )
            dependencies[dependency_id] = output
        prompt = task.prompt_template.replace(
            "{{dependencies}}",
            "\n\n".join(
                f"[{logical_key}]\n{output}"
                for logical_key, output in dependencies.items()
            ),
        )

        def replace_output(match: re.Match[str]) -> str:
            logical_key = match.group(1)
            if logical_key not in dependencies:
                raise ProviderExecutionError(
                    f"prompt references non-dependency task output: {logical_key}",
                    code="invalid_prompt_template",
                )
            return dependencies[logical_key]

        prompt = _OUTPUT_TOKEN.sub(replace_output, prompt)
        input_data = self.store.get_instance(instance_id)["input"]

        def replace_input(match: re.Match[str]) -> str:
            path = match.group(1)
            value: Any = input_data
            if path:
                for part in path.split("."):
                    if not isinstance(value, dict) or part not in value:
                        raise ProviderExecutionError(
                            f"prompt references missing instance input: {path}",
                            code="invalid_prompt_template",
                        )
                    value = value[part]
            return self._serialize_output(value)

        return _INPUT_TOKEN.sub(replace_input, prompt)

    async def execute(self, instance_id: str, task: TaskSpec) -> None:
        provider = self.providers.get(task.provider)
        retry_policy = task.retry_policy
        async with self._global_semaphore:
            async with self._provider_semaphore(task.provider):
                async with self.workspaces.access(
                    task.workspace_id, task.access
                ) as workspace:
                    session_id = (
                        task.provider_session_id
                        if task.session_mode == SessionMode.resume
                        else None
                    )
                    async with self._session_guard(task.provider, session_id):
                        for attempt_index in range(
                            1, retry_policy.max_attempts + 1
                        ):
                            attempt_id, _attempt_number = self.store.start_attempt(
                                instance_id, task.id
                            )
                            self.store.set_work_item_status(
                                instance_id,
                                task.id,
                                TaskInstanceStatus.running,
                            )
                            work_item_id = self.store.work_item_id(
                                instance_id, task.id
                            )
                            self.store.append_event(
                                instance_id=instance_id,
                                work_item_id=work_item_id,
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
                                provider = await self.providers.ensure_started(
                                    task.provider
                                )
                                request = ExecutionRequest(
                                    workflow_instance_id=instance_id,
                                    work_item_id=work_item_id,
                                    logical_key=task.id,
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
                                    handle = await provider.start_execution(
                                        request, session
                                    )
                                    self._active_handles[(instance_id, task.id)] = (
                                        provider,
                                        handle,
                                    )
                                    self.store.set_work_item_session(
                                        instance_id,
                                        task.id,
                                        handle.session.session_id,
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
                                self.store.set_work_item_output(
                                    instance_id, task.id, output
                                )
                                self.store.set_work_item_status(
                                    instance_id,
                                    task.id,
                                    TaskInstanceStatus.succeeded,
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
                                self.store.set_work_item_status(
                                    instance_id, task.id, status
                                )
                                self.store.finish_attempt(attempt_id, status.value)
                                self.store.append_event(
                                    instance_id=instance_id,
                                    work_item_id=work_item_id,
                                    attempt_id=attempt_id,
                                    provider=task.provider,
                                    event=ProviderEvent(
                                        kind=EventKind.cancelled,
                                        summary=f"Task {task.id} {status.value}",
                                        payload={"status": status.value},
                                        raw_event_type=(
                                            "orchestrator.task_cancelled"
                                        ),
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
                                    str(exc),
                                    code=getattr(
                                        exc, "code", "orchestration_error"
                                    ),
                                )
                            except BaseException as exc:
                                error = ProviderExecutionError(str(exc))
                            finally:
                                self._active_handles.pop(
                                    (instance_id, task.id), None
                                )

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
                                work_item_id=work_item_id,
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
                                    raw_event_type=(
                                        "orchestrator.attempt_failed"
                                    ),
                                ),
                            )
                            if not can_retry:
                                self.store.set_work_item_status(
                                    instance_id,
                                    task.id,
                                    TaskInstanceStatus.failed,
                                    error_code=error.code,
                                    error_message=str(error),
                                )
                                return

    async def _notify_approval_changed(self, approval_id: str) -> None:
        hook = self._approval_hook
        if hook is None:
            return
        try:
            await hook(approval_id)
        except Exception:
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
        work_item_id = self.store.work_item_id(instance_id, task.id)
        async for raw_event in provider.stream(handle):
            event = raw_event.model_copy(
                update={"payload": redact_payload(raw_event.payload)}
            )
            if event.kind == EventKind.approval_required:
                if not provider.capabilities().approval_callback:
                    raise ProviderCapabilityError(
                        f"provider {provider.name!r} emitted an approval "
                        "without callback support"
                    )
                request_id = str(event.payload.get("request_id", ""))
                if not request_id:
                    raise ProviderExecutionError(
                        "approval event is missing request_id",
                        code="invalid_provider_event",
                    )
                approval = self.store.create_approval(
                    instance_id=instance_id,
                    logical_key=task.id,
                    attempt_id=attempt_id,
                    provider=task.provider,
                    provider_request_id=request_id,
                    request=event.payload,
                )
                await self._notify_approval_changed(approval["id"])
                event = event.model_copy(
                    update={
                        "payload": {
                            **event.payload,
                            "approval_id": approval["id"],
                        }
                    }
                )
                self.store.append_event(
                    instance_id=instance_id,
                    work_item_id=work_item_id,
                    attempt_id=attempt_id,
                    provider=task.provider,
                    event=event,
                )
                self.store.set_work_item_status(
                    instance_id,
                    task.id,
                    TaskInstanceStatus.awaiting_approval,
                )
                waiter = asyncio.get_running_loop().create_future()
                self._approval_waiters[approval["id"]] = waiter
                try:
                    approved = await waiter
                    await provider.resolve_approval(handle, request_id, approved)
                finally:
                    self._approval_waiters.pop(approval["id"], None)
                self.store.set_work_item_status(
                    instance_id,
                    task.id,
                    TaskInstanceStatus.running,
                )
                continue

            self.store.append_event(
                instance_id=instance_id,
                work_item_id=work_item_id,
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
                    code=str(
                        event.payload.get(
                            "code", "provider_reported_failure"
                        )
                    ),
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

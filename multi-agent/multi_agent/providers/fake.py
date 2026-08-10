from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import AsyncIterator
from uuid import uuid4

from multi_agent.domain.errors import ProviderExecutionError
from multi_agent.domain.models import (
    EventKind,
    ExecutionRequest,
    ProviderCapabilities,
    ProviderEvent,
    ProviderSessionRef,
)
from multi_agent.providers.base import AgentProvider, ExecutionHandle


@dataclass(slots=True)
class _FakeExecution:
    request: ExecutionRequest
    approval_event: asyncio.Event = field(default_factory=asyncio.Event)
    approval_result: bool | None = None
    cancelled: bool = False


class FakeProvider(AgentProvider):
    """Deterministic provider used by all automated tests."""

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.started = False
        self.closed = False
        self.cancel_count = 0
        self.start_calls: list[ExecutionRequest] = []
        self.active = 0
        self.max_active = 0
        self.active_by_workspace: dict[str, int] = {}
        self.max_active_by_workspace: dict[str, int] = {}
        self._failures: dict[str, int] = {}

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            resume_session=True,
            stream_events=True,
            steer_running_turn=True,
            cancel_running_turn=True,
            structured_output=True,
            approval_callback=True,
            read_only_mode=True,
            workspace_write_mode=True,
        )

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def start_execution(
        self,
        request: ExecutionRequest,
        session: ProviderSessionRef | None = None,
    ) -> ExecutionHandle:
        self.start_calls.append(request)
        session_ref = session or ProviderSessionRef(
            provider=self.name,
            session_id=f"fake-{uuid4().hex}",
        )
        return ExecutionHandle(session=session_ref, native=_FakeExecution(request=request))

    async def stream(self, handle: ExecutionHandle) -> AsyncIterator[ProviderEvent]:
        native: _FakeExecution = handle.native
        request = native.request
        workspace = str(request.workspace)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.active_by_workspace[workspace] = self.active_by_workspace.get(workspace, 0) + 1
        self.max_active_by_workspace[workspace] = max(
            self.max_active_by_workspace.get(workspace, 0),
            self.active_by_workspace[workspace],
        )
        try:
            chunks = request.provider_options.get("chunks", [])
            for chunk in chunks:
                if native.cancelled:
                    raise asyncio.CancelledError
                yield ProviderEvent(
                    kind=EventKind.message_delta,
                    summary=str(chunk),
                    payload={"delta": str(chunk)},
                    raw_event_type="fake.message_delta",
                )

            if request.provider_options.get("approval_required"):
                request_id = f"fake-approval-{uuid4().hex}"
                yield ProviderEvent(
                    kind=EventKind.approval_required,
                    summary="Fake tool requires approval",
                    payload={
                        "request_id": request_id,
                        "tool": request.provider_options.get("approval_tool", "write_file"),
                    },
                    raw_event_type="fake.approval_required",
                )
                await native.approval_event.wait()
                if native.approval_result is not True:
                    raise ProviderExecutionError(
                        "fake approval was rejected",
                        code="permission_denied",
                    )

            delay = float(request.provider_options.get("delay", 0.0))
            if delay:
                await asyncio.sleep(delay)
            if native.cancelled:
                raise asyncio.CancelledError

            failures_before_success = int(
                request.provider_options.get("failures_before_success", 0)
            )
            failures = self._failures.get(request.task_run_id, 0)
            if failures < failures_before_success:
                self._failures[request.task_run_id] = failures + 1
                raise ProviderExecutionError(
                    "planned fake transient failure",
                    code="fake_transient",
                    retryable=True,
                )

            output = str(request.provider_options.get("output", request.prompt))
            yield ProviderEvent(
                kind=EventKind.message_completed,
                summary=output,
                payload={"content": output},
                raw_event_type="fake.message_completed",
            )
            yield ProviderEvent(
                kind=EventKind.completed,
                summary="Fake execution completed",
                payload={"final_output": output},
                raw_event_type="fake.completed",
            )
        finally:
            self.active -= 1
            self.active_by_workspace[workspace] -= 1

    async def steer(self, handle: ExecutionHandle, prompt: str) -> None:
        native: _FakeExecution = handle.native
        native.request.prompt += f"\n{prompt}"

    async def cancel(self, handle: ExecutionHandle) -> None:
        native: _FakeExecution = handle.native
        native.cancelled = True
        native.approval_event.set()
        self.cancel_count += 1

    async def resolve_approval(
        self,
        handle: ExecutionHandle,
        request_id: str,
        approved: bool,
    ) -> None:
        native: _FakeExecution = handle.native
        native.approval_result = approved
        native.approval_event.set()

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

from multi_agent.domain.errors import ProviderExecutionError, ProviderUnavailableError
from multi_agent.domain.models import (
    AccessMode,
    EventKind,
    ExecutionRequest,
    ProviderCapabilities,
    ProviderEvent,
    ProviderSessionRef,
)
from multi_agent.providers.base import AgentProvider, ExecutionHandle
from multi_agent.providers.utils import extract_text, native_event_type, redact_payload, to_plain_data


@dataclass(slots=True)
class _CopilotExecution:
    session: Any | None
    request: ExecutionRequest
    queue: asyncio.Queue[Any] = field(default_factory=asyncio.Queue)
    approvals: dict[str, asyncio.Future[bool]] = field(default_factory=dict)
    unsubscribe: Any | None = None


class CopilotProvider(AgentProvider):
    name = "copilot"
    _ALLOWED_OPTIONS = {
        "available_tools",
        "client_name",
        "excluded_tools",
        "model",
        "reasoning_effort",
        "reasoning_summary",
        "system_message",
    }

    def __init__(self, client: Any | None = None, sdk_module: Any | None = None) -> None:
        self._client = client
        self._sdk = sdk_module
        self._owns_client = client is None
        self._approve_type: Any | None = None
        self._reject_type: Any | None = None

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            resume_session=True,
            stream_events=True,
            steer_running_turn=True,
            cancel_running_turn=True,
            structured_output=False,
            approval_callback=True,
            read_only_mode=True,
            workspace_write_mode=False,
        )

    async def start(self) -> None:
        if self._client is not None:
            return
        try:
            import copilot as sdk
            from copilot.generated.rpc import (
                PermissionDecisionApproveOnce,
                PermissionDecisionReject,
            )
        except ImportError as exc:
            raise ProviderUnavailableError(
                "Copilot provider requires the optional package 'github-copilot-sdk'"
            ) from exc
        self._sdk = sdk
        self._approve_type = PermissionDecisionApproveOnce
        self._reject_type = PermissionDecisionReject
        self._client = sdk.CopilotClient()
        await self._client.start()

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            client = self._client
            self._client = None
            await client.stop()

    def _approval_decision(self, approved: bool) -> Any:
        if approved:
            return self._approve_type() if self._approve_type else {"kind": "approved"}
        return (
            self._reject_type(feedback="approval rejected")
            if self._reject_type
            else {"kind": "rejected", "feedback": "approval rejected"}
        )

    @staticmethod
    def _paths_within_workspace(value: Any, workspace: Path) -> bool:
        plain = to_plain_data(value)

        def check(item: Any) -> bool:
            if isinstance(item, dict):
                for key, nested in item.items():
                    if key.lower() in {"path", "file_path", "filepath", "cwd"} and nested:
                        candidate = Path(str(nested))
                        if not candidate.is_absolute():
                            candidate = workspace / candidate
                        try:
                            candidate.resolve(strict=False).relative_to(workspace)
                        except ValueError:
                            return False
                    if not check(nested):
                        return False
            elif isinstance(item, list):
                return all(check(nested) for nested in item)
            return True

        return check(plain)

    async def start_execution(
        self,
        request: ExecutionRequest,
        session: ProviderSessionRef | None = None,
    ) -> ExecutionHandle:
        await self.start()
        assert self._client is not None
        unknown = set(request.provider_options) - self._ALLOWED_OPTIONS
        if unknown:
            raise ProviderExecutionError(
                f"unsupported Copilot provider options: {sorted(unknown)}",
                code="invalid_provider_options",
            )

        execution = _CopilotExecution(session=None, request=request)

        async def on_permission_request(_request: Any, _invocation: Any) -> Any:
            if request.access == AccessMode.read_only:
                return self._approval_decision(False)
            if not self._paths_within_workspace(_request, request.workspace):
                return self._approval_decision(False)
            request_id = uuid4().hex
            future = asyncio.get_running_loop().create_future()
            execution.approvals[request_id] = future
            await execution.queue.put(
                ProviderEvent(
                    kind=EventKind.approval_required,
                    summary="Copilot tool requires approval",
                    payload={
                        "request_id": request_id,
                        "request": redact_payload(_request),
                    },
                    raw_event_type="copilot.permission.requested",
                )
            )
            approved = await future
            return self._approval_decision(approved)

        options = request.provider_options
        if request.access != AccessMode.read_only:
            raise ProviderExecutionError(
                "Copilot workspace-write is disabled until its runtime isolation is verified",
                code="unsupported_access_mode",
            )
        safe_read_tools = {"view", "grep", "glob"}
        requested_tools = options.get("available_tools")
        if requested_tools is not None:
            if not isinstance(requested_tools, list) or not set(requested_tools).issubset(
                safe_read_tools
            ):
                raise ProviderExecutionError(
                    "Copilot read-only available_tools must be a subset of view, grep, glob",
                    code="invalid_provider_options",
                )
            available_tools = requested_tools
        else:
            available_tools = sorted(safe_read_tools)
        session_kwargs = {
            "on_permission_request": on_permission_request,
            "model": options.get("model"),
            "client_name": options.get("client_name", "aitools-multi-agent"),
            "reasoning_effort": options.get("reasoning_effort"),
            "reasoning_summary": options.get("reasoning_summary"),
            "system_message": options.get("system_message"),
            "available_tools": available_tools,
            "excluded_tools": options.get("excluded_tools"),
            "working_directory": str(request.workspace),
            "streaming": True,
        }
        session_kwargs = {
            key: value for key, value in session_kwargs.items() if value is not None
        }
        if session is None:
            session_id = uuid4().hex
            native_session = await self._client.create_session(
                session_id=session_id,
                **session_kwargs,
            )
        else:
            session_id = session.session_id
            native_session = await self._client.resume_session(
                session_id,
                **session_kwargs,
            )
        execution.session = native_session
        loop = asyncio.get_running_loop()

        def on_event(event: Any) -> None:
            loop.call_soon_threadsafe(execution.queue.put_nowait, event)

        try:
            execution.unsubscribe = native_session.on(on_event)
            await native_session.send(request.prompt)
        except BaseException:
            if execution.unsubscribe:
                execution.unsubscribe()
                execution.unsubscribe = None
            try:
                await native_session.disconnect()
            except Exception:
                pass
            raise
        return ExecutionHandle(
            session=ProviderSessionRef(provider=self.name, session_id=session_id),
            native=execution,
        )

    async def stream(self, handle: ExecutionHandle) -> AsyncIterator[ProviderEvent]:
        execution: _CopilotExecution = handle.native
        collected: list[str] = []
        try:
            while True:
                native = await execution.queue.get()
                if isinstance(native, ProviderEvent):
                    yield native
                    continue
                event_type = native_event_type(native)
                payload_source = getattr(native, "data", native)
                payload = redact_payload(payload_source)
                text = extract_text(payload_source)
                lowered = event_type.lower()
                if "session.idle" in lowered:
                    yield ProviderEvent(
                        kind=EventKind.completed,
                        summary="Copilot session is idle",
                        payload={"final_output": "".join(collected), "native": payload},
                        raw_event_type=event_type,
                    )
                    break
                if "session.error" in lowered:
                    yield ProviderEvent(
                        kind=EventKind.failed,
                        summary=text or "Copilot session failed",
                        payload=payload if isinstance(payload, dict) else {"value": payload},
                        raw_event_type=event_type,
                    )
                    break
                if "message_delta" in lowered or "reasoning_delta" in lowered:
                    if text:
                        collected.append(text)
                    kind = EventKind.message_delta
                elif "assistant.message" in lowered:
                    if text:
                        collected = [text]
                    kind = EventKind.message_completed
                elif "tool" in lowered and ("complete" in lowered or "end" in lowered):
                    kind = EventKind.tool_completed
                elif "tool" in lowered:
                    kind = EventKind.tool_started
                elif "usage" in lowered:
                    kind = EventKind.usage
                else:
                    kind = EventKind.message_delta
                yield ProviderEvent(
                    kind=kind,
                    summary=text or event_type,
                    payload=payload if isinstance(payload, dict) else {"value": payload},
                    raw_event_type=event_type,
                )
        finally:
            if execution.unsubscribe:
                execution.unsubscribe()
                execution.unsubscribe = None
            for future in execution.approvals.values():
                if not future.done():
                    future.cancel()
            execution.approvals.clear()
            if execution.session is not None:
                await execution.session.disconnect()

    async def steer(self, handle: ExecutionHandle, prompt: str) -> None:
        execution: _CopilotExecution = handle.native
        await execution.session.send(prompt, mode="immediate")

    async def cancel(self, handle: ExecutionHandle) -> None:
        execution: _CopilotExecution = handle.native
        await execution.session.abort()

    async def resolve_approval(
        self,
        handle: ExecutionHandle,
        request_id: str,
        approved: bool,
    ) -> None:
        execution: _CopilotExecution = handle.native
        try:
            future = execution.approvals.pop(request_id)
        except KeyError as exc:
            raise ProviderExecutionError(
                f"unknown Copilot approval request: {request_id}",
                code="approval_not_found",
            ) from exc
        if not future.done():
            future.set_result(approved)

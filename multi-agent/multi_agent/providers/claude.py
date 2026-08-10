from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable
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
class _ClaudeExecution:
    client: Any
    request: ExecutionRequest
    queue: asyncio.Queue[Any] = field(default_factory=asyncio.Queue)
    approvals: dict[str, asyncio.Future[bool]] = field(default_factory=dict)


class ClaudeProvider(AgentProvider):
    name = "claude"
    _READ_TOOLS = {"Read", "Glob", "Grep"}
    _ALLOWED_OPTIONS = {"max_budget_usd", "max_turns", "model", "system_prompt"}

    def __init__(
        self,
        client_factory: Callable[..., Any] | None = None,
        options_factory: Callable[..., Any] | None = None,
        sdk_module: Any | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._options_factory = options_factory
        self._sdk = sdk_module

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
        if self._client_factory is not None and self._options_factory is not None:
            return
        try:
            import claude_agent_sdk as sdk
        except ImportError as exc:
            raise ProviderUnavailableError(
                "Claude provider requires the optional package 'claude-agent-sdk'"
            ) from exc
        self._sdk = sdk
        self._client_factory = sdk.ClaudeSDKClient
        self._options_factory = sdk.ClaudeAgentOptions

    def _allow(self, input_data: dict[str, Any]) -> Any:
        result_type = getattr(self._sdk, "PermissionResultAllow", None)
        return result_type(updated_input=input_data) if result_type else {"allow": True}

    def _deny(self, message: str) -> Any:
        result_type = getattr(self._sdk, "PermissionResultDeny", None)
        return result_type(message=message, interrupt=True) if result_type else {"allow": False}

    @staticmethod
    def _path_within_workspace(input_data: dict[str, Any], workspace: Path) -> bool:
        for key in ("file_path", "path", "notebook_path"):
            raw = input_data.get(key)
            if not raw:
                continue
            candidate = Path(str(raw))
            if not candidate.is_absolute():
                candidate = workspace / candidate
            try:
                candidate.resolve(strict=False).relative_to(workspace)
            except ValueError:
                return False
        return True

    async def start_execution(
        self,
        request: ExecutionRequest,
        session: ProviderSessionRef | None = None,
    ) -> ExecutionHandle:
        await self.start()
        assert self._client_factory is not None and self._options_factory is not None
        unknown = set(request.provider_options) - self._ALLOWED_OPTIONS
        if unknown:
            raise ProviderExecutionError(
                f"unsupported Claude provider options: {sorted(unknown)}",
                code="invalid_provider_options",
            )

        native_holder: dict[str, _ClaudeExecution] = {}

        async def can_use_tool(
            tool_name: str,
            input_data: dict[str, Any],
            _context: Any,
        ) -> Any:
            if tool_name in self._READ_TOOLS:
                return self._allow(input_data)
            if request.access == AccessMode.read_only:
                return self._deny("read-only task denied a mutating or external tool")
            if not self._path_within_workspace(input_data, request.workspace):
                return self._deny("tool path is outside the allowlisted workspace")
            execution = native_holder["execution"]
            request_id = uuid4().hex
            future = asyncio.get_running_loop().create_future()
            execution.approvals[request_id] = future
            await execution.queue.put(
                ProviderEvent(
                    kind=EventKind.approval_required,
                    summary=f"Claude tool {tool_name} requires approval",
                    payload={
                        "request_id": request_id,
                        "tool": tool_name,
                        "input": redact_payload(input_data),
                    },
                    raw_event_type="claude.permission.requested",
                )
            )
            approved = await future
            return self._allow(input_data) if approved else self._deny("approval rejected")

        options_kwargs: dict[str, Any] = {
            "cwd": str(request.workspace),
            "can_use_tool": can_use_tool,
            "model": request.provider_options.get("model"),
            "system_prompt": request.provider_options.get("system_prompt"),
            "max_turns": request.provider_options.get("max_turns"),
            "max_budget_usd": request.provider_options.get("max_budget_usd"),
            "resume": session.session_id if session else None,
            "permission_mode": "default",
            "setting_sources": [],
            "sandbox": {
                "enabled": True,
                "autoAllowBashIfSandboxed": False,
                "allowUnsandboxedCommands": False,
                "failIfUnavailable": True,
            },
            "output_format": (
                {"type": "json_schema", "schema": request.output_schema}
                if request.output_schema
                else None
            ),
        }
        if request.access == AccessMode.read_only:
            options_kwargs.update(
                allowed_tools=sorted(self._READ_TOOLS),
                disallowed_tools=[
                    "Bash",
                    "Edit",
                    "NotebookEdit",
                    "WebFetch",
                    "WebSearch",
                    "Write",
                ],
            )
        options = self._options_factory(
            **{key: value for key, value in options_kwargs.items() if value is not None}
        )
        client = self._client_factory(options=options)
        execution = _ClaudeExecution(client=client, request=request)
        native_holder["execution"] = execution
        try:
            await client.connect()
            await client.query(request.prompt)
            info = (
                await client.get_server_info()
                if hasattr(client, "get_server_info")
                else None
            )
        except BaseException:
            try:
                await client.disconnect()
            except Exception:
                pass
            raise
        info_data = to_plain_data(info)
        session_id = None
        if isinstance(info_data, dict):
            session_id = info_data.get("session_id") or info_data.get("sessionId")
        session_id = str(session_id or (session.session_id if session else uuid4().hex))
        return ExecutionHandle(
            session=ProviderSessionRef(provider=self.name, session_id=session_id),
            native=execution,
        )

    async def stream(self, handle: ExecutionHandle) -> AsyncIterator[ProviderEvent]:
        execution: _ClaudeExecution = handle.native
        stream_ended = object()

        async def pump_messages() -> None:
            try:
                async for native in execution.client.receive_response():
                    await execution.queue.put(native)
            finally:
                await execution.queue.put(stream_ended)

        pump = asyncio.create_task(pump_messages())
        try:
            while True:
                native = await execution.queue.get()
                if native is stream_ended:
                    break
                if isinstance(native, ProviderEvent):
                    yield native
                    continue
                event_type = native_event_type(native)
                payload = redact_payload(native)
                text = extract_text(native)
                lowered = event_type.lower()
                if "resultmessage" in lowered:
                    raw = to_plain_data(native)
                    is_error = bool(raw.get("is_error")) if isinstance(raw, dict) else False
                    subtype = str(raw.get("subtype", "")) if isinstance(raw, dict) else ""
                    final_output = text
                    if isinstance(raw, dict) and raw.get("structured_output") is not None:
                        final_output = raw["structured_output"]
                    kind = EventKind.failed if is_error or "error" in subtype else EventKind.completed
                    yield ProviderEvent(
                        kind=kind,
                        summary=text or ("Claude failed" if kind == EventKind.failed else "Claude completed"),
                        payload={
                            "final_output": final_output,
                            "native": payload,
                        },
                        raw_event_type=event_type,
                    )
                elif "assistantmessage" in lowered:
                    yield ProviderEvent(
                        kind=EventKind.message_completed,
                        summary=text or event_type,
                        payload=payload if isinstance(payload, dict) else {"value": payload},
                        raw_event_type=event_type,
                    )
                else:
                    yield ProviderEvent(
                        kind=EventKind.message_delta if text else EventKind.tool_started,
                        summary=text or event_type,
                        payload=payload if isinstance(payload, dict) else {"value": payload},
                        raw_event_type=event_type,
                    )
        finally:
            if not pump.done():
                pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
            for future in execution.approvals.values():
                if not future.done():
                    future.cancel()
            execution.approvals.clear()
            await execution.client.disconnect()

    async def steer(self, handle: ExecutionHandle, prompt: str) -> None:
        execution: _ClaudeExecution = handle.native
        await execution.client.query(prompt)

    async def cancel(self, handle: ExecutionHandle) -> None:
        execution: _ClaudeExecution = handle.native
        await execution.client.interrupt()

    async def resolve_approval(
        self,
        handle: ExecutionHandle,
        request_id: str,
        approved: bool,
    ) -> None:
        execution: _ClaudeExecution = handle.native
        try:
            future = execution.approvals.pop(request_id)
        except KeyError as exc:
            raise ProviderExecutionError(
                f"unknown Claude approval request: {request_id}",
                code="approval_not_found",
            ) from exc
        if not future.done():
            future.set_result(approved)

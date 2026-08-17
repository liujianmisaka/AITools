from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from multi_agent_v2.packages.agent_runtime.codex_catalog import catalog_from_app_server
from multi_agent_v2.packages.agent_runtime.codex_locator import (
    CodexRuntimeDescriptor,
    CodexRuntimeLocator,
)
from multi_agent_v2.packages.agent_runtime.codex_native import (
    extract_text,
    native_event_type,
    to_plain_data,
)
from multi_agent_v2.packages.agent_runtime.errors import AgentRuntimeError
from multi_agent_v2.packages.agent_runtime.models import (
    AgentCancelledEvent,
    AgentCompletedEvent,
    AgentErrorInfo,
    AgentEvent,
    AgentExecutionRequest,
    AgentFailedEvent,
    AgentMessageCompletedEvent,
    AgentMessageDeltaEvent,
    AgentModelCatalog,
    AgentReconcileRequest,
    AgentResumeRequest,
    AgentRuntimeCapabilities,
    AgentRuntimeDescription,
    AgentStartedEvent,
    AgentTurnHandle,
    CancelResult,
    PreparedAgentSession,
    ReconcileResult,
    logical_agent_request_key,
)
from multi_agent_v2.packages.agent_runtime.stream_contract import validate_agent_output
from multi_agent_v2.packages.domain.json_types import JsonObject, JsonValue
from multi_agent_v2.packages.sandbox import (
    Enforcement,
    SandboxAdmissionError,
    SandboxAttestation,
    require_sandbox,
)


@dataclass(slots=True)
class _PreparedNative:
    request: AgentExecutionRequest
    public: PreparedAgentSession
    client: Any  # pyright: ignore[reportExplicitAny]
    thread: Any  # pyright: ignore[reportExplicitAny]
    closed: bool = False


@dataclass(slots=True)
class _TurnNative:
    request: AgentExecutionRequest
    public: AgentTurnHandle
    prepared: _PreparedNative
    turn: Any  # pyright: ignore[reportExplicitAny]
    terminal: ReconcileResult | None = None
    stream_started: bool = False
    last_sequence: int = 0
    uncertain_reason: str | None = None


class CodexRuntime:
    name = "codex"

    def __init__(
        self,
        *,
        sdk_module: Any | None = None,  # pyright: ignore[reportExplicitAny]
        runtime_locator: CodexRuntimeLocator | None = None,
        catalog_ttl_seconds: float = 15.0,
        network_deny_is_enforced: bool = False,
        filesystem_enforcement: Enforcement = "partial",
    ) -> None:
        self._sdk = sdk_module
        self._locator = runtime_locator or CodexRuntimeLocator()
        self._catalog_ttl = max(catalog_ttl_seconds, 0.0)
        self._catalog: AgentModelCatalog | None = None
        self._catalog_signature: tuple[object, ...] | None = None
        self._catalog_loaded_at = 0.0
        self._catalog_lock = asyncio.Lock()
        self._network_deny_is_enforced = network_deny_is_enforced
        self._filesystem_enforcement: Enforcement = filesystem_enforcement
        self._prepared: dict[str, _PreparedNative] = {}
        self._turns: dict[str, _TurnNative] = {}
        self._terminal: dict[str, ReconcileResult] = {}
        self._closed = False

    async def describe(self) -> AgentRuntimeDescription:
        try:
            runtime = self._locator.resolve()
            catalog = await self.list_models()
        except Exception as exc:
            return AgentRuntimeDescription(
                name=self.name,
                runtime_id="codex:unavailable",
                available=False,
                capabilities=AgentRuntimeCapabilities(),
                error=AgentErrorInfo(code="agent.codex_unavailable", message=str(exc)),
            )
        return AgentRuntimeDescription(
            name=self.name,
            runtime_id=runtime.runtime_id,
            available=True,
            capabilities=AgentRuntimeCapabilities(
                new_session=True,
                resume_session=True,
                stream_events=True,
                steer_running_turn=True,
                cancel_running_turn=True,
                structured_output=True,
                reconcile_execution=True,
                read_only_mode=True,
                workspace_write_mode=True,
            ),
            sandbox_attestation=self._sandbox_attestation(),
            catalog_revision=catalog.revision,
            metadata={
                "environmentKind": runtime.environment_kind.value,
                "providerId": runtime.provider_id,
                "configSource": runtime.config_source,
                "processSupervision": "sdk_managed",
            },
        )

    async def list_models(self, *, refresh: bool = False) -> AgentModelCatalog:
        self._ensure_open()
        runtime = self._locator.resolve()
        now = time.monotonic()
        if (
            not refresh
            and self._catalog is not None
            and self._catalog_signature == runtime.signature
            and now - self._catalog_loaded_at < self._catalog_ttl
        ):
            return self._catalog
        async with self._catalog_lock:
            runtime = self._locator.resolve()
            await self._ensure_sdk()
            client = self._new_client(runtime)
            entered = False
            try:
                await client.__aenter__()
                entered = True
                models = getattr(client, "models", None)
                if not callable(models):
                    raise AgentRuntimeError(
                        "installed Codex SDK does not expose model/list",
                        code="agent.codex_catalog_unavailable",
                    )
                models_call = cast(Callable[..., Awaitable[Any]], models)
                response = await models_call(include_hidden=False)
                catalog = catalog_from_app_server(runtime, response)
            finally:
                if entered:
                    await client.__aexit__(None, None, None)
                else:
                    close = getattr(client, "close", None)
                    if callable(close):
                        close_call = cast(Callable[[], Awaitable[object]], close)
                        await close_call()
            self._catalog = catalog
            self._catalog_signature = runtime.signature
            self._catalog_loaded_at = time.monotonic()
            return catalog

    async def prepare_session(
        self,
        request: AgentExecutionRequest,
    ) -> PreparedAgentSession:
        self._ensure_open()
        await self.validate_request(request)

        execution_id = request.identity.execution_id
        existing = self._prepared.get(execution_id)
        if existing is not None:
            if logical_agent_request_key(existing.request) != logical_agent_request_key(request):
                raise AgentRuntimeError(
                    "execution ID was reused with a different request",
                    code="agent.idempotency_conflict",
                )
            return existing.public

        runtime = self._locator.resolve()
        await self._ensure_sdk()
        client = self._new_client(runtime, request=request)
        try:
            await client.__aenter__()
            options = self._thread_options(request)
            if isinstance(request, AgentResumeRequest):
                thread = await client.thread_resume(request.provider_session_id, **options)
            else:
                thread = await client.thread_start(ephemeral=False, **options)
        except BaseException:
            await self._close_client(client)
            raise
        session_id = str(getattr(thread, "id", "")).strip()
        if not session_id:
            await self._close_client(client)
            raise AgentRuntimeError(
                "Codex did not return a thread ID",
                code="agent.codex_session_missing",
                reconciliation_required=True,
            )
        public = PreparedAgentSession(
            handle_id=f"codex-session:{execution_id}",
            execution_id=execution_id,
            provider=self.name,
            provider_session_id=session_id,
        )
        self._prepared[execution_id] = _PreparedNative(
            request=request,
            public=public,
            client=client,
            thread=thread,
        )
        return public

    async def validate_request(self, request: AgentExecutionRequest) -> None:
        if request.provider != self.name:
            raise AgentRuntimeError(
                "request provider does not match Codex runtime",
                code="agent.provider_mismatch",
            )
        self._validate_policy(request)
        await self._validate_selection(request)

    async def start_turn(
        self,
        request: AgentExecutionRequest,
        session: PreparedAgentSession,
    ) -> AgentTurnHandle:
        self._ensure_open()
        execution_id = request.identity.execution_id
        existing_turn = self._turns.get(execution_id)
        if existing_turn is not None:
            if (
                logical_agent_request_key(existing_turn.request)
                != logical_agent_request_key(request)
                or existing_turn.prepared.public != session
            ):
                raise AgentRuntimeError(
                    "execution ID was reused with a different turn",
                    code="agent.idempotency_conflict",
                )
            return existing_turn.public
        prepared = self._prepared.get(execution_id)
        if (
            prepared is None
            or prepared.public != session
            or logical_agent_request_key(prepared.request) != logical_agent_request_key(request)
        ):
            raise AgentRuntimeError(
                "Codex turn does not reference the prepared session",
                code="agent.session_not_prepared",
            )
        try:
            turn = await prepared.thread.turn(request.prompt, **self._turn_options(request))
        except BaseException:
            await self._close_prepared(prepared)
            raise
        native_turn_id = str(getattr(turn, "id", "")).strip() or f"turn:{execution_id}"
        public = AgentTurnHandle(
            handle_id=f"codex-turn:{execution_id}",
            execution_id=execution_id,
            provider=self.name,
            provider_session_id=session.provider_session_id,
            provider_turn_id=native_turn_id,
        )
        self._turns[execution_id] = _TurnNative(
            request=request,
            public=public,
            prepared=prepared,
            turn=turn,
        )
        return public

    def stream(self, handle: AgentTurnHandle) -> AsyncIterator[AgentEvent]:
        return self._stream(handle)

    async def _stream(self, handle: AgentTurnHandle) -> AsyncIterator[AgentEvent]:
        state = self._turn(handle)
        if state.stream_started:
            raise AgentRuntimeError(
                "Codex turn stream is already being consumed",
                code="agent.stream_already_started",
                reconciliation_required=True,
            )
        state.stream_started = True
        sequence = 1
        state.last_sequence = sequence
        yield AgentStartedEvent(
            execution_id=handle.execution_id,
            sequence=sequence,
            provider_session_id=handle.provider_session_id,
            summary="Codex turn started",
            model=state.request.model,
            effort=state.request.effort,
        )
        final_answer: str | None = None
        unknown_phase_answer: str | None = None
        terminal: AgentEvent | None = None
        try:
            async for native in state.turn.stream():
                event_type = native_event_type(native)
                payload = to_plain_data(getattr(native, "payload", native))
                lowered = event_type.replace("_", "").lower()
                text = extract_text(payload)
                item_answer = self._agent_message_answer(payload)
                if item_answer is not None:
                    phase, answer = item_answer
                    if phase == "final_answer":
                        final_answer = answer
                    elif phase is None:
                        unknown_phase_answer = answer
                    sequence += 1
                    state.last_sequence = sequence
                    yield AgentMessageCompletedEvent(
                        execution_id=handle.execution_id,
                        sequence=sequence,
                        provider_session_id=handle.provider_session_id,
                        summary=answer[:4096],
                        native_event_type=event_type,
                        text=answer,
                    )
                    continue
                if "turn/completed" in lowered or lowered.endswith("turn.completed"):
                    sequence += 1
                    state.last_sequence = sequence
                    terminal = self._terminal_event(
                        state,
                        payload,
                        final_answer or unknown_phase_answer,
                        sequence,
                    )
                    yield terminal
                    break
                if "delta" in lowered and text:
                    sequence += 1
                    state.last_sequence = sequence
                    yield AgentMessageDeltaEvent(
                        execution_id=handle.execution_id,
                        sequence=sequence,
                        provider_session_id=handle.provider_session_id,
                        summary=text[:4096],
                        native_event_type=event_type,
                        text=text,
                    )
                elif "agentmessage" in lowered and text:
                    sequence += 1
                    state.last_sequence = sequence
                    yield AgentMessageCompletedEvent(
                        execution_id=handle.execution_id,
                        sequence=sequence,
                        provider_session_id=handle.provider_session_id,
                        summary=text[:4096],
                        native_event_type=event_type,
                        text=text,
                    )
            if terminal is None:
                state.uncertain_reason = "Codex stream ended without a terminal event"
                raise AgentRuntimeError(
                    state.uncertain_reason,
                    code="agent.stream_incomplete",
                    reconciliation_required=True,
                )
            state.terminal = self._reconcile_from_terminal(terminal, state.public)
            self._terminal[handle.execution_id] = state.terminal
        except asyncio.CancelledError:
            if terminal is None:
                state.uncertain_reason = "Codex stream consumer was cancelled before completion"
            raise
        except AgentRuntimeError:
            raise
        except BaseException as exc:
            state.uncertain_reason = (
                "Codex stream failed before the provider terminal state was known"
            )
            raise AgentRuntimeError(
                state.uncertain_reason,
                code="agent.stream_uncertain",
                reconciliation_required=True,
            ) from exc
        finally:
            await self._close_prepared(state.prepared)

    async def steer(self, handle: AgentTurnHandle, message: str) -> None:
        if not message.strip():
            raise AgentRuntimeError("steer message must not be blank", code="agent.steer_invalid")
        state = self._turn(handle)
        await state.turn.steer(message.strip())

    async def cancel(self, handle: AgentTurnHandle) -> CancelResult:
        state = self._turns.get(handle.execution_id)
        if state is None or state.public != handle:
            return CancelResult(execution_id=handle.execution_id, status="not_found")
        if state.terminal is not None:
            return CancelResult(execution_id=handle.execution_id, status="already_terminal")
        await state.turn.interrupt()
        return CancelResult(execution_id=handle.execution_id, status="requested")

    async def reconcile(self, request: AgentReconcileRequest) -> ReconcileResult:
        terminal = self._terminal.get(request.execution_id)
        if terminal is not None:
            return terminal
        turn = self._turns.get(request.execution_id)
        if turn is not None:
            if turn.uncertain_reason is not None:
                return ReconcileResult(
                    execution_id=request.execution_id,
                    status="uncertain",
                    provider_session_id=turn.public.provider_session_id,
                    provider_turn_id=turn.public.provider_turn_id,
                    last_sequence=max(request.last_sequence, turn.last_sequence),
                )
            return ReconcileResult(
                execution_id=request.execution_id,
                status="running",
                provider_session_id=turn.public.provider_session_id,
                provider_turn_id=turn.public.provider_turn_id,
                attachable=not turn.stream_started and request.last_sequence == 0,
                last_sequence=max(request.last_sequence, turn.last_sequence),
            )
        prepared = self._prepared.get(request.execution_id)
        if prepared is not None:
            return ReconcileResult(
                execution_id=request.execution_id,
                status="prepared",
                provider_session_id=prepared.public.provider_session_id,
                attachable=False,
                last_sequence=request.last_sequence,
            )
        if request.provider_session_id is not None or request.provider_turn_id is not None:
            return ReconcileResult(
                execution_id=request.execution_id,
                status="uncertain",
                provider_session_id=request.provider_session_id,
                last_sequence=request.last_sequence,
            )
        return ReconcileResult(execution_id=request.execution_id, status="not_found")

    async def attach(self, request: AgentReconcileRequest) -> AgentTurnHandle:
        turn = self._turns.get(request.execution_id)
        if turn is None or turn.terminal is not None:
            raise AgentRuntimeError(
                "Codex execution is not attachable in this worker",
                code="agent.execution_not_attachable",
                reconciliation_required=True,
            )
        if turn.stream_started or request.last_sequence > 0:
            raise AgentRuntimeError(
                "Codex SDK cannot reattach to a stream that has already been consumed",
                code="agent.execution_not_attachable",
                reconciliation_required=True,
            )
        if request.provider_session_id not in {
            None,
            turn.public.provider_session_id,
        } or request.provider_turn_id not in {None, turn.public.provider_turn_id}:
            raise AgentRuntimeError(
                "persisted Codex identity does not match active turn",
                code="agent.execution_identity_mismatch",
                reconciliation_required=True,
            )
        return turn.public

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.gather(
            *(self._close_prepared(prepared) for prepared in tuple(self._prepared.values())),
            return_exceptions=True,
        )

    async def _validate_selection(self, request: AgentExecutionRequest) -> None:
        catalog = await self.list_models(refresh=True)
        selected = next((model for model in catalog.models if model.id == request.model), None)
        if selected is None:
            raise AgentRuntimeError(
                "Codex model is not in the active runtime catalog",
                code="agent.model_unsupported",
            )
        if request.effort not in selected.efforts:
            raise AgentRuntimeError(
                "Codex effort is not supported by the selected model",
                code="agent.effort_unsupported",
            )

    async def _ensure_sdk(self) -> None:
        if self._sdk is not None:
            return
        try:
            import openai_codex as sdk  # pyright: ignore[reportMissingImports]
        except ImportError as exc:
            raise AgentRuntimeError(
                "Codex runtime requires the openai-codex package",
                code="agent.codex_sdk_missing",
            ) from exc
        self._sdk = sdk

    def _new_client(
        self,
        runtime: CodexRuntimeDescriptor,
        *,
        request: AgentExecutionRequest | None = None,
    ) -> Any:  # pyright: ignore[reportExplicitAny]
        config_factory = getattr(self._sdk, "CodexConfig", None)
        client_factory = getattr(self._sdk, "AsyncCodex", None)
        if not callable(config_factory) or not callable(client_factory):
            raise AgentRuntimeError(
                "installed Codex SDK has an incompatible API",
                code="agent.codex_sdk_incompatible",
            )
        config_kwargs: dict[str, object] = {"env": {"CODEX_HOME": str(runtime.codex_home)}}
        if request is not None and request.policy.network_policy == "deny":
            config_kwargs["config_overrides"] = (
                'web_search="disabled"',
                "tools.web_search=false",
                "sandbox_workspace_write.network_access=false",
            )
        if runtime.codex_bin is not None:
            config_kwargs["codex_bin"] = runtime.codex_bin
        return client_factory(config_factory(**config_kwargs))

    def _thread_options(self, request: AgentExecutionRequest) -> dict[str, object]:
        return {
            "approval_mode": self._enum("ApprovalMode", request.policy.approval_mode),
            "cwd": str(request.workspace.root),
            "model": request.model,
            "sandbox": self._enum("Sandbox", request.policy.sandbox_mode),
        }

    def _turn_options(self, request: AgentExecutionRequest) -> dict[str, object]:
        return {
            **self._thread_options(request),
            "effort": request.effort,
            "output_schema": json.loads(request.output_schema.canonical),
        }

    def _enum(self, owner: str, member: str) -> object:
        enum_owner = getattr(self._sdk, owner, None)
        return getattr(enum_owner, member, member) if enum_owner is not None else member

    def _validate_policy(self, request: AgentExecutionRequest) -> None:
        if request.policy.allowed_tool_profile != "coding-default":
            raise AgentRuntimeError(
                "Codex runtime does not support the requested tool profile",
                code="agent.tool_profile_unsupported",
            )
        if request.policy.network_policy == "deny" and not self._network_deny_is_enforced:
            raise AgentRuntimeError(
                "Codex network-deny policy requires a platform-enforced restricted runtime",
                code="agent.network_policy_unenforced",
            )
        try:
            require_sandbox(
                self._sandbox_attestation(request.policy.sandbox_mode),
                request.policy.sandbox_requirements,
            )
        except SandboxAdmissionError as exc:
            raise AgentRuntimeError(str(exc), code=exc.code) from exc

    def _sandbox_attestation(
        self,
        effective_policy: str = "runtime-capabilities",
    ) -> SandboxAttestation:
        return SandboxAttestation(
            filesystem=self._filesystem_enforcement,
            network="full" if self._network_deny_is_enforced else "unavailable",
            process_tree="sdk_managed",
            backend="codex-sdk",
            effective_policy=effective_policy,
            limitations=(
                "Codex SDK owns the CLI process lifecycle",
                "filesystem enforcement is reported by deployment configuration",
            ),
        )

    @staticmethod
    def _agent_message_answer(payload: JsonValue) -> tuple[str | None, str] | None:
        if not isinstance(payload, dict):
            return None
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "agentMessage":
            return None
        text = item.get("text")
        if not isinstance(text, str) or not text:
            return None
        phase = item.get("phase")
        return (str(phase) if phase is not None else None, text)

    def _terminal_event(
        self,
        state: _TurnNative,
        payload: JsonValue,
        text: str | None,
        sequence: int,
    ) -> AgentEvent:
        status = ""
        if isinstance(payload, dict):
            turn = payload.get("turn")
            if isinstance(turn, dict):
                status = str(turn.get("status", ""))
                if text is None:
                    text = self._final_answer_from_turn(turn)
        if status.lower() in {"interrupted", "cancelled"}:
            return AgentCancelledEvent(
                execution_id=state.public.execution_id,
                sequence=sequence,
                provider_session_id=state.public.provider_session_id,
                summary="Codex turn was interrupted",
                reason="provider confirmed interruption",
            )
        if status.lower() not in {"completed", "failed"}:
            raise AgentRuntimeError(
                f"Codex reported an unexpected terminal turn status: {status or 'missing'}",
                code="agent.codex_terminal_uncertain",
                reconciliation_required=True,
            )
        if "fail" in status.lower() or "error" in status.lower():
            error = AgentErrorInfo(
                code="agent.codex_turn_failed",
                message=text or "Codex turn failed",
            )
            return AgentFailedEvent(
                execution_id=state.public.execution_id,
                sequence=sequence,
                provider_session_id=state.public.provider_session_id,
                summary=error.message,
                error=error,
            )
        try:
            raw_output = json.loads(text or "")
            if not isinstance(raw_output, dict):
                raise ValueError
            output = cast(JsonObject, raw_output)
        except (json.JSONDecodeError, ValueError):
            error = AgentErrorInfo(
                code="agent.codex_output_invalid",
                message="Codex final response is not a JSON object",
            )
            return AgentFailedEvent(
                execution_id=state.public.execution_id,
                sequence=sequence,
                provider_session_id=state.public.provider_session_id,
                summary=error.message,
                error=error,
            )
        try:
            validate_agent_output(state.request.output_schema, output)
        except AgentRuntimeError as exc:
            error = AgentErrorInfo(code=exc.code, message=str(exc))
            return AgentFailedEvent(
                execution_id=state.public.execution_id,
                sequence=sequence,
                provider_session_id=state.public.provider_session_id,
                summary=error.message,
                error=error,
            )
        return AgentCompletedEvent(
            execution_id=state.public.execution_id,
            sequence=sequence,
            provider_session_id=state.public.provider_session_id,
            summary="Codex turn completed",
            output=output,
        )

    @classmethod
    def _final_answer_from_turn(cls, turn: JsonObject) -> str | None:
        items = turn.get("items")
        if not isinstance(items, list):
            return None
        unknown: str | None = None
        for raw_item in reversed(items):
            answer = cls._agent_message_answer({"item": raw_item})
            if answer is None:
                continue
            phase, text = answer
            if phase == "final_answer":
                return text
            if phase is None and unknown is None:
                unknown = text
        return unknown

    @staticmethod
    def _reconcile_from_terminal(
        event: AgentEvent,
        handle: AgentTurnHandle,
    ) -> ReconcileResult:
        if isinstance(event, AgentCompletedEvent):
            return ReconcileResult(
                execution_id=event.execution_id,
                status="succeeded",
                provider_session_id=event.provider_session_id,
                provider_turn_id=handle.provider_turn_id,
                last_sequence=event.sequence,
                output=event.output,
            )
        if isinstance(event, AgentCancelledEvent):
            return ReconcileResult(
                execution_id=event.execution_id,
                status="cancelled",
                provider_session_id=event.provider_session_id,
                provider_turn_id=handle.provider_turn_id,
                last_sequence=event.sequence,
            )
        assert isinstance(event, AgentFailedEvent)
        return ReconcileResult(
            execution_id=event.execution_id,
            status="failed",
            provider_session_id=event.provider_session_id,
            provider_turn_id=handle.provider_turn_id,
            last_sequence=event.sequence,
            error=event.error,
        )

    def _turn(self, handle: AgentTurnHandle) -> _TurnNative:
        state = self._turns.get(handle.execution_id)
        if state is None or state.public != handle:
            raise AgentRuntimeError("unknown Codex turn handle", code="agent.turn_not_found")
        return state

    async def _close_prepared(self, prepared: _PreparedNative) -> None:
        if prepared.closed:
            return
        prepared.closed = True
        await self._close_client(prepared.client)

    @staticmethod
    async def _close_client(client: Any) -> None:  # pyright: ignore[reportExplicitAny]
        await client.__aexit__(None, None, None)

    def _ensure_open(self) -> None:
        if self._closed:
            raise AgentRuntimeError("Codex runtime is closed", code="agent.runtime_closed")

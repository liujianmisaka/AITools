from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar, cast

from misaka_agent_capability import agent_descriptor, matches_json_schema
from misaka_invocation_contracts import (
    CapabilityDescriptor,
    CapabilityFeature,
    InvocationEvent,
    InvocationRequest,
    InvocationResult,
    InvocationStatus,
    ModelDescriptor,
    ReconcileResult,
    ReconcileStatus,
    SessionRef,
)
from misaka_invocation_runtime import ProviderExecutionError
from misaka_kernel_contracts import JsonObject, JsonValue
from misaka_session_capability import SessionLease, SessionLeaseError, SessionStore

from misaka_claude_provider.models import CLAUDE_EFFORTS, ClaudeModelCatalog, ClaudeProviderConfig
from misaka_claude_provider.native import NativeClaudeClient, NativeClaudeOptions, NativeClaudeSdk
from misaka_claude_provider.sdk import ClaudeAgentSdk

T = TypeVar("T")

_CANCELLATION_REASONS = frozenset(
    {
        "cancelled",
        "canceled",
        "interrupted",
        "interrupt",
        "user_cancelled",
        "aborted",
        "aborted_streaming",
        "aborted_tools",
    }
)
_READ_ONLY_TOOLS = ("Read", "Glob", "Grep")
_WORKSPACE_WRITE_TOOLS = ("Read", "Glob", "Grep", "Edit", "Write")


@dataclass(frozen=True, slots=True)
class _InvocationInput:
    prompt: str
    cwd: str
    sandbox: str


@dataclass(slots=True)
class _SessionLeaseState:
    lease: SessionLease
    stop: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    error: Exception | None = field(default=None, repr=False)
    on_lost: Callable[[Exception], Awaitable[None]] | None = field(default=None, repr=False)


@dataclass(slots=True)
class ClaudePreparedSession:
    provider: ClaudeAgentProvider
    request: InvocationRequest
    client: NativeClaudeClient
    session_id: str
    lease_state: _SessionLeaseState
    invocation_input: _InvocationInput
    entered: bool
    _handed_off: bool = False
    _closed: bool = False
    _operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def provider_session_id(self) -> str:
        return self.session_id

    @property
    def session_lease(self) -> SessionLease:
        return self.lease_state.lease

    @property
    def operation_lock(self) -> asyncio.Lock:
        return self._operation_lock

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def is_handed_off(self) -> bool:
        return self._handed_off

    def mark_handed_off(self) -> None:
        self._handed_off = True

    async def close(self) -> str | None:
        async with self._operation_lock:
            return await self._close_unlocked()

    async def close_for_provider(self) -> str | None:
        return await self._close_unlocked()

    async def _close_unlocked(self) -> str | None:
        if self._closed or self._handed_off:
            return None
        self._closed = True
        lease_error = await self.provider.stop_session_lease_heartbeat(self.lease_state)
        cleanup_error = await self.provider.close_client(self.client, entered=self.entered)
        self.entered = False
        release_error = await self.provider.release_session_lease(self.lease_state)
        return _combine_errors(lease_error, cleanup_error, release_error)


class ClaudeAgentProvider:
    def __init__(
        self,
        config: ClaudeProviderConfig | None = None,
        *,
        sdk: NativeClaudeSdk | None = None,
        session_store: SessionStore | None = None,
    ) -> None:
        self.config = config or ClaudeProviderConfig()
        self._sdk = sdk or ClaudeAgentSdk()
        self._session_store = session_store

    @property
    def session_store(self) -> SessionStore:
        return self._require_session_store()

    def bind_session_store(self, session_store: SessionStore) -> None:
        if self._session_store is not None and self._session_store is not session_store:
            raise RuntimeError("Claude provider session store is already bound")
        self._session_store = session_store

    async def describe(self) -> CapabilityDescriptor:
        return agent_descriptor(
            features=frozenset(
                {
                    CapabilityFeature.STRUCTURED_OUTPUT,
                    CapabilityFeature.STREAMING,
                    CapabilityFeature.CANCELLATION,
                    CapabilityFeature.RESUME,
                    CapabilityFeature.STEERING,
                }
            )
        )

    async def models(self, *, include_hidden: bool = False) -> ClaudeModelCatalog:
        del include_hidden
        return ClaudeModelCatalog(self.config.model_ids)

    async def model_catalog(
        self, *, include_hidden: bool = False
    ) -> tuple[ModelDescriptor, ...]:
        catalog = await self.models(include_hidden=include_hidden)
        return tuple(
            ModelDescriptor(
                model_id=model_id,
                display_name=model_id,
                description="Configured Claude Agent SDK model",
                supported_efforts=CLAUDE_EFFORTS,
            )
            for model_id in catalog.models
        )

    async def start(self, request: InvocationRequest) -> _ClaudeHandle:
        prepared = await self.prepare_session(request)
        try:
            return await self.start_turn(prepared)
        except BaseException:
            await prepared.close()
            raise

    async def prepare_session(self, request: InvocationRequest) -> ClaudePreparedSession:
        invocation_input = self._validate_request(request)
        self._require_session_store()
        native_session_id = (
            request.session_ref.native_id if request.session_ref is not None else str(uuid.uuid4())
        )
        session_ref = SessionRef(self.config.provider_id, native_session_id)
        lease_state: _SessionLeaseState | None = None
        client: NativeClaudeClient | None = None
        entered = False
        try:
            lease_state = _SessionLeaseState(
                await self._acquire_session_lease(session_ref, request)
            )
            self._start_session_lease_heartbeat(lease_state)
            options = self._build_options(request, invocation_input, native_session_id)
            client = self._sdk.create_client(options)
            await self._rpc(
                client.connect(),
                code="agent.claude_initialize_timeout",
                message="Claude SDK initialization exceeded its deadline",
                reconciliation_required=False,
            )
            entered = True
            return ClaudePreparedSession(
                provider=self,
                request=request,
                client=client,
                session_id=native_session_id,
                lease_state=lease_state,
                invocation_input=invocation_input,
                entered=entered,
            )
        except asyncio.CancelledError:
            if client is not None:
                await self.close_client(client, entered=True)
            if lease_state is not None:
                await self.stop_session_lease_heartbeat(lease_state)
                await self.release_session_lease(lease_state)
            raise
        except ProviderExecutionError:
            if client is not None:
                await self.close_client(client, entered=True)
            if lease_state is not None:
                await self.stop_session_lease_heartbeat(lease_state)
                await self.release_session_lease(lease_state)
            raise
        except Exception as exc:
            if client is not None:
                await self.close_client(client, entered=True)
            if lease_state is not None:
                await self.stop_session_lease_heartbeat(lease_state)
                await self.release_session_lease(lease_state)
            raise ProviderExecutionError(
                "agent.claude_prepare_unknown",
                str(exc),
                reconciliation_required=True,
            ) from exc

    async def start_turn(self, prepared: ClaudePreparedSession) -> _ClaudeHandle:
        async with prepared.operation_lock:
            if prepared.is_closed:
                raise ProviderExecutionError(
                    "agent.claude_prepared_session_closed",
                    "Claude prepared session has already been closed",
                )
            if prepared.is_handed_off:
                raise ProviderExecutionError(
                    "agent.claude_prepared_session_handed_off",
                    "Claude prepared session has already started a turn",
                )
            if prepared.lease_state.error is not None:
                await prepared.close_for_provider()
                raise ProviderExecutionError(
                    "agent.session_lease_lost",
                    str(prepared.lease_state.error),
                    reconciliation_required=True,
                )
            try:
                prepared.lease_state.lease = await self._require_session_store().renew(
                    prepared.lease_state.lease,
                    ttl_seconds=self.config.session_lease_ttl_seconds,
                )
            except Exception as exc:
                cleanup_error = await prepared.close_for_provider()
                raise ProviderExecutionError(
                    "agent.session_lease_lost",
                    _combine_errors(str(exc), cleanup_error) or str(exc),
                    reconciliation_required=True,
                ) from exc
            try:
                await self._rpc(
                    prepared.client.query(prepared.invocation_input.prompt),
                    code="agent.claude_turn_start_timeout",
                    message="Claude SDK turn start exceeded its deadline",
                    reconciliation_required=True,
                )
                handle = _ClaudeHandle(
                    provider=self,
                    request=prepared.request,
                    client=prepared.client,
                    session_id=prepared.session_id,
                    lease_state=prepared.lease_state,
                    entered=prepared.entered,
                )
                prepared.lease_state.on_lost = handle.session_lease_lost
                handle.start_consumer()
                prepared.mark_handed_off()
                return handle
            except asyncio.CancelledError:
                await prepared.close_for_provider()
                raise
            except ProviderExecutionError:
                await prepared.close_for_provider()
                raise
            except Exception as exc:
                await prepared.close_for_provider()
                raise ProviderExecutionError(
                    "agent.claude_turn_start_unknown",
                    str(exc),
                    reconciliation_required=True,
                ) from exc

    def _validate_request(self, request: InvocationRequest) -> _InvocationInput:
        if request.model is None or request.effort is None:
            raise ProviderExecutionError(
                "agent.model_selection_required",
                "Claude invocations must provide both model and effort",
            )
        if request.effort not in CLAUDE_EFFORTS:
            raise ProviderExecutionError(
                "agent.effort_invalid",
                "Claude effort must be one of low, medium, high, xhigh, or max",
            )
        invocation_input = self._parse_input(request)
        self._validate_policy(request)
        if (
            request.session_ref is not None
            and request.session_ref.provider != self.config.provider_id
        ):
            raise ProviderExecutionError(
                "agent.session_provider_mismatch",
                (
                    f"session belongs to provider {request.session_ref.provider}, "
                    f"not {self.config.provider_id}"
                ),
            )
        return invocation_input

    def _build_options(
        self,
        request: InvocationRequest,
        invocation_input: _InvocationInput,
        native_session_id: str,
    ) -> NativeClaudeOptions:
        model = request.model
        effort = request.effort
        if model is None or effort is None:
            raise ProviderExecutionError(
                "agent.model_selection_required",
                "Claude invocations must provide both model and effort",
            )
        env = {"CLAUDE_AGENT_SDK_CLIENT_APP": "misaka-multi-agent-v3/0.1.0"}
        if self.config.claude_config_dir is not None:
            env["CLAUDE_CONFIG_DIR"] = str(self.config.claude_config_dir.expanduser().resolve())
        output_format = (
            cast(JsonObject, {"type": "json_schema", "schema": request.output_schema})
            if request.output_schema is not None
            else None
        )
        return NativeClaudeOptions(
            model=model,
            effort=effort,
            cwd=invocation_input.cwd,
            resume=request.session_ref.native_id if request.session_ref is not None else None,
            session_id=None if request.session_ref is not None else native_session_id,
            cli_path=self.config.cli_path,
            env=env,
            tools=(
                _READ_ONLY_TOOLS
                if invocation_input.sandbox == "read_only"
                else _WORKSPACE_WRITE_TOOLS
            ),
            output_format=output_format,
            tool_policy=self._tool_policy(invocation_input),
        )

    def _tool_policy(
        self, invocation_input: _InvocationInput
    ) -> Callable[[str, Mapping[str, object]], Awaitable[bool]]:
        allowed_tools = set(
            _READ_ONLY_TOOLS
            if invocation_input.sandbox == "read_only"
            else _WORKSPACE_WRITE_TOOLS
        )
        root = Path(invocation_input.cwd)

        async def allow(tool_name: str, tool_input: Mapping[str, object]) -> bool:
            if tool_name not in allowed_tools:
                return False
            for key in ("file_path", "path", "directory"):
                value = tool_input.get(key)
                if isinstance(value, str) and value.strip() and not _within(root, value):
                    return False
            return True

        return allow

    def _parse_input(self, request: InvocationRequest) -> _InvocationInput:
        prompt = request.input.get("prompt")
        cwd = request.input.get("cwd")
        sandbox = request.input.get("sandbox", "read_only")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ProviderExecutionError("agent.prompt_required", "Claude prompt must be non-empty")
        if not isinstance(cwd, str) or not cwd.strip():
            raise ProviderExecutionError("agent.cwd_required", "Claude cwd must be explicit")
        requested = Path(cwd).expanduser()
        if not requested.is_absolute():
            raise ProviderExecutionError("agent.cwd_invalid", "Claude cwd must be an absolute path")
        path = requested.resolve()
        if not path.is_dir():
            raise ProviderExecutionError("agent.cwd_invalid", f"Claude cwd does not exist: {cwd}")
        if not isinstance(sandbox, str) or sandbox not in {"read_only", "workspace_write"}:
            raise ProviderExecutionError(
                "agent.sandbox_invalid",
                "Claude sandbox must be read_only or workspace_write",
            )
        return _InvocationInput(prompt.strip(), str(path), sandbox)

    def _validate_policy(self, request: InvocationRequest) -> None:
        network = request.policy_context.get("network_policy", "deny")
        if network not in {"allow", "deny"}:
            raise ProviderExecutionError(
                "agent.network_policy_invalid",
                "Claude network policy must be allow or deny",
            )
        if network == "deny" and not self.config.network_deny_enforced:
            raise ProviderExecutionError(
                "agent.network_policy_unenforced",
                "Claude network-deny policy requires an enforced runtime configuration",
            )

    async def _acquire_session_lease(
        self, session_ref: SessionRef, request: InvocationRequest
    ) -> SessionLease:
        try:
            store = self._require_session_store()
            await store.ensure(session_ref)
            return await store.acquire(
                session_ref,
                request.lease_owner or request.owner_id,
                request.invocation_id,
                ttl_seconds=self.config.session_lease_ttl_seconds,
            )
        except SessionLeaseError as exc:
            if getattr(exc, "code", None) == "session.lease_busy":
                raise ProviderExecutionError("agent.session_busy", str(exc)) from exc
            raise ProviderExecutionError("agent.session_lease_unavailable", str(exc)) from exc
        except Exception as exc:
            raise ProviderExecutionError("agent.session_lease_unavailable", str(exc)) from exc

    def _require_session_store(self) -> SessionStore:
        if self._session_store is None:
            raise ProviderExecutionError(
                "agent.session_store_unbound",
                "Claude provider requires a profile-bound Session Store",
            )
        return self._session_store

    def _start_session_lease_heartbeat(self, state: _SessionLeaseState) -> None:
        state.task = asyncio.create_task(self._renew_session_lease_loop(state))

    async def _renew_session_lease_loop(self, state: _SessionLeaseState) -> None:
        interval = self.config.session_lease_renew_interval_seconds
        if interval is None:
            interval = self.config.session_lease_ttl_seconds / 3
        try:
            while True:
                try:
                    await asyncio.wait_for(state.stop.wait(), timeout=interval)
                    return
                except TimeoutError:
                    pass
                state.lease = await self._require_session_store().renew(
                    state.lease,
                    ttl_seconds=self.config.session_lease_ttl_seconds,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.error = exc
            if state.on_lost is not None:
                try:
                    await state.on_lost(exc)
                except Exception as callback_error:
                    state.error = RuntimeError(
                        _combine_errors(str(exc), str(callback_error)) or str(exc)
                    )

    async def stop_session_lease_heartbeat(self, state: _SessionLeaseState) -> str | None:
        state.stop.set()
        task = state.task
        if task is not None and task is not asyncio.current_task():
            try:
                async with asyncio.timeout(self.config.rpc_timeout_seconds):
                    await task
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                return _combine_errors(
                    str(state.error) if state.error is not None else None,
                    "Claude session lease heartbeat did not stop before its deadline",
                )
        return str(state.error) if state.error is not None else None

    async def release_session_lease(self, state: _SessionLeaseState) -> str | None:
        try:
            await self._require_session_store().release(state.lease)
        except SessionLeaseError as exc:
            return str(exc)
        except Exception as exc:
            return str(exc)
        return None

    async def handle_finished(self, handle: _ClaudeHandle) -> str | None:
        lease_error = await self.stop_session_lease_heartbeat(handle.lease_state)
        release_error = await self.release_session_lease(handle.lease_state)
        return _combine_errors(lease_error, release_error)

    async def _rpc(
        self,
        operation: Awaitable[T],
        *,
        code: str,
        message: str,
        reconciliation_required: bool,
    ) -> T:
        try:
            async with asyncio.timeout(self.config.rpc_timeout_seconds):
                return await operation
        except TimeoutError as exc:
            raise ProviderExecutionError(
                code,
                message,
                reconciliation_required=reconciliation_required,
            ) from exc

    async def close_client(self, client: NativeClaudeClient, *, entered: bool = True) -> str | None:
        if not entered:
            return None
        try:
            async with asyncio.timeout(self.config.rpc_timeout_seconds):
                await client.disconnect()
        except Exception as exc:
            return str(exc)
        return None

    async def interrupt_after_session_lease_loss(self, client: NativeClaudeClient) -> None:
        await self._rpc(
            client.interrupt(),
            code="agent.session_lease_interrupt_timeout",
            message="Claude session lease was lost and turn interruption timed out",
            reconciliation_required=True,
        )

    async def interrupt_turn(self, client: NativeClaudeClient) -> None:
        await self._rpc(
            client.interrupt(),
            code="agent.claude_interrupt_timeout",
            message="Claude turn interruption exceeded its deadline",
            reconciliation_required=True,
        )

    async def steer_turn(self, client: NativeClaudeClient, prompt: str) -> None:
        await self._rpc(
            client.query(prompt),
            code="agent.claude_steer_timeout",
            message="Claude live input exceeded its deadline",
            reconciliation_required=True,
        )


class _ClaudeHandle:
    def __init__(
        self,
        *,
        provider: ClaudeAgentProvider,
        request: InvocationRequest,
        client: NativeClaudeClient,
        session_id: str,
        lease_state: _SessionLeaseState,
        entered: bool,
    ) -> None:
        self.provider = provider
        self.request = request
        self.client = client
        self.session_id = session_id
        self.lease_state = lease_state
        self._entered = entered
        self._events: asyncio.Queue[InvocationEvent | None] = asyncio.Queue()
        self._result: asyncio.Future[InvocationResult] = asyncio.get_running_loop().create_future()
        self._sequence = 0
        self._consumer: asyncio.Task[None] | None = None
        self._terminal_reconcile: ReconcileResult | None = None
        self._forced_result: InvocationResult | None = None
        self._control_lock = asyncio.Lock()
        self._pending_responses = 1
        self._cancel_requested = False
        self._operation_id = request.invocation_id
        self._turn_id = request.invocation_id
        self._final_text: str | None = None
        self._current_message_id: str | None = None
        self._content_blocks: dict[int, tuple[str, str]] = {}
        self._started_tools: set[str] = set()
        self._completed_tools: set[str] = set()
        self._message_count = 0

    @property
    def provider_session_id(self) -> str:
        return self.session_id

    @property
    def provider_operation_id(self) -> str:
        return self._operation_id

    def start_consumer(self) -> None:
        self._consumer = asyncio.create_task(self._consume())

    async def events(self) -> AsyncIterator[InvocationEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    async def wait(self) -> InvocationResult:
        result = await self._result
        if self._consumer is not None:
            await asyncio.gather(self._consumer, return_exceptions=True)
        return result

    async def cancel(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("cancellation reason must not be empty")
        if self._result.done():
            return
        self._cancel_requested = True
        try:
            async with self._control_lock:
                await self.provider.interrupt_turn(self.client)
        except Exception as exc:
            self._forced_result = InvocationResult(
                invocation_id=self.request.invocation_id,
                status=InvocationStatus.RECONCILIATION_REQUIRED,
                error_code=getattr(exc, "code", "agent.claude_cancel_unknown"),
                error_message=str(exc) or type(exc).__name__,
            )
            if self._consumer is not None:
                self._consumer.cancel()

    async def steer(self, input_value: JsonObject) -> None:
        prompt = _steer_input_text(input_value)
        if self._result.done():
            raise ProviderExecutionError(
                "agent.claude_turn_terminal",
                "a terminal Claude turn cannot receive live input",
            )
        if self._cancel_requested:
            raise ProviderExecutionError(
                "agent.claude_turn_stopping",
                "a Claude turn being interrupted cannot receive live input",
            )
        if self.lease_state.error is not None:
            raise ProviderExecutionError(
                "agent.session_lease_lost",
                str(self.lease_state.error),
                reconciliation_required=True,
            )
        try:
            async with self._control_lock:
                if self._result.done():
                    raise ProviderExecutionError(
                        "agent.claude_turn_terminal",
                        "a terminal Claude turn cannot receive live input",
                    )
                if self._cancel_requested:
                    raise ProviderExecutionError(
                        "agent.claude_turn_stopping",
                        "a Claude turn being interrupted cannot receive live input",
                    )
                self._pending_responses += 1
                try:
                    await self.provider.steer_turn(self.client, prompt)
                except BaseException:
                    self._pending_responses -= 1
                    raise
        except ProviderExecutionError:
            raise
        except Exception as exc:
            raise ProviderExecutionError(
                "agent.claude_steer_unknown",
                str(exc) or type(exc).__name__,
                reconciliation_required=True,
            ) from exc

    async def reconcile(self) -> ReconcileResult:
        if self._terminal_reconcile is not None:
            return self._terminal_reconcile
        return ReconcileResult(
            ReconcileStatus.RUNNING,
            provider_operation_id=self._operation_id,
            provider_session_id=self.session_id,
            provider_turn_id=self._operation_id,
            last_sequence=self._sequence,
            attachable=False,
        )

    async def session_lease_lost(self, error: Exception) -> None:
        if self._result.done():
            return
        message = f"Claude session lease was lost: {error}"
        self._forced_result = InvocationResult(
            invocation_id=self.request.invocation_id,
            status=InvocationStatus.RECONCILIATION_REQUIRED,
            error_code="agent.session_lease_lost",
            error_message=message,
        )
        try:
            async with self._control_lock:
                await self.provider.interrupt_after_session_lease_loss(self.client)
        except Exception as interrupt_error:
            self._forced_result = InvocationResult(
                invocation_id=self.request.invocation_id,
                status=InvocationStatus.RECONCILIATION_REQUIRED,
                error_code="agent.session_lease_lost",
                error_message=_combine_errors(message, str(interrupt_error)),
            )
        if self._consumer is not None and self._consumer is not asyncio.current_task():
            self._consumer.cancel()

    async def close(self) -> None:
        if self._result.done():
            return
        self._forced_result = InvocationResult(
            invocation_id=self.request.invocation_id,
            status=InvocationStatus.RECONCILIATION_REQUIRED,
            error_code="agent.claude_force_closed",
            error_message="Claude client was force-closed before terminal state was proven",
        )
        if self._consumer is not None:
            self._consumer.cancel()
            await asyncio.gather(self._consumer, return_exceptions=True)

    async def _consume(self) -> None:
        terminal: InvocationResult | None = None
        try:
            await self._emit("agent.turn.started", {"status": "in_progress"})
            async for message in self.client.receive_messages():
                if not self._validate_message_session(message):
                    terminal = InvocationResult(
                        invocation_id=self.request.invocation_id,
                        status=InvocationStatus.RECONCILIATION_REQUIRED,
                        error_code="agent.claude_session_identity_changed",
                        error_message="Claude returned a different session identity",
                    )
                    break
                kind = type(message).__name__
                if kind == "AssistantMessage":
                    await self._consume_assistant(message)
                elif kind == "UserMessage":
                    await self._consume_user(message)
                elif kind == "StreamEvent":
                    await self._consume_stream(message)
                elif kind in {
                    "TaskStartedMessage",
                    "TaskProgressMessage",
                    "TaskNotificationMessage",
                    "TaskUpdatedMessage",
                }:
                    await self._consume_task(message, kind)
                elif kind == "SystemMessage":
                    await self._emit_system(message)
                elif kind == "ResultMessage":
                    terminal = self._result_from_message(message)
                    completed_payload: JsonObject = {"status": terminal.status.value}
                    if terminal.error_message:
                        completed_payload["error_message"] = terminal.error_message
                    event_status = (
                        InvocationStatus.STOPPING
                        if self._cancel_requested
                        else InvocationStatus.RUNNING
                    )
                    await self._emit(
                        "agent.turn.completed",
                        completed_payload,
                        status=event_status,
                    )
                    if (
                        terminal.status is InvocationStatus.SUCCEEDED
                        and not self._cancel_requested
                    ):
                        async with self._control_lock:
                            if self._pending_responses > 1:
                                self._pending_responses -= 1
                                terminal = None
                                self._reset_response_state()
                        if terminal is None:
                            await self._emit(
                                "agent.turn.started",
                                {"status": "in_progress", "continued": True},
                            )
                            continue
                    break
            if terminal is None:
                terminal = InvocationResult(
                    invocation_id=self.request.invocation_id,
                    status=InvocationStatus.RECONCILIATION_REQUIRED,
                    error_code="agent.claude_stream_incomplete",
                    error_message="Claude stream ended without a ResultMessage",
                )
        except asyncio.CancelledError:
            terminal = self._forced_result or InvocationResult(
                invocation_id=self.request.invocation_id,
                status=InvocationStatus.RECONCILIATION_REQUIRED,
                error_code="agent.claude_stream_uncertain",
                error_message="Claude stream consumer stopped before terminal state",
            )
        except Exception as exc:
            terminal = InvocationResult(
                invocation_id=self.request.invocation_id,
                status=InvocationStatus.RECONCILIATION_REQUIRED,
                error_code="agent.claude_stream_uncertain",
                error_message=str(exc),
            )
        finally:
            if terminal is None:
                terminal = InvocationResult(
                    invocation_id=self.request.invocation_id,
                    status=InvocationStatus.RECONCILIATION_REQUIRED,
                    error_code="agent.claude_stream_uncertain",
                    error_message="Claude stream consumer stopped before terminal state",
                )
            if self._forced_result is not None:
                terminal = self._forced_result
            if self._entered:
                cleanup_error = await self.provider.close_client(self.client)
                if (
                    cleanup_error is not None
                    and terminal.status is InvocationStatus.RECONCILIATION_REQUIRED
                ):
                    terminal = InvocationResult(
                        invocation_id=self.request.invocation_id,
                        status=InvocationStatus.RECONCILIATION_REQUIRED,
                        error_code="agent.claude_cleanup_unknown",
                        error_message=cleanup_error,
                    )
                self._entered = False
            lease_error = await self.provider.handle_finished(self)
            if lease_error is not None:
                if terminal.status is InvocationStatus.RECONCILIATION_REQUIRED:
                    terminal = InvocationResult(
                        invocation_id=self.request.invocation_id,
                        status=InvocationStatus.RECONCILIATION_REQUIRED,
                        error_code=terminal.error_code or "agent.session_lease_unknown",
                        error_message=_combine_errors(terminal.error_message, lease_error),
                    )
                else:
                    terminal = InvocationResult(
                        invocation_id=self.request.invocation_id,
                        status=InvocationStatus.RECONCILIATION_REQUIRED,
                        error_code="agent.session_lease_unknown",
                        error_message=lease_error,
                    )
            self._finish(terminal)

    def _validate_message_session(self, message: object) -> bool:
        session_id = _read_string(message, "session_id")
        if session_id is None and type(message).__name__ == "SystemMessage":
            session_id = _read_string(getattr(message, "data", {}), "session_id")
        return session_id is None or session_id == self.session_id

    def _reset_response_state(self) -> None:
        self._final_text = None
        self._current_message_id = None
        self._content_blocks.clear()
        self._started_tools.clear()
        self._completed_tools.clear()

    async def _consume_assistant(self, message: object) -> None:
        raw_content = getattr(message, "content", ())
        if not isinstance(raw_content, (list, tuple)):
            return
        content = cast(list[object] | tuple[object, ...], raw_content)
        self._message_count += 1
        message_id = _read_string(message, "message_id", "uuid") or self._current_message_id
        if message_id is None:
            message_id = f"assistant-message-{self._message_count}"
        parent_tool_use_id = _read_string(message, "parent_tool_use_id")
        for index, block in enumerate(content):
            block_kind = type(block).__name__
            if block_kind == "TextBlock":
                text = _read_string(block, "text")
                if text:
                    if parent_tool_use_id is None:
                        self._final_text = text
                    payload: JsonObject = {
                        "item_id": self._block_item_id(index, message_id, "text"),
                        "text": text,
                    }
                    if parent_tool_use_id:
                        payload["parent_tool_use_id"] = parent_tool_use_id
                    await self._emit("agent.message.completed", payload)
            elif block_kind == "ToolUseBlock":
                name = _read_string(block, "name") or "unknown"
                tool_id = _read_string(block, "id") or "unknown"
                await self._emit_tool_started(tool_id, name, parent_tool_use_id)
            elif block_kind == "ThinkingBlock":
                payload = {"item_id": self._block_item_id(index, message_id, "reasoning")}
                if parent_tool_use_id:
                    payload["parent_tool_use_id"] = parent_tool_use_id
                await self._emit("agent.reasoning.completed", payload)

    async def _consume_user(self, message: object) -> None:
        raw_content = getattr(message, "content", ())
        if not isinstance(raw_content, (list, tuple)):
            return
        for block in cast(list[object] | tuple[object, ...], raw_content):
            if type(block).__name__ != "ToolResultBlock":
                continue
            tool_id = _read_string(block, "tool_use_id")
            if not tool_id or tool_id in self._completed_tools:
                continue
            self._completed_tools.add(tool_id)
            is_error = getattr(block, "is_error", None)
            payload: JsonObject = {
                "item_id": tool_id,
                "tool_use_id": tool_id,
                "status": "failed" if is_error is True else "completed",
            }
            text = _claude_tool_result_text(getattr(block, "content", None))
            if text:
                payload["text"] = text
            await self._emit("agent.tool.completed", payload)

    async def _consume_stream(self, message: object) -> None:
        event = _as_mapping(getattr(message, "event", {}))
        event_type = _read_string(event, "type")
        if event_type == "message_start":
            native_message = _as_mapping(event.get("message"))
            self._current_message_id = _read_string(native_message, "id")
            self._content_blocks.clear()
            return
        if event_type == "content_block_delta":
            delta = _as_mapping(event.get("delta"))
            index = _read_index(event)
            block_type, item_id = self._stream_block(index)
            delta_type = _read_string(delta, "type")
            if delta_type == "text_delta":
                text = _read_text_chunk(delta, "text")
                if text:
                    await self._emit(
                        "agent.message.delta",
                        {"item_id": item_id, "text": text},
                    )
            elif delta_type == "thinking_delta":
                # Raw chain-of-thought is intentionally not exposed. The lifecycle
                # event still tells observers that the reasoning block completed.
                return
            elif delta_type == "input_json_delta" and block_type == "tool_use":
                return
            return
        if event_type == "content_block_start":
            block = _as_mapping(event.get("content_block"))
            block_type = _read_string(block, "type") or "unknown"
            index = _read_index(event)
            item_id = _read_string(block, "id") or self._default_block_item_id(index, block_type)
            self._content_blocks[index] = (block_type, item_id)
            if block_type == "tool_use":
                await self._emit_tool_started(
                    item_id,
                    _read_string(block, "name") or "unknown",
                    None,
                )
            return

    async def _consume_task(self, message: object, kind: str) -> None:
        task_id = _read_string(message, "task_id") or "unknown-task"
        payload: JsonObject = {"item_id": task_id}
        description = _read_string(message, "description")
        summary = _read_string(message, "summary")
        status = _read_string(message, "status")
        tool_use_id = _read_string(message, "tool_use_id")
        if description:
            payload["summary"] = description
        elif summary:
            payload["summary"] = summary
        if status:
            payload["status"] = status
        if tool_use_id:
            payload["parent_tool_use_id"] = tool_use_id
        if kind == "TaskStartedMessage":
            await self._emit("agent.task.started", payload)
        elif kind == "TaskProgressMessage":
            name = _read_string(message, "last_tool_name")
            if name:
                payload["tool_name"] = name
            await self._emit("agent.task.progress", payload)
        else:
            patch = _as_mapping(getattr(message, "patch", {}))
            patch_status = _read_string(patch, "status")
            if patch_status:
                payload["status"] = patch_status
            await self._emit("agent.task.completed", payload)

    async def _emit_tool_started(
        self,
        tool_id: str,
        name: str,
        parent_tool_use_id: str | None,
    ) -> None:
        if tool_id in self._started_tools:
            return
        self._started_tools.add(tool_id)
        payload: JsonObject = {
            "item_id": tool_id,
            "tool_name": name,
            "tool_use_id": tool_id,
        }
        if parent_tool_use_id:
            payload["parent_tool_use_id"] = parent_tool_use_id
        await self._emit("agent.tool.started", payload)

    def _block_item_id(self, index: int, message_id: str, block_type: str) -> str:
        block = self._content_blocks.get(index)
        if block is not None and block[0] == block_type:
            return block[1]
        return f"{message_id}:{block_type}:{index}"

    def _stream_block(self, index: int) -> tuple[str, str]:
        block = self._content_blocks.get(index)
        if block is not None:
            return block
        return "text", self._default_block_item_id(index, "text")

    def _default_block_item_id(self, index: int, block_type: str) -> str:
        message_id = self._current_message_id or "assistant-message"
        return f"{message_id}:{block_type}:{index}"

    async def _emit_system(self, message: object) -> None:
        data = _as_mapping(getattr(message, "data", {}))
        subtype = _read_string(message, "subtype") or "system"
        payload: JsonObject = {"subtype": subtype}
        session_id = _read_string(data, "session_id")
        if session_id:
            payload["session_id"] = session_id
        await self._emit("agent.system", payload)

    def _result_from_message(self, message: object) -> InvocationResult:
        if self._cancel_requested:
            return InvocationResult(
                invocation_id=self.request.invocation_id,
                status=InvocationStatus.CANCELLED,
                error_code="agent.cancelled",
                error_message="Claude confirmed turn interruption",
            )
        terminal_reason = (
            _read_string(message, "terminal_reason", "stop_reason") or ""
        ).lower()
        if terminal_reason in _CANCELLATION_REASONS:
            return InvocationResult(
                invocation_id=self.request.invocation_id,
                status=InvocationStatus.CANCELLED,
                error_code="agent.cancelled",
                error_message="Claude reported an interrupted turn",
            )
        is_error = bool(getattr(message, "is_error", False))
        subtype = (_read_string(message, "subtype") or "").lower()
        if is_error or subtype.startswith("error_") or terminal_reason in {
            "api_error",
            "error",
            "max_turns",
            "max_budget_usd",
        }:
            errors = getattr(message, "errors", None)
            error_message = (
                _first_string(errors)
                or _read_string(message, "result")
                or "Claude turn failed"
            )
            return InvocationResult(
                invocation_id=self.request.invocation_id,
                status=InvocationStatus.FAILED,
                error_code="agent.claude_turn_failed",
                error_message=error_message,
            )
        structured = getattr(message, "structured_output", None)
        if self.request.output_schema is not None:
            output = structured
            if output is None:
                raw = _read_string(message, "result")
                if raw is None:
                    return _output_missing(self.request.invocation_id)
                try:
                    output = json.loads(raw)
                except json.JSONDecodeError:
                    return _output_invalid(self.request.invocation_id)
            if not _is_json_value(output) or not matches_json_schema(
                output, self.request.output_schema
            ):
                return InvocationResult(
                    invocation_id=self.request.invocation_id,
                    status=InvocationStatus.FAILED,
                    error_code="agent.output_contract_violated",
                    error_message="Claude output does not satisfy output_schema",
                )
            return InvocationResult(
                invocation_id=self.request.invocation_id,
                status=InvocationStatus.SUCCEEDED,
                output=cast(JsonValue, output),
            )
        raw_result = getattr(message, "result", None)
        if isinstance(raw_result, str) and raw_result.strip():
            return InvocationResult(
                invocation_id=self.request.invocation_id,
                status=InvocationStatus.SUCCEEDED,
                output=raw_result,
            )
        if self._final_text is not None and self._final_text.strip():
            return InvocationResult(
                invocation_id=self.request.invocation_id,
                status=InvocationStatus.SUCCEEDED,
                output=self._final_text,
            )
        if _is_json_value(structured):
            return InvocationResult(
                invocation_id=self.request.invocation_id,
                status=InvocationStatus.SUCCEEDED,
                output=cast(JsonValue, structured),
            )
        return _output_missing(self.request.invocation_id)

    async def _emit(
        self,
        event_type: str,
        payload: JsonObject,
        *,
        status: InvocationStatus = InvocationStatus.RUNNING,
    ) -> None:
        self._sequence += 1
        await self._events.put(
            InvocationEvent(
                invocation_id=self.request.invocation_id,
                sequence=self._sequence,
                status=status,
                payload={
                    "type": event_type,
                    "provider_session_id": self.session_id,
                    "provider_operation_id": self._operation_id,
                    "turn_id": self._turn_id,
                    **payload,
                },
            )
        )

    def _finish(self, result: InvocationResult) -> None:
        if self._result.done():
            return
        self._terminal_reconcile = _reconcile_from_result(
            result, self.session_id, self._operation_id, self._sequence
        )
        self._result.set_result(result)
        self._events.put_nowait(None)


def _output_missing(invocation_id: str) -> InvocationResult:
    return InvocationResult(
        invocation_id=invocation_id,
        status=InvocationStatus.FAILED,
        error_code="agent.claude_output_missing",
        error_message="Claude completed without a final agent message",
    )


def _output_invalid(invocation_id: str) -> InvocationResult:
    return InvocationResult(
        invocation_id=invocation_id,
        status=InvocationStatus.FAILED,
        error_code="agent.claude_output_invalid",
        error_message="Claude final response is not valid JSON",
    )


def _reconcile_from_result(
    result: InvocationResult, session_id: str, operation_id: str, sequence: int
) -> ReconcileResult:
    mapping = {
        InvocationStatus.SUCCEEDED: ReconcileStatus.SUCCEEDED,
        InvocationStatus.FAILED: ReconcileStatus.FAILED,
        InvocationStatus.CANCELLED: ReconcileStatus.CANCELLED,
        InvocationStatus.RECONCILIATION_REQUIRED: ReconcileStatus.UNREACHABLE,
    }
    return ReconcileResult(
        mapping[result.status],
        provider_operation_id=operation_id,
        provider_session_id=session_id,
        provider_turn_id=operation_id,
        last_sequence=sequence,
        output=result.output,
        error_code=result.error_code,
        error_message=result.error_message,
    )


def _steer_input_text(input_value: JsonObject) -> str:
    candidates = [
        value.strip()
        for field_name in ("prompt", "instruction", "text")
        if isinstance((value := input_value.get(field_name)), str) and value.strip()
    ]
    if len(candidates) != 1:
        raise ProviderExecutionError(
            "agent.claude_steer_input_invalid",
            "Claude live input requires exactly one non-empty prompt, instruction, or text field",
        )
    return candidates[0]


def _combine_errors(*errors: str | None) -> str | None:
    values = tuple(error for error in errors if error)
    return "; ".join(values) if values else None


def _as_mapping(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        raw = cast(dict[object, object], value)
        return {str(key): item for key, item in raw.items()}
    model_dump = cast(Callable[..., object] | None, getattr(value, "model_dump", None))
    if callable(model_dump):
        try:
            dumped = model_dump(mode="json")
        except Exception:
            dumped = None
        if isinstance(dumped, dict):
            raw = cast(dict[object, object], dumped)
            return {str(key): item for key, item in raw.items()}
    return {}


def _read_string(value: object, *keys: str) -> str | None:
    mapping = _as_mapping(value)
    for key in keys:
        item = mapping.get(key)
        if item is None:
            item = getattr(value, key, None)
        if isinstance(item, str) and item.strip():
            return item.strip()
    return None


def _read_text_chunk(value: object, *keys: str) -> str | None:
    mapping = _as_mapping(value)
    for key in keys:
        item = mapping.get(key)
        if isinstance(item, str) and item:
            return item
    return None


def _first_string(value: object) -> str | None:
    if isinstance(value, (list, tuple)):
        items = cast(list[object] | tuple[object, ...], value)
        for item in items:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def _read_index(event: dict[str, object]) -> int:
    value = event.get("index")
    return value if isinstance(value, int) and value >= 0 else 0


def _claude_tool_result_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if not isinstance(value, list):
        return None
    parts: list[str] = []
    for raw_part in cast(list[object], value):
        if isinstance(raw_part, str) and raw_part.strip():
            parts.append(raw_part.strip())
            continue
        text = _read_string(raw_part, "text", "content")
        if text:
            parts.append(text)
    return "\n".join(parts) or None


def _within(root: Path, requested: str) -> bool:
    path = Path(requested).expanduser()
    if not path.is_absolute():
        path = root / path
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return resolved == root or root in resolved.parents


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, bool | int | float | str):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in cast(list[object], value))
    if isinstance(value, dict):
        raw = cast(dict[object, object], value)
        return all(isinstance(key, str) and _is_json_value(item) for key, item in raw.items())
    return False

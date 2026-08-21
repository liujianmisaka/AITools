from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
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
from misaka_invocation_runtime import ProviderExecutionError, ProviderHandle
from misaka_kernel_contracts import JsonObject, JsonValue
from misaka_session_capability import (
    SessionLease,
    SessionLeaseError,
    SessionStore,
)

from misaka_codex_provider.models import CodexModel, CodexModelCatalog, CodexProviderConfig
from misaka_codex_provider.native import NativeClient, NativeSdk, NativeThread, NativeTurn
from misaka_codex_provider.sdk import OpenAICodexSdk

T = TypeVar("T")


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
class CodexPreparedSession:
    provider: CodexAgentProvider
    request: InvocationRequest
    client: NativeClient
    thread: NativeThread
    session_id: str
    lease_state: _SessionLeaseState
    invocation_input: _InvocationInput
    entered: bool
    _handed_off: bool = False
    _closed: bool = False
    _operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def close(self) -> str | None:
        async with self._operation_lock:
            return await self._close_unlocked()

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


class CodexAgentProvider:
    def __init__(
        self,
        config: CodexProviderConfig | None = None,
        *,
        sdk: NativeSdk | None = None,
        session_store: SessionStore | None = None,
    ) -> None:
        self.config = config or CodexProviderConfig()
        self._sdk = sdk or OpenAICodexSdk(self.config)
        self._session_store = session_store

    @property
    def session_store(self) -> SessionStore:
        return self._require_session_store()

    def bind_session_store(self, session_store: SessionStore) -> None:
        if self._session_store is not None and self._session_store is not session_store:
            raise RuntimeError("Codex provider session store is already bound")
        self._session_store = session_store

    async def describe(self) -> CapabilityDescriptor:
        return agent_descriptor(
            features=frozenset(
                {
                    CapabilityFeature.STRUCTURED_OUTPUT,
                    CapabilityFeature.STREAMING,
                    CapabilityFeature.CANCELLATION,
                    CapabilityFeature.RESUME,
                }
            )
        )

    async def models(self, *, include_hidden: bool = False) -> CodexModelCatalog:
        client = self._sdk.create_client()
        catalog: CodexModelCatalog
        completed = False
        try:
            await self._rpc(
                client.__aenter__(),
                code="agent.codex_initialize_timeout",
                message="Codex SDK initialization exceeded its deadline",
                reconciliation_required=False,
            )
            response = await self._rpc(
                client.models(include_hidden=include_hidden),
                code="agent.codex_catalog_timeout",
                message="Codex model catalog request exceeded its deadline",
                reconciliation_required=False,
            )
            raw = _as_mapping(response)
            if raw.get("next_cursor") or raw.get("nextCursor"):
                raise ProviderExecutionError(
                    "agent.codex_catalog_pagination",
                    "Codex model catalog returned an unhandled next cursor",
                )
            raw_models = raw.get("data", [])
            if not isinstance(raw_models, list):
                raise ProviderExecutionError(
                    "agent.codex_catalog_invalid",
                    "Codex model catalog data is not a list",
                )
            models: list[CodexModel] = []
            for raw_model in cast(list[object], raw_models):
                model = _as_mapping(raw_model)
                model_id = _read_string(model, "id")
                if model_id is None:
                    continue
                efforts = model.get(
                    "supported_reasoning_efforts",
                    model.get("supportedReasoningEfforts", []),
                )
                effort_values = _effort_values(efforts)
                models.append(
                    CodexModel(
                        id=model_id,
                        display_name=_read_string(model, "display_name", "displayName") or model_id,
                        description=_read_string(model, "description") or "",
                        supported_efforts=effort_values,
                    )
                )
            catalog = CodexModelCatalog(tuple(models))
            completed = True
        finally:
            cleanup_error = await self.close_client(client)
            if completed and cleanup_error is not None:
                raise ProviderExecutionError(
                    "agent.codex_cleanup_unknown",
                    cleanup_error,
                )
        return catalog

    async def model_catalog(self, *, include_hidden: bool = False) -> tuple[ModelDescriptor, ...]:
        catalog = await self.models(include_hidden=include_hidden)
        return tuple(
            ModelDescriptor(
                model_id=model.id,
                display_name=model.display_name,
                description=model.description,
                supported_efforts=model.supported_efforts,
            )
            for model in catalog.models
        )

    async def start(self, request: InvocationRequest) -> ProviderHandle:
        prepared = await self.prepare_session(request)
        try:
            return await self.start_turn(prepared)
        except BaseException:
            await prepared.close()
            raise

    async def prepare_session(self, request: InvocationRequest) -> CodexPreparedSession:
        invocation_input = self._validate_request(request)
        self._require_session_store()
        session_ref = request.session_ref
        client: NativeClient | None = None
        entered = False
        lease_state: _SessionLeaseState | None = None
        try:
            if session_ref is not None:
                lease_state = _SessionLeaseState(
                    await self._acquire_session_lease(session_ref, request)
                )
                self._start_session_lease_heartbeat(lease_state)
            client = self._sdk.create_client()
            await self._rpc(
                client.__aenter__(),
                code="agent.codex_initialize_timeout",
                message="Codex SDK initialization exceeded its deadline",
                reconciliation_required=False,
            )
            entered = True
            thread = await self._rpc(
                self._open_thread(client, request, invocation_input),
                code="agent.codex_thread_timeout",
                message="Codex thread creation or resume exceeded its deadline",
                reconciliation_required=True,
            )
            session_id = _read_string(thread, "id")
            if session_id is None:
                raise ProviderExecutionError(
                    "agent.codex_session_missing",
                    "Codex did not return a thread id",
                    reconciliation_required=True,
                )
            if session_ref is not None and session_id != session_ref.native_id:
                raise ProviderExecutionError(
                    "agent.codex_session_identity_changed",
                    "Codex resumed a different thread than the requested session",
                    reconciliation_required=True,
                )
            if lease_state is None:
                lease_state = _SessionLeaseState(
                    await self._acquire_session_lease(
                        SessionRef(self.config.provider_id, session_id),
                        request,
                        external_session_created=True,
                    )
                )
                self._start_session_lease_heartbeat(lease_state)
            return CodexPreparedSession(
                provider=self,
                request=request,
                client=client,
                thread=thread,
                session_id=session_id,
                lease_state=lease_state,
                invocation_input=invocation_input,
                entered=entered,
            )
        except asyncio.CancelledError:
            if client is not None:
                await self.close_client(client, entered=entered)
            if lease_state is not None:
                await self.stop_session_lease_heartbeat(lease_state)
                await self.release_session_lease(lease_state)
            raise
        except ProviderExecutionError:
            if client is not None:
                await self.close_client(client, entered=entered)
            if lease_state is not None:
                await self.stop_session_lease_heartbeat(lease_state)
                await self.release_session_lease(lease_state)
            raise
        except Exception as exc:
            if client is not None:
                await self.close_client(client, entered=entered)
            if lease_state is not None:
                await self.stop_session_lease_heartbeat(lease_state)
                await self.release_session_lease(lease_state)
            raise ProviderExecutionError(
                "agent.codex_prepare_unknown",
                str(exc),
                reconciliation_required=True,
            ) from exc

    async def start_turn(self, prepared: CodexPreparedSession) -> ProviderHandle:
        async with prepared.operation_lock:
            if prepared.is_closed:
                raise ProviderExecutionError(
                    "agent.codex_prepared_session_closed",
                    "Codex prepared session has already been closed",
                )
            if prepared.is_handed_off:
                raise ProviderExecutionError(
                    "agent.codex_prepared_session_handed_off",
                    "Codex prepared session has already started a turn",
                )
            request = prepared.request
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
            if request.model is None or request.effort is None:
                await prepared.close_for_provider()
                raise ProviderExecutionError(
                    "agent.model_selection_required",
                    "Codex invocations must provide both model and effort",
                )
            try:
                turn = await self._rpc(
                    prepared.thread.turn(
                        prepared.invocation_input.prompt,
                        approval_mode=self._sdk.approval_deny_all(),
                        cwd=prepared.invocation_input.cwd,
                        effort=self._sdk.effort(request.effort),
                        model=request.model,
                        output_schema=request.output_schema,
                        sandbox=self._sdk.sandbox(prepared.invocation_input.sandbox),
                    ),
                    code="agent.codex_turn_start_timeout",
                    message="Codex turn start exceeded its deadline",
                    reconciliation_required=True,
                )
                turn_id = _read_string(turn, "id")
                if turn_id is None:
                    raise ProviderExecutionError(
                        "agent.codex_turn_missing",
                        "Codex did not return a turn id",
                        reconciliation_required=True,
                    )
                handle = _CodexHandle(
                    provider=self,
                    request=request,
                    client=prepared.client,
                    turn=turn,
                    session_id=prepared.session_id,
                    turn_id=turn_id,
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
                    "agent.codex_turn_start_unknown",
                    str(exc),
                    reconciliation_required=True,
                ) from exc

    def _validate_request(self, request: InvocationRequest) -> _InvocationInput:
        if request.model is None or request.effort is None:
            raise ProviderExecutionError(
                "agent.model_selection_required",
                "Codex invocations must provide both model and effort",
            )
        invocation_input = self._parse_input(request)
        _validate_codex_schema(request.output_schema)
        self._validate_policy(request)
        session_ref = request.session_ref
        if session_ref is not None and session_ref.provider != self.config.provider_id:
            raise ProviderExecutionError(
                "agent.session_provider_mismatch",
                (
                    f"session belongs to provider {session_ref.provider}, "
                    f"not {self.config.provider_id}"
                ),
            )

        return invocation_input

    async def _open_thread(
        self,
        client: NativeClient,
        request: InvocationRequest,
        invocation_input: _InvocationInput,
    ) -> NativeThread:
        approval_mode = self._sdk.approval_deny_all()
        sandbox = self._sdk.sandbox(invocation_input.sandbox)
        if request.session_ref is None:
            model = request.model
            if model is None:
                raise ProviderExecutionError(
                    "agent.model_selection_required",
                    "Codex invocations must provide a model",
                )
            return await client.thread_start(
                approval_mode=approval_mode,
                cwd=invocation_input.cwd,
                ephemeral=self.config.new_sessions_ephemeral,
                model=model,
                sandbox=sandbox,
            )
        model = request.model
        if model is None:
            raise ProviderExecutionError(
                "agent.model_selection_required",
                "Codex invocations must provide a model",
            )
        return await client.thread_resume(
            request.session_ref.native_id,
            approval_mode=approval_mode,
            cwd=invocation_input.cwd,
            model=model,
            sandbox=sandbox,
        )

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

    async def close_client(self, client: NativeClient, *, entered: bool = True) -> str | None:
        if not entered:
            return None
        try:
            async with asyncio.timeout(self.config.rpc_timeout_seconds):
                await client.__aexit__(None, None, None)
        except Exception as exc:
            return str(exc)
        return None

    async def interrupt_turn_after_session_lease_loss(self, turn: NativeTurn) -> None:
        await self._rpc(
            turn.interrupt(),
            code="agent.session_lease_interrupt_timeout",
            message="Codex session lease was lost and turn interruption timed out",
            reconciliation_required=True,
        )

    def _parse_input(self, request: InvocationRequest) -> _InvocationInput:
        prompt = request.input.get("prompt")
        cwd = request.input.get("cwd")
        sandbox = request.input.get("sandbox", "read_only")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ProviderExecutionError("agent.prompt_required", "Codex prompt must be non-empty")
        if not isinstance(cwd, str) or not cwd.strip():
            raise ProviderExecutionError("agent.cwd_required", "Codex cwd must be explicit")
        if not self.config.workspace_roots:
            raise ProviderExecutionError(
                "agent.workspace_allowlist_required",
                "Codex provider must be configured with at least one workspace root",
            )
        path = Path(cwd).expanduser().resolve()
        if not path.is_dir():
            raise ProviderExecutionError("agent.cwd_invalid", f"Codex cwd does not exist: {cwd}")
        if not any(_is_within(path, root) for root in self.config.workspace_roots):
            raise ProviderExecutionError(
                "agent.cwd_not_allowed",
                f"Codex cwd is outside configured workspace roots: {cwd}",
            )
        if not isinstance(sandbox, str) or sandbox not in {"read_only", "workspace_write"}:
            raise ProviderExecutionError(
                "agent.sandbox_invalid",
                "Codex sandbox must be read_only or workspace_write",
            )
        return _InvocationInput(prompt.strip(), str(path), sandbox)

    def _validate_policy(self, request: InvocationRequest) -> None:
        network = request.policy_context.get("network_policy", "deny")
        if network not in {"allow", "deny"}:
            raise ProviderExecutionError(
                "agent.network_policy_invalid",
                "Codex network policy must be allow or deny",
            )
        if network == "deny" and not self.config.network_deny_enforced:
            raise ProviderExecutionError(
                "agent.network_policy_unenforced",
                "Codex network-deny policy requires an enforced runtime configuration",
            )

    async def _acquire_session_lease(
        self,
        session_ref: SessionRef,
        request: InvocationRequest,
        *,
        external_session_created: bool = False,
    ) -> SessionLease:
        session_store = self._require_session_store()
        try:
            await session_store.ensure(session_ref)
            return await session_store.acquire(
                session_ref,
                request.lease_owner or request.owner_id,
                request.invocation_id,
                ttl_seconds=self.config.session_lease_ttl_seconds,
            )
        except SessionLeaseError as exc:
            if getattr(exc, "code", None) == "session.lease_busy" and not external_session_created:
                raise ProviderExecutionError(
                    "agent.session_busy",
                    str(exc),
                ) from exc
            raise ProviderExecutionError(
                "agent.session_lease_unavailable",
                str(exc),
                reconciliation_required=external_session_created,
            ) from exc
        except Exception as exc:
            raise ProviderExecutionError(
                "agent.session_lease_unavailable",
                str(exc),
                reconciliation_required=external_session_created,
            ) from exc

    def _require_session_store(self) -> SessionStore:
        if self._session_store is None:
            raise ProviderExecutionError(
                "agent.session_store_unbound",
                "Codex provider requires a profile-bound Session Store",
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
                    "Codex session lease heartbeat did not stop before its deadline",
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

    async def handle_finished(self, handle: _CodexHandle) -> str | None:
        lease_error = await self.stop_session_lease_heartbeat(handle.lease_state)
        release_error = await self.release_session_lease(handle.lease_state)
        return _combine_errors(lease_error, release_error)


class _CodexHandle:
    def __init__(
        self,
        *,
        provider: CodexAgentProvider,
        request: InvocationRequest,
        client: NativeClient,
        turn: NativeTurn,
        session_id: str,
        turn_id: str,
        lease_state: _SessionLeaseState,
        entered: bool,
    ) -> None:
        self.provider = provider
        self.request = request
        self.client = client
        self.turn = turn
        self.session_id = session_id
        self.turn_id = turn_id
        self.lease_state = lease_state
        self._entered = entered
        self._events: asyncio.Queue[InvocationEvent | None] = asyncio.Queue()
        self._result: asyncio.Future[InvocationResult] = asyncio.get_running_loop().create_future()
        self._sequence = 0
        self._consumer: asyncio.Task[None] | None = None
        self._terminal_reconcile: ReconcileResult | None = None
        self._forced_result: InvocationResult | None = None

    @property
    def provider_session_id(self) -> str:
        return self.session_id

    @property
    def provider_operation_id(self) -> str:
        return self.turn_id

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
        try:
            await self.turn.interrupt()
        except Exception as exc:
            self._forced_result = InvocationResult(
                invocation_id=self.request.invocation_id,
                status=InvocationStatus.RECONCILIATION_REQUIRED,
                error_code="agent.codex_cancel_unknown",
                error_message=str(exc),
            )
            if self._consumer is not None:
                self._consumer.cancel()

    async def reconcile(self) -> ReconcileResult:
        if self._terminal_reconcile is not None:
            return self._terminal_reconcile
        return ReconcileResult(
            ReconcileStatus.RUNNING,
            provider_operation_id=self.turn_id,
            provider_session_id=self.session_id,
            provider_turn_id=self.turn_id,
            last_sequence=self._sequence,
            attachable=False,
        )

    async def session_lease_lost(self, error: Exception) -> None:
        if self._result.done():
            return
        message = f"Codex session lease was lost: {error}"
        self._forced_result = InvocationResult(
            invocation_id=self.request.invocation_id,
            status=InvocationStatus.RECONCILIATION_REQUIRED,
            error_code="agent.session_lease_lost",
            error_message=message,
        )
        try:
            await self.provider.interrupt_turn_after_session_lease_loss(self.turn)
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
            error_code="agent.codex_force_closed",
            error_message="Codex client was force-closed before terminal state was proven",
        )
        if self._consumer is not None:
            self._consumer.cancel()
            await asyncio.gather(self._consumer, return_exceptions=True)

    async def _consume(self) -> None:
        final_answer: str | None = None
        unknown_answer: str | None = None
        terminal: InvocationResult | None = None
        try:
            async for notification in self.turn.stream():
                method = getattr(notification, "method", "")
                payload = _as_mapping(getattr(notification, "payload", notification))
                lowered = method.replace("_", "").lower()
                item = _as_mapping(payload.get("item"))
                if "item/completed" in lowered or lowered.endswith("itemcompletednotification"):
                    text = _read_string(item, "text")
                    if item.get("type") == "agentMessage" and text:
                        phase = _read_string(item, "phase")
                        if phase == "final_answer":
                            final_answer = text
                        elif phase is None:
                            unknown_answer = text
                        await self._emit("agent.message.completed", {"text": text, "phase": phase})
                    continue
                if "agentmessage/delta" in lowered or lowered.endswith(
                    "agentmessagedeltanotification"
                ):
                    text = _read_string(payload, "delta") or _read_string(payload, "text")
                    if text:
                        await self._emit("agent.message.delta", {"text": text})
                    continue
                if "turn/completed" not in lowered and not lowered.endswith(
                    "turncompletednotification"
                ):
                    continue
                turn = _as_mapping(payload.get("turn"))
                status = (_read_string(turn, "status") or "").lower()
                if status in {"interrupted", "cancelled"}:
                    terminal = InvocationResult(
                        invocation_id=self.request.invocation_id,
                        status=InvocationStatus.CANCELLED,
                        error_code="agent.cancelled",
                        error_message="Codex confirmed turn interruption",
                    )
                elif status == "failed":
                    message = _turn_error_message(turn) or "Codex turn failed"
                    terminal = InvocationResult(
                        invocation_id=self.request.invocation_id,
                        status=InvocationStatus.FAILED,
                        error_code="agent.codex_turn_failed",
                        error_message=message,
                    )
                elif status == "completed":
                    terminal = self._completed_result(final_answer or unknown_answer)
                else:
                    terminal = InvocationResult(
                        invocation_id=self.request.invocation_id,
                        status=InvocationStatus.RECONCILIATION_REQUIRED,
                        error_code="agent.codex_terminal_uncertain",
                        error_message=(
                            f"Codex returned unexpected turn status: {status or 'missing'}"
                        ),
                    )
                break
            if terminal is None:
                terminal = InvocationResult(
                    invocation_id=self.request.invocation_id,
                    status=InvocationStatus.RECONCILIATION_REQUIRED,
                    error_code="agent.codex_stream_incomplete",
                    error_message="Codex stream ended without a terminal event",
                )
        except asyncio.CancelledError:
            terminal = self._forced_result or InvocationResult(
                invocation_id=self.request.invocation_id,
                status=InvocationStatus.RECONCILIATION_REQUIRED,
                error_code="agent.codex_stream_uncertain",
                error_message="Codex stream consumer stopped before terminal state",
            )
        except Exception as exc:
            terminal = InvocationResult(
                invocation_id=self.request.invocation_id,
                status=InvocationStatus.RECONCILIATION_REQUIRED,
                error_code="agent.codex_stream_uncertain",
                error_message=str(exc),
            )
        finally:
            if terminal is None:
                terminal = InvocationResult(
                    invocation_id=self.request.invocation_id,
                    status=InvocationStatus.RECONCILIATION_REQUIRED,
                    error_code="agent.codex_stream_uncertain",
                    error_message="Codex stream consumer stopped before terminal state",
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
                        error_code="agent.codex_cleanup_unknown",
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

    def _completed_result(self, text: str | None) -> InvocationResult:
        if text is None or not text.strip():
            return InvocationResult(
                invocation_id=self.request.invocation_id,
                status=InvocationStatus.FAILED,
                error_code="agent.codex_output_missing",
                error_message="Codex completed without a final agent message",
            )
        if self.request.output_schema is None:
            return InvocationResult(
                invocation_id=self.request.invocation_id,
                status=InvocationStatus.SUCCEEDED,
                output=text,
            )
        try:
            output = json.loads(text)
        except json.JSONDecodeError:
            return InvocationResult(
                invocation_id=self.request.invocation_id,
                status=InvocationStatus.FAILED,
                error_code="agent.codex_output_invalid",
                error_message="Codex final response is not valid JSON",
            )
        if not _is_json_value(output) or not matches_json_schema(
            output, self.request.output_schema
        ):
            return InvocationResult(
                invocation_id=self.request.invocation_id,
                status=InvocationStatus.FAILED,
                error_code="agent.output_contract_violated",
                error_message="Codex output does not satisfy output_schema",
            )
        return InvocationResult(
            invocation_id=self.request.invocation_id,
            status=InvocationStatus.SUCCEEDED,
            output=cast(JsonValue, output),
        )

    async def _emit(self, event_type: str, payload: JsonObject) -> None:
        self._sequence += 1
        await self._events.put(
            InvocationEvent(
                invocation_id=self.request.invocation_id,
                sequence=self._sequence,
                status=InvocationStatus.RUNNING,
                payload={"type": event_type, **payload},
            )
        )

    def _finish(self, result: InvocationResult) -> None:
        if self._result.done():
            return
        self._terminal_reconcile = _reconcile_from_result(
            result, self.session_id, self.turn_id, self._sequence
        )
        self._result.set_result(result)
        self._events.put_nowait(None)


def _reconcile_from_result(
    result: InvocationResult, session_id: str, turn_id: str, sequence: int
) -> ReconcileResult:
    mapping = {
        InvocationStatus.SUCCEEDED: ReconcileStatus.SUCCEEDED,
        InvocationStatus.FAILED: ReconcileStatus.FAILED,
        InvocationStatus.CANCELLED: ReconcileStatus.CANCELLED,
        InvocationStatus.RECONCILIATION_REQUIRED: ReconcileStatus.UNREACHABLE,
    }
    return ReconcileResult(
        mapping[result.status],
        provider_operation_id=turn_id,
        provider_session_id=session_id,
        provider_turn_id=turn_id,
        last_sequence=sequence,
        output=result.output,
        error_code=result.error_code,
        error_message=result.error_message,
    )


def _combine_errors(*errors: str | None) -> str | None:
    values = tuple(error for error in errors if error)
    return "; ".join(values) if values else None


def _as_mapping(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        raw = cast(dict[object, object], value)
        return {str(key): item for key, item in raw.items()}
    model_dump = cast(Callable[..., object] | None, getattr(value, "model_dump", None))
    if callable(model_dump):
        dumped = model_dump(mode="json")
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


def _read_effort(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return _read_string(value, "reasoning_effort", "reasoningEffort", "effort")


def _effort_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    results: list[str] = []
    for item in cast(list[object], value):
        effort = _read_effort(item)
        if effort is not None:
            results.append(effort)
    return tuple(results)


def _turn_error_message(turn: dict[str, object]) -> str | None:
    error = turn.get("error") or turn.get("turnError")
    return _read_string(error, "message", "additional_details", "additionalDetails")


def _validate_codex_schema(schema: JsonObject | None) -> None:
    if schema is None:
        return
    schema_type = schema.get("type")
    if schema_type == "object" and schema.get("additionalProperties") is not False:
        raise ProviderExecutionError(
            "agent.output_schema_invalid",
            "Codex object schemas must set additionalProperties to false",
        )
    properties = schema.get("properties")
    if isinstance(properties, dict):
        raw_properties = cast(dict[object, object], properties)
        for child in raw_properties.values():
            if isinstance(child, dict):
                _validate_codex_schema(cast(JsonObject, child))
    items = schema.get("items")
    if isinstance(items, dict):
        _validate_codex_schema(cast(JsonObject, items))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, bool | int | float | str):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in cast(list[object], value))
    if isinstance(value, dict):
        raw = cast(dict[object, object], value)
        return all(isinstance(key, str) and _is_json_value(item) for key, item in raw.items())
    return False

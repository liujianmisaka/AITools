from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from misaka_agent_capability import AGENT_CAPABILITY_ID, AGENT_OPERATION_INVOKE
from misaka_codex_provider import CodexAgentModule, CodexAgentProvider, CodexProviderConfig
from misaka_codex_provider.native import (
    NativeClient,
    NativeNotification,
    NativeThread,
    NativeTurn,
)
from misaka_invocation_contracts import (
    CompletionBoundary,
    InvocationRequest,
    InvocationStatus,
    ReconcileStatus,
    SessionRef,
)
from misaka_invocation_runtime import (
    InvocationRuntime,
    InvocationRuntimeModule,
    ProviderExecutionError,
)
from misaka_kernel import Host
from misaka_kernel_contracts import JsonObject
from misaka_session_capability import (
    MemorySessionStore,
    MemorySessionStoreModule,
    SessionLease,
    SessionRecord,
)

OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
    "additionalProperties": False,
}


class _RecordingSessionStore(MemorySessionStore):
    def __init__(self) -> None:
        super().__init__()
        self.renewed = asyncio.Event()

    async def renew(self, lease: SessionLease, *, ttl_seconds: float) -> SessionLease:
        renewed = await super().renew(lease, ttl_seconds=ttl_seconds)
        self.renewed.set()
        return renewed


class _FailingSessionStore(MemorySessionStore):
    async def ensure(self, session: SessionRef) -> SessionRecord:
        del session
        raise RuntimeError("session store unavailable")


@dataclass(slots=True)
class _Notification:
    method: str
    payload: object


@dataclass(slots=True)
class _Turn:
    notifications: tuple[_Notification, ...]
    id: str = "turn-1"
    wait_for_interrupt: bool = False
    interrupt_error: Exception | None = None
    interrupted: asyncio.Event = field(default_factory=asyncio.Event)

    async def stream(self) -> AsyncIterator[NativeNotification]:
        if self.wait_for_interrupt:
            await self.interrupted.wait()
        for notification in self.notifications:
            await asyncio.sleep(0)
            yield notification

    async def interrupt(self) -> object:
        if self.interrupt_error is not None:
            raise self.interrupt_error
        self.interrupted.set()
        return {"requested": True}


@dataclass(slots=True)
class _Thread:
    turn_handle: _Turn
    id: str = "thread-1"
    turn_calls: list[dict[str, object]] = field(default_factory=list)
    turn_error: Exception | None = None

    async def turn(
        self,
        input: str,
        *,
        approval_mode: object,
        cwd: str,
        effort: object,
        model: str,
        output_schema: JsonObject | None,
        sandbox: object,
    ) -> NativeTurn:
        if self.turn_error is not None:
            raise self.turn_error
        self.turn_calls.append(
            {
                "input": input,
                "approval_mode": approval_mode,
                "cwd": cwd,
                "effort": effort,
                "model": model,
                "output_schema": output_schema,
                "sandbox": sandbox,
            }
        )
        return self.turn_handle


@dataclass(slots=True)
class _Client:
    thread: _Thread
    model_response: object = field(default_factory=lambda: {"data": [], "next_cursor": None})
    entered: bool = False
    closed: bool = False
    start_calls: list[dict[str, object]] = field(default_factory=list)
    resume_calls: list[dict[str, object]] = field(default_factory=list)
    start_gate: asyncio.Event | None = None
    exit_error: Exception | None = None

    async def __aenter__(self) -> NativeClient:
        self.entered = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exc_type, exc, traceback
        self.closed = True
        if self.exit_error is not None:
            raise self.exit_error

    async def thread_start(
        self,
        *,
        approval_mode: object,
        cwd: str,
        ephemeral: bool,
        model: str,
        sandbox: object,
    ) -> NativeThread:
        if self.start_gate is not None:
            await self.start_gate.wait()
        self.start_calls.append(
            {
                "approval_mode": approval_mode,
                "cwd": cwd,
                "ephemeral": ephemeral,
                "model": model,
                "sandbox": sandbox,
            }
        )
        return self.thread

    async def thread_resume(
        self,
        thread_id: str,
        *,
        approval_mode: object,
        cwd: str,
        model: str,
        sandbox: object,
    ) -> NativeThread:
        self.resume_calls.append(
            {
                "thread_id": thread_id,
                "approval_mode": approval_mode,
                "cwd": cwd,
                "model": model,
                "sandbox": sandbox,
            }
        )
        return self.thread

    async def models(self, *, include_hidden: bool = False) -> object:
        del include_hidden
        return self.model_response


class _Sdk:
    def __init__(self, clients: list[_Client]) -> None:
        self.clients = clients
        self.creations = 0

    def create_client(self) -> NativeClient:
        client = self.clients[self.creations]
        self.creations += 1
        return client

    def approval_deny_all(self) -> object:
        return "deny_all"

    def sandbox(self, value: str) -> object:
        return value

    def effort(self, value: str) -> object:
        return value


def _request(
    invocation_id: str,
    cwd: Path,
    *,
    model: str | None = "pixel/gpt-5.6-luna",
    effort: str | None = "high",
    session_ref: SessionRef | None = None,
    output_schema: JsonObject | None = OUTPUT_SCHEMA,
) -> InvocationRequest:
    return InvocationRequest(
        invocation_id=invocation_id,
        capability_id=AGENT_CAPABILITY_ID,
        operation=AGENT_OPERATION_INVOKE,
        input={"prompt": "Return JSON", "cwd": str(cwd), "sandbox": "read_only"},
        idempotency_key=f"key-{invocation_id}",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        session_ref=session_ref,
        output_schema=output_schema,
        policy_context={"network_policy": "deny"},
        model=model,
        effort=effort,
    )


def _provider(tmp_path: Path, client: _Client) -> tuple[CodexAgentProvider, _Sdk]:
    sdk = _Sdk([client])
    provider = CodexAgentProvider(
        CodexProviderConfig(
            network_deny_enforced=True,
        ),
        sdk=sdk,
        session_store=MemorySessionStore(),
    )
    return provider, sdk


@pytest.mark.asyncio
async def test_describe_is_static_and_does_not_start_codex(tmp_path: Path) -> None:
    client = _Client(_Thread(_Turn(())))
    provider, sdk = _provider(tmp_path, client)

    descriptor = await provider.describe()

    assert descriptor.capability_id == AGENT_CAPABILITY_ID
    assert sdk.creations == 0


@pytest.mark.asyncio
async def test_codex_execution_requires_profile_bound_session_store(tmp_path: Path) -> None:
    sdk = _Sdk([_Client(_Thread(_Turn(())))])
    provider = CodexAgentProvider(
        CodexProviderConfig(
            network_deny_enforced=True,
        ),
        sdk=sdk,
    )

    with pytest.raises(ProviderExecutionError) as raised:
        await provider.start(_request("inv-session-store-unbound", tmp_path))

    assert raised.value.code == "agent.session_store_unbound"
    assert sdk.creations == 0


@pytest.mark.asyncio
async def test_codex_session_store_failure_precedes_provider_side_effect(tmp_path: Path) -> None:
    sdk = _Sdk([_Client(_Thread(_Turn(())))])
    provider = CodexAgentProvider(
        CodexProviderConfig(
            network_deny_enforced=True,
        ),
        sdk=sdk,
        session_store=_FailingSessionStore(),
    )

    with pytest.raises(ProviderExecutionError) as raised:
        await provider.start(
            _request(
                "inv-session-store-failure",
                tmp_path,
                session_ref=SessionRef("codex", "existing-thread"),
            )
        )

    assert raised.value.code == "agent.session_lease_unavailable"
    assert raised.value.reconciliation_required is False
    assert sdk.creations == 0


@pytest.mark.asyncio
async def test_codex_provider_passes_explicit_selection_and_returns_json(tmp_path: Path) -> None:
    notifications = (
        _Notification(
            "item/completed",
            {
                "item": {
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": '{"answer":"ok"}',
                }
            },
        ),
        _Notification("turn/completed", {"turn": {"status": "completed"}}),
    )
    turn = _Turn(notifications)
    thread = _Thread(turn)
    client = _Client(thread)
    provider, _ = _provider(tmp_path, client)
    runtime = InvocationRuntime()
    await runtime.register_provider("codex", provider)

    handle = await runtime.submit(_request("inv-1", tmp_path), provider_id="codex")
    result = await handle.wait()
    snapshot = await handle.snapshot()

    assert result.status is InvocationStatus.SUCCEEDED
    assert result.output == {"answer": "ok"}
    assert thread.turn_calls[0]["model"] == "pixel/gpt-5.6-luna"
    assert thread.turn_calls[0]["effort"] == "high"
    assert client.start_calls[0]["ephemeral"] is False
    assert client.closed
    assert any(event.payload.get("type") == "agent.message.completed" for event in snapshot.events)
    await runtime.stop()


@pytest.mark.asyncio
async def test_prepare_session_does_not_start_a_turn_and_close_is_idempotent(
    tmp_path: Path,
) -> None:
    client = _Client(_Thread(_Turn(())))
    provider, _ = _provider(tmp_path, client)

    prepared = await provider.prepare_session(_request("inv-prepare", tmp_path))

    assert prepared.session_id == "thread-1"
    assert client.thread.turn_calls == []
    assert not client.closed

    assert await prepared.close() is None
    assert await prepared.close() is None
    assert client.closed


@pytest.mark.asyncio
async def test_start_turn_is_the_only_operation_that_starts_a_turn(tmp_path: Path) -> None:
    notifications = (
        _Notification(
            "item/completed",
            {
                "item": {
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": '{"answer":"ok"}',
                }
            },
        ),
        _Notification("turn/completed", {"turn": {"status": "completed"}}),
    )
    thread = _Thread(_Turn(notifications))
    provider, _ = _provider(tmp_path, _Client(thread))

    prepared = await provider.prepare_session(_request("inv-start-turn", tmp_path))
    assert thread.turn_calls == []

    handle = await provider.start_turn(prepared)
    assert len(thread.turn_calls) == 1
    assert (await handle.wait()).status is InvocationStatus.SUCCEEDED

    with pytest.raises(ProviderExecutionError) as raised:
        await provider.start_turn(prepared)
    assert raised.value.code == "agent.codex_prepared_session_handed_off"


@pytest.mark.asyncio
async def test_start_turn_failure_releases_prepared_session_resources(tmp_path: Path) -> None:
    first_client = _Client(_Thread(_Turn(()), turn_error=RuntimeError("turn failed")))
    second_client = _Client(
        _Thread(
            _Turn(
                (_Notification("turn/completed", {"turn": {"status": "interrupted"}}),),
                wait_for_interrupt=True,
            ),
        )
    )
    sdk = _Sdk([first_client, second_client])
    provider = CodexAgentProvider(
        CodexProviderConfig(
            network_deny_enforced=True,
        ),
        sdk=sdk,
        session_store=MemorySessionStore(),
    )
    session = SessionRef("codex", "thread-1")

    prepared = await provider.prepare_session(
        _request("inv-turn-failure", tmp_path, session_ref=session)
    )
    with pytest.raises(ProviderExecutionError) as raised:
        await provider.start_turn(prepared)

    assert raised.value.code == "agent.codex_turn_start_unknown"
    assert first_client.closed

    second = await provider.start(_request("inv-turn-retry", tmp_path, session_ref=session))
    await second.cancel("test cleanup")
    assert (await second.wait()).status is InvocationStatus.CANCELLED


@pytest.mark.asyncio
async def test_codex_provider_requires_model_and_effort_before_client_start(tmp_path: Path) -> None:
    client = _Client(_Thread(_Turn(())))
    provider, sdk = _provider(tmp_path, client)
    runtime = InvocationRuntime()
    await runtime.register_provider("codex", provider)

    result = await (
        await runtime.submit(
            _request("inv-selection", tmp_path, model=None),
            provider_id="codex",
        )
    ).wait()

    assert result.status is InvocationStatus.FAILED
    assert result.error_code == "agent.model_selection_required"
    assert sdk.creations == 0
    await runtime.stop()


@pytest.mark.asyncio
async def test_codex_cleanup_failure_does_not_replace_confirmed_terminal_result(
    tmp_path: Path,
) -> None:
    notifications = (
        _Notification(
            "item/completed",
            {
                "item": {
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": '{"answer":"ok"}',
                }
            },
        ),
        _Notification("turn/completed", {"turn": {"status": "completed"}}),
    )
    client = _Client(
        _Thread(_Turn(notifications)),
        exit_error=RuntimeError("close failed"),
    )
    provider, _ = _provider(tmp_path, client)
    runtime = InvocationRuntime()
    await runtime.register_provider("codex", provider)

    result = await (
        await runtime.submit(_request("inv-cleanup-terminal", tmp_path), provider_id="codex")
    ).wait()

    assert result.status is InvocationStatus.SUCCEEDED
    assert result.output == {"answer": "ok"}
    await runtime.stop()


@pytest.mark.asyncio
async def test_codex_provider_preserves_upstream_turn_error(tmp_path: Path) -> None:
    client = _Client(
        _Thread(
            _Turn(
                (
                    _Notification(
                        "turn/completed",
                        {
                            "turn": {
                                "status": "failed",
                                "error": {
                                    "message": (
                                        "exceeded retry limit, last status: 429 Too Many Requests"
                                    )
                                },
                            }
                        },
                    ),
                )
            )
        )
    )
    provider, _ = _provider(tmp_path, client)
    runtime = InvocationRuntime()
    await runtime.register_provider("codex", provider)

    result = await (await runtime.submit(_request("inv-429", tmp_path), provider_id="codex")).wait()

    assert result.status is InvocationStatus.FAILED
    assert result.error_code == "agent.codex_turn_failed"
    assert result.error_message is not None and "429 Too Many Requests" in result.error_message
    await runtime.stop()


@pytest.mark.asyncio
async def test_codex_cancel_waits_for_interrupted_terminal(tmp_path: Path) -> None:
    turn = _Turn(
        (
            _Notification(
                "turn/completed",
                {"turn": {"status": "interrupted"}},
            ),
        ),
        wait_for_interrupt=True,
    )
    provider, _ = _provider(tmp_path, _Client(_Thread(turn)))
    runtime = InvocationRuntime()
    await runtime.register_provider("codex", provider)
    handle = await runtime.submit(_request("inv-cancel", tmp_path), provider_id="codex")
    await asyncio.sleep(0)

    await handle.cancel("user cancelled")
    result = await handle.wait()

    assert turn.interrupted.is_set()
    assert result.status is InvocationStatus.CANCELLED
    await runtime.stop()


@pytest.mark.asyncio
async def test_incomplete_codex_stream_requires_reconciliation(tmp_path: Path) -> None:
    provider, _ = _provider(tmp_path, _Client(_Thread(_Turn(()))))
    runtime = InvocationRuntime()
    await runtime.register_provider("codex", provider)

    result = await (
        await runtime.submit(_request("inv-incomplete", tmp_path), provider_id="codex")
    ).wait()

    assert result.status is InvocationStatus.RECONCILIATION_REQUIRED
    assert result.error_code == "agent.codex_stream_incomplete"
    await runtime.stop()


@pytest.mark.asyncio
async def test_same_codex_session_cannot_run_two_turns(tmp_path: Path) -> None:
    first_turn = _Turn(
        (_Notification("turn/completed", {"turn": {"status": "interrupted"}}),),
        wait_for_interrupt=True,
    )
    first_client = _Client(_Thread(first_turn, id="shared-thread"))
    second_client = _Client(_Thread(_Turn(()), id="shared-thread"))
    sdk = _Sdk([first_client, second_client])
    provider = CodexAgentProvider(
        CodexProviderConfig(
            network_deny_enforced=True,
        ),
        sdk=sdk,
        session_store=MemorySessionStore(),
    )
    session = SessionRef("codex", "shared-thread")
    first = await provider.start(_request("inv-first", tmp_path, session_ref=session))

    with pytest.raises(ProviderExecutionError) as raised:
        await provider.start(_request("inv-second", tmp_path, session_ref=session))

    assert raised.value.code == "agent.session_busy"
    await first.cancel("test cleanup")
    assert (await first.wait()).status is InvocationStatus.CANCELLED


@pytest.mark.asyncio
async def test_new_codex_session_lease_conflict_requires_reconciliation(tmp_path: Path) -> None:
    store = MemorySessionStore()
    session = SessionRef("codex", "thread-1")
    await store.ensure(session)
    lease = await store.acquire(
        session,
        "other-owner",
        "other-operation",
    )
    client = _Client(_Thread(_Turn(())))
    sdk = _Sdk([client])
    provider = CodexAgentProvider(
        CodexProviderConfig(
            network_deny_enforced=True,
        ),
        sdk=sdk,
        session_store=store,
    )

    with pytest.raises(ProviderExecutionError) as raised:
        await provider.start(_request("inv-new-session-conflict", tmp_path))

    assert raised.value.code == "agent.session_lease_unavailable"
    assert raised.value.reconciliation_required is True
    assert sdk.creations == 1
    assert client.closed
    await store.release(lease)


@pytest.mark.asyncio
async def test_codex_provider_renews_and_releases_shared_session_lease(
    tmp_path: Path,
) -> None:
    first_turn = _Turn(
        (_Notification("turn/completed", {"turn": {"status": "interrupted"}}),),
        wait_for_interrupt=True,
    )
    session = SessionRef("codex", "leased-thread")
    store = _RecordingSessionStore()
    provider = CodexAgentProvider(
        CodexProviderConfig(
            network_deny_enforced=True,
            rpc_timeout_seconds=0.05,
            session_lease_ttl_seconds=0.06,
            session_lease_renew_interval_seconds=0.01,
        ),
        sdk=_Sdk(
            [
                _Client(_Thread(first_turn, id=session.native_id)),
                _Client(_Thread(_Turn(()), id=session.native_id)),
            ]
        ),
        session_store=store,
    )

    first = await provider.start(_request("inv-lease-first", tmp_path, session_ref=session))
    initial_record = await store.get(session)
    assert initial_record is not None
    assert initial_record.lease is not None
    initial_expiry = initial_record.lease.expires_at

    store.renewed.clear()
    await asyncio.wait_for(store.renewed.wait(), timeout=0.5)
    renewed_record = await store.get(session)
    assert renewed_record is not None
    assert renewed_record.lease is not None
    assert renewed_record.lease.expires_at > initial_expiry

    with pytest.raises(ProviderExecutionError) as raised:
        await provider.start(_request("inv-lease-second", tmp_path, session_ref=session))
    assert raised.value.code == "agent.session_busy"

    await first.cancel("test cleanup")
    assert (await first.wait()).status is InvocationStatus.CANCELLED
    released = await store.get(session)
    assert released is not None
    assert released.lease is None


@pytest.mark.asyncio
async def test_codex_provider_fails_closed_when_session_lease_is_transferred(
    tmp_path: Path,
) -> None:
    turn = _Turn(
        (_Notification("turn/completed", {"turn": {"status": "interrupted"}}),),
        wait_for_interrupt=True,
    )
    session = SessionRef("codex", "fenced-thread")
    store = MemorySessionStore()
    provider = CodexAgentProvider(
        CodexProviderConfig(
            network_deny_enforced=True,
            rpc_timeout_seconds=0.05,
            session_lease_ttl_seconds=0.2,
            session_lease_renew_interval_seconds=0.01,
        ),
        sdk=_Sdk([_Client(_Thread(turn, id=session.native_id))]),
        session_store=store,
    )
    handle = await provider.start(_request("inv-lease-fenced", tmp_path, session_ref=session))
    record = await store.get(session)
    assert record is not None
    assert record.lease is not None

    transferred = await store.transfer(
        record.lease,
        "recovery-worker",
        "recovery-operation",
        ttl_seconds=0.2,
    )
    result = await asyncio.wait_for(handle.wait(), timeout=0.5)

    assert result.status is InvocationStatus.RECONCILIATION_REQUIRED
    assert result.error_code == "agent.session_lease_lost"
    assert turn.interrupted.is_set()
    await store.release(transferred)


@pytest.mark.asyncio
async def test_codex_module_binds_profile_session_store() -> None:
    runtime_module = InvocationRuntimeModule()
    session_module = MemorySessionStoreModule()
    codex_module = CodexAgentModule()
    host = Host()
    host.add_module(runtime_module)
    host.add_module(session_module)
    host.add_module(codex_module)

    await host.start()
    try:
        assert codex_module.provider.session_store is session_module.store
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_resume_rejects_changed_native_session_identity(tmp_path: Path) -> None:
    client = _Client(_Thread(_Turn(()), id="unexpected-thread"))
    provider, _ = _provider(tmp_path, client)

    with pytest.raises(ProviderExecutionError) as raised:
        await provider.start(
            _request(
                "inv-resume-mismatch",
                tmp_path,
                session_ref=SessionRef("codex", "expected-thread"),
            )
        )

    assert raised.value.code == "agent.codex_session_identity_changed"
    assert client.closed


@pytest.mark.asyncio
async def test_codex_catalog_is_normalized_and_rejects_partial_pages(tmp_path: Path) -> None:
    client = _Client(
        _Thread(_Turn(())),
        model_response={
            "data": [
                {
                    "id": "pixel/gpt-5.6-luna",
                    "display_name": "GPT-5.6 Luna",
                    "description": "Local configured model",
                    "supported_reasoning_efforts": [{"reasoning_effort": "high"}],
                }
            ],
            "next_cursor": None,
        },
    )
    provider, _ = _provider(tmp_path, client)

    catalog = await provider.models()

    assert catalog.models[0].id == "pixel/gpt-5.6-luna"
    assert catalog.models[0].supported_efforts == ("high",)
    assert client.closed

    partial_client = _Client(
        _Thread(_Turn(())),
        model_response={"data": [], "next_cursor": "next-page"},
    )
    partial_provider, _ = _provider(tmp_path, partial_client)
    with pytest.raises(ProviderExecutionError) as raised:
        await partial_provider.models()
    assert raised.value.code == "agent.codex_catalog_pagination"


@pytest.mark.asyncio
async def test_codex_reconcile_exposes_native_identity(tmp_path: Path) -> None:
    turn = _Turn(
        (_Notification("turn/completed", {"turn": {"status": "interrupted"}}),),
        wait_for_interrupt=True,
    )
    provider, _ = _provider(tmp_path, _Client(_Thread(turn)))
    handle = await provider.start(_request("inv-reconcile", tmp_path))

    reconciled = await handle.reconcile()

    assert reconciled.status is ReconcileStatus.RUNNING
    assert reconciled.provider_session_id == "thread-1"
    assert reconciled.provider_turn_id == "turn-1"
    assert not reconciled.attachable
    await handle.cancel("test cleanup")
    await handle.wait()


@pytest.mark.asyncio
async def test_codex_schema_requires_strict_object_contract(tmp_path: Path) -> None:
    client = _Client(_Thread(_Turn(())))
    provider, sdk = _provider(tmp_path, client)
    invalid_schema: JsonObject = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
    }

    with pytest.raises(ProviderExecutionError) as raised:
        await provider.start(_request("inv-schema", tmp_path, output_schema=invalid_schema))

    assert raised.value.code == "agent.output_schema_invalid"
    assert sdk.creations == 0


@pytest.mark.asyncio
async def test_codex_execution_requires_an_absolute_cwd() -> None:
    sdk = _Sdk([_Client(_Thread(_Turn(())))])
    provider = CodexAgentProvider(
        CodexProviderConfig(network_deny_enforced=True),
        sdk=sdk,
        session_store=MemorySessionStore(),
    )

    with pytest.raises(ProviderExecutionError) as raised:
        await provider.start(_request("inv-relative-cwd", Path("relative")))

    assert raised.value.code == "agent.cwd_invalid"
    assert sdk.creations == 0


@pytest.mark.asyncio
async def test_codex_thread_start_timeout_closes_client(tmp_path: Path) -> None:
    client = _Client(_Thread(_Turn(())), start_gate=asyncio.Event())
    provider = CodexAgentProvider(
        CodexProviderConfig(
            network_deny_enforced=True,
            rpc_timeout_seconds=0.01,
        ),
        sdk=_Sdk([client]),
        session_store=MemorySessionStore(),
    )

    with pytest.raises(ProviderExecutionError) as raised:
        await provider.start(_request("inv-thread-timeout", tmp_path))

    assert raised.value.code == "agent.codex_thread_timeout"
    assert client.closed


@pytest.mark.asyncio
async def test_codex_ephemeral_session_is_explicit(tmp_path: Path) -> None:
    notifications = (
        _Notification(
            "item/completed",
            {
                "item": {
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": '{"answer":"ok"}',
                }
            },
        ),
        _Notification("turn/completed", {"turn": {"status": "completed"}}),
    )
    client = _Client(_Thread(_Turn(notifications)))
    provider = CodexAgentProvider(
        CodexProviderConfig(
            network_deny_enforced=True,
            new_sessions_ephemeral=True,
        ),
        sdk=_Sdk([client]),
        session_store=MemorySessionStore(),
    )

    result = await (await provider.start(_request("inv-ephemeral", tmp_path))).wait()

    assert result.status is InvocationStatus.SUCCEEDED
    assert client.start_calls[0]["ephemeral"] is True

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
from misaka_agent_capability import AGENT_CAPABILITY_ID, AGENT_OPERATION_INVOKE
from misaka_claude_provider import ClaudeAgentProvider, ClaudeAgentSdk, ClaudeProviderConfig
from misaka_claude_provider.native import NativeClaudeClient, NativeClaudeOptions, NativeClaudeSdk
from misaka_delegation_contracts import (
    DelegationMode,
    DelegationRequest,
    DelegationStatus,
    MessageDispatchMode,
    MessageDispatchRequest,
    MessageDispatchStatus,
    MessageDispatchStrategy,
)
from misaka_delegation_runtime import DelegationRuntime
from misaka_interaction_contracts import MessageType, PrincipalKind, PrincipalRef, ScopeRef
from misaka_interaction_memory import MemoryInteractionChannelStore
from misaka_invocation_contracts import (
    CapabilityFeature,
    CompletionBoundary,
    InvocationRequest,
    InvocationStatus,
    ReconcileStatus,
    SessionRef,
)
from misaka_invocation_runtime import InvocationRuntime, ProviderExecutionError
from misaka_kernel_contracts import JsonObject
from misaka_persistence_jsonl import JsonlEventLog, JsonlSessionLog
from misaka_session_capability import MemorySessionStore

OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
    "additionalProperties": False,
}


@dataclass(slots=True)
class SystemMessage:
    subtype: str
    data: dict[str, object]


@dataclass(slots=True)
class TextBlock:
    text: str


@dataclass(slots=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, object]


@dataclass(slots=True)
class ToolResultBlock:
    tool_use_id: str
    content: str | list[dict[str, object]] | None = None
    is_error: bool | None = None


@dataclass(slots=True)
class UserMessage:
    content: list[object]


@dataclass(slots=True)
class AssistantMessage:
    content: list[object]
    model: str = "claude-test"
    session_id: str | None = None
    message_id: str | None = None
    parent_tool_use_id: str | None = None


@dataclass(slots=True)
class StreamEvent:
    event: dict[str, object]
    session_id: str


@dataclass(slots=True)
class ResultMessage:
    subtype: str = "success"
    duration_ms: int = 1
    duration_api_ms: int = 1
    is_error: bool = False
    num_turns: int = 1
    session_id: str = ""
    result: str | None = None
    structured_output: object = None
    errors: list[str] | None = None
    terminal_reason: str | None = None
    uuid: str | None = None


@dataclass(slots=True)
class TaskStartedMessage:
    task_id: str
    description: str
    tool_use_id: str | None = None


@dataclass(slots=True)
class TaskProgressMessage:
    task_id: str
    description: str
    last_tool_name: str | None = None
    tool_use_id: str | None = None


@dataclass(slots=True)
class TaskNotificationMessage:
    task_id: str
    status: str
    summary: str
    tool_use_id: str | None = None


@dataclass(slots=True)
class _Client:
    messages: tuple[object, ...]
    wait_for_interrupt: bool = False
    wait_for_queries: int = 0
    connect_error: Exception | None = None
    query_error: Exception | None = None
    options: NativeClaudeOptions | None = None
    connected: bool = False
    disconnected: bool = False
    queried: list[str] = field(default_factory=list)
    interrupted: asyncio.Event = field(default_factory=asyncio.Event)
    queries_ready: asyncio.Event = field(default_factory=asyncio.Event)

    async def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    async def query(self, prompt: str) -> None:
        if self.query_error is not None:
            raise self.query_error
        self.queried.append(prompt)
        if len(self.queried) >= self.wait_for_queries:
            self.queries_ready.set()

    async def receive_messages(self) -> AsyncIterator[object]:
        if self.wait_for_interrupt:
            await self.interrupted.wait()
        if len(self.queried) < self.wait_for_queries:
            await self.queries_ready.wait()
        for message in self.messages:
            await asyncio.sleep(0)
            yield message

    async def interrupt(self) -> None:
        self.interrupted.set()

    async def disconnect(self) -> None:
        self.disconnected = True


class _Sdk(NativeClaudeSdk):
    def __init__(self, client: _Client) -> None:
        self.client = client
        self.options: NativeClaudeOptions | None = None
        self.creations = 0

    def create_client(self, options: NativeClaudeOptions) -> NativeClaudeClient:
        self.options = options
        self.client.options = options
        self.creations += 1
        return self.client


def _request(
    invocation_id: str,
    cwd: Path,
    *,
    model: str | None = "claude-sonnet-4-5",
    effort: str | None = "high",
    sandbox: str = "read_only",
    session_ref: SessionRef | None = None,
    output_schema: JsonObject | None = OUTPUT_SCHEMA,
) -> InvocationRequest:
    return InvocationRequest(
        invocation_id=invocation_id,
        capability_id=AGENT_CAPABILITY_ID,
        operation=AGENT_OPERATION_INVOKE,
        input={"prompt": "Return JSON", "cwd": str(cwd), "sandbox": sandbox},
        idempotency_key=f"key-{invocation_id}",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        session_ref=session_ref,
        output_schema=output_schema,
        policy_context={"network_policy": "deny"},
        model=model,
        effort=effort,
    )


def _provider(client: _Client) -> tuple[ClaudeAgentProvider, _Sdk]:
    sdk = _Sdk(client)
    provider = ClaudeAgentProvider(
        ClaudeProviderConfig(
            model_ids=("claude-sonnet-4-5",),
            network_deny_enforced=True,
        ),
        sdk=sdk,
        session_store=MemorySessionStore(),
    )
    return provider, sdk


def test_claude_sdk_adapter_maps_isolation_options_without_starting_cli(tmp_path: Path) -> None:
    from claude_agent_sdk import ClaudeSDKClient

    sdk = ClaudeAgentSdk()
    client = sdk.create_client(
        NativeClaudeOptions(
            model="claude-sonnet-4-5",
            effort="high",
            cwd=str(tmp_path),
            session_id="00000000-0000-0000-0000-000000000001",
            tools=("Read", "Glob", "Grep"),
            output_format={"type": "json_schema", "schema": OUTPUT_SCHEMA},
        )
    )

    options = cast(ClaudeSDKClient, client).options
    assert options.model == "claude-sonnet-4-5"
    assert options.effort == "high"
    assert options.cwd == str(tmp_path)
    assert options.setting_sources == []
    assert options.strict_mcp_config
    assert options.include_partial_messages
    assert options.forward_subagent_text
    assert options.tools == ["Read", "Glob", "Grep"]


@pytest.mark.asyncio
async def test_claude_connect_failure_disconnects_created_client(tmp_path: Path) -> None:
    client = _Client((), connect_error=RuntimeError("connect failed"))
    provider, _ = _provider(client)

    with pytest.raises(ProviderExecutionError) as raised:
        await provider.start(_request("inv-connect-failed", tmp_path))

    assert raised.value.code == "agent.claude_prepare_unknown"
    assert client.disconnected


@pytest.mark.asyncio
async def test_claude_aborted_result_is_cancelled(tmp_path: Path) -> None:
    client = _Client(
        (ResultMessage(result=None, terminal_reason="aborted_streaming"),),
    )
    provider, _ = _provider(client)

    result = await (
        await provider.start(_request("inv-aborted", tmp_path, output_schema=None))
    ).wait()

    assert result.status is InvocationStatus.CANCELLED


@pytest.mark.asyncio
async def test_claude_error_result_is_failed(tmp_path: Path) -> None:
    client = _Client(
        (
            ResultMessage(
                is_error=True,
                result="API failed",
                errors=["rate limited"],
                terminal_reason="api_error",
            ),
        )
    )
    provider, _ = _provider(client)

    result = await (
        await provider.start(_request("inv-error", tmp_path, output_schema=None))
    ).wait()

    assert result.status is InvocationStatus.FAILED
    assert result.error_code == "agent.claude_turn_failed"
    assert result.error_message == "rate limited"


@pytest.mark.asyncio
async def test_describe_and_catalog_do_not_start_claude(tmp_path: Path) -> None:
    provider, sdk = _provider(_Client(()))

    descriptor = await provider.describe()
    catalog = await provider.model_catalog()

    assert descriptor.capability_id == AGENT_CAPABILITY_ID
    assert catalog[0].model_id == "claude-sonnet-4-5"
    assert sdk.creations == 0


@pytest.mark.asyncio
async def test_claude_requires_model_and_effort_before_sdk_side_effect(tmp_path: Path) -> None:
    provider, sdk = _provider(_Client(()))

    with pytest.raises(ProviderExecutionError) as raised:
        await provider.start(_request("inv-selection", tmp_path, model=None))

    assert raised.value.code == "agent.model_selection_required"
    assert sdk.creations == 0


@pytest.mark.asyncio
async def test_claude_maps_options_and_structured_output(tmp_path: Path) -> None:
    client = _Client(
        (
            SystemMessage("init", {}),
            StreamEvent(
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ok"}},
                "",
            ),
            AssistantMessage([TextBlock('{"answer":"ok"}')]),
            ResultMessage(result='{"answer":"ok"}', session_id=""),
        )
    )
    provider, sdk = _provider(client)
    runtime = InvocationRuntime()
    await runtime.register_provider("claude", provider)

    handle = await runtime.submit(_request("inv-1", tmp_path), provider_id="claude")
    result = await handle.wait()
    snapshot = await handle.snapshot()

    assert result.status is InvocationStatus.SUCCEEDED
    assert result.output == {"answer": "ok"}
    assert sdk.options is not None
    assert sdk.options.model == "claude-sonnet-4-5"
    assert sdk.options.effort == "high"
    assert sdk.options.cwd == str(tmp_path)
    assert sdk.options.output_format == {"type": "json_schema", "schema": OUTPUT_SCHEMA}
    assert sdk.options.tools == ("Read", "Glob", "Grep")
    assert client.queried == ["Return JSON"]
    assert client.disconnected
    assert any(
        event.payload.get("type") == "agent.message.delta" for event in snapshot.events
    )
    await runtime.stop()


@pytest.mark.asyncio
async def test_claude_projects_rich_realtime_events(tmp_path: Path) -> None:
    client = _Client(
        (
            StreamEvent(
                {"type": "message_start", "message": {"id": "message-1"}},
                "",
            ),
            StreamEvent(
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
                "",
            ),
            StreamEvent(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "working"},
                },
                "",
            ),
            StreamEvent(
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "tool_use", "id": "tool-1", "name": "Read"},
                },
                "",
            ),
            AssistantMessage(
                [TextBlock("done"), ToolUseBlock("tool-1", "Read", {"file_path": "README.md"})],
                message_id="message-1",
            ),
            UserMessage([ToolResultBlock("tool-1", "README contents")]),
            TaskStartedMessage("task-1", "Inspect dependency", "tool-1"),
            TaskProgressMessage("task-1", "Reading files", "Grep", "tool-1"),
            TaskNotificationMessage("task-1", "completed", "Dependency inspected", "tool-1"),
            ResultMessage(result="done", uuid="turn-1"),
        )
    )
    provider, _ = _provider(client)

    handle = await provider.start(_request("inv-rich-stream", tmp_path, output_schema=None))
    events = [event async for event in handle.events()]
    result = await handle.wait()

    assert result.status is InvocationStatus.SUCCEEDED
    assert result.output == "done"
    by_type = {str(event.payload["type"]): event.payload for event in events}
    assert by_type["agent.message.delta"]["item_id"] == "message-1:text:0"
    assert by_type["agent.tool.started"]["tool_name"] == "Read"
    assert by_type["agent.tool.completed"]["text"] == "README contents"
    assert by_type["agent.task.started"]["summary"] == "Inspect dependency"
    assert by_type["agent.task.progress"]["tool_name"] == "Grep"
    assert by_type["agent.task.completed"]["status"] == "completed"
    assert by_type["agent.turn.completed"]["provider_operation_id"] == "inv-rich-stream"
    assert by_type["agent.turn.completed"]["turn_id"] == "inv-rich-stream"


@pytest.mark.asyncio
async def test_claude_result_uuid_cannot_replace_persisted_operation_identity(
    tmp_path: Path,
) -> None:
    client = _Client((ResultMessage(result="done", uuid="claude-result-uuid"),))
    provider, _ = _provider(client)
    runtime = InvocationRuntime()
    await runtime.register_provider("claude", provider)

    try:
        handle = await runtime.submit(
            _request("inv-stable-operation", tmp_path, output_schema=None),
            provider_id="claude",
        )
        result = await handle.wait()
        snapshot = await runtime.store.snapshot("inv-stable-operation")

        assert result.status is InvocationStatus.SUCCEEDED
        assert snapshot.provider_execution is not None
        assert snapshot.provider_execution.provider_operation_id == "inv-stable-operation"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_claude_resume_uses_requested_session_identity(tmp_path: Path) -> None:
    session = SessionRef("claude", "session-1")
    client = _Client((ResultMessage(result="done", session_id="session-1"),))
    provider, sdk = _provider(client)

    handle = await provider.start(
        _request("inv-resume", tmp_path, session_ref=session, output_schema=None)
    )
    result = await handle.wait()

    assert result.status is InvocationStatus.SUCCEEDED
    assert sdk.options is not None
    assert sdk.options.resume == "session-1"
    assert sdk.options.session_id is None


@pytest.mark.asyncio
async def test_claude_cancel_interrupts_stream(tmp_path: Path) -> None:
    client = _Client((), wait_for_interrupt=True)
    provider, _ = _provider(client)
    runtime = InvocationRuntime()
    await runtime.register_provider("claude", provider)

    handle = await runtime.submit(_request("inv-cancel", tmp_path), provider_id="claude")
    await asyncio.sleep(0)
    await handle.cancel("user cancelled")
    result = await handle.wait()

    assert client.interrupted.is_set()
    assert result.status is InvocationStatus.RECONCILIATION_REQUIRED
    await runtime.stop()


@pytest.mark.asyncio
async def test_claude_provider_accepts_live_input_on_the_connected_session(
    tmp_path: Path,
) -> None:
    client = _Client(
        (ResultMessage(terminal_reason="interrupted"),),
        wait_for_interrupt=True,
    )
    provider, _ = _provider(client)
    descriptor = await provider.describe()
    handle = await provider.start(_request("inv-live-input", tmp_path))

    await handle.steer({"instruction": "focus on the lease race"})

    assert CapabilityFeature.STEERING in descriptor.features
    assert client.queried == ["Return JSON", "focus on the lease race"]
    await handle.cancel("test cleanup")
    assert (await handle.wait()).status is InvocationStatus.CANCELLED


@pytest.mark.asyncio
async def test_invocation_runtime_forwards_live_input_to_claude(tmp_path: Path) -> None:
    client = _Client(
        (ResultMessage(terminal_reason="interrupted"),),
        wait_for_interrupt=True,
    )
    provider, _ = _provider(client)
    runtime = InvocationRuntime()
    await runtime.register_provider("claude", provider)
    handle = await runtime.submit(
        _request("inv-runtime-live-input", tmp_path),
        provider_id="claude",
    )

    assert handle.supports_control("steer") is True
    await handle.steer({"prompt": "add a regression test"})

    assert client.queried == ["Return JSON", "add a regression test"]
    await handle.cancel("test cleanup")
    assert (await handle.wait()).status is InvocationStatus.CANCELLED
    await runtime.stop()


@pytest.mark.asyncio
async def test_claude_live_input_waits_for_the_appended_response(tmp_path: Path) -> None:
    client = _Client(
        (
            ResultMessage(structured_output={"answer": "initial"}),
            ResultMessage(structured_output={"answer": "after live input"}),
        ),
        wait_for_queries=2,
    )
    provider, _ = _provider(client)
    handle = await provider.start(_request("inv-live-response", tmp_path))

    await handle.steer({"instruction": "revise the answer"})
    result = await handle.wait()

    assert result.status is InvocationStatus.SUCCEEDED
    assert result.output == {"answer": "after live input"}
    assert client.queried == ["Return JSON", "revise the answer"]


@pytest.mark.asyncio
async def test_claude_live_input_failure_does_not_interrupt_the_turn(tmp_path: Path) -> None:
    client = _Client(
        (ResultMessage(terminal_reason="interrupted"),),
        wait_for_interrupt=True,
    )
    provider, _ = _provider(client)
    handle = await provider.start(_request("inv-live-input-failure", tmp_path))
    client.query_error = RuntimeError("live input outcome unknown")

    with pytest.raises(ProviderExecutionError) as raised:
        await handle.steer({"text": "change direction"})

    assert raised.value.code == "agent.claude_steer_unknown"
    assert raised.value.reconciliation_required is True
    assert not client.interrupted.is_set()
    client.query_error = None
    await handle.cancel("test cleanup")
    await handle.wait()


@pytest.mark.asyncio
async def test_delegation_runtime_steers_and_interrupts_claude_in_the_same_session(
    tmp_path: Path,
) -> None:
    first_client = _Client(
        (ResultMessage(terminal_reason="interrupted"),),
        wait_for_interrupt=True,
    )
    second_client = _Client(
        (ResultMessage(structured_output={"answer": "continued"}),),
    )

    class _ClientsSdk(NativeClaudeSdk):
        def __init__(self) -> None:
            self.clients = [first_client, second_client]
            self.options: list[NativeClaudeOptions] = []

        def create_client(self, options: NativeClaudeOptions) -> NativeClaudeClient:
            self.options.append(options)
            client = self.clients[len(self.options) - 1]
            client.options = options
            return client

    sdk = _ClientsSdk()
    provider = ClaudeAgentProvider(
        ClaudeProviderConfig(
            model_ids=("claude-sonnet-4-5",),
            network_deny_enforced=True,
        ),
        sdk=sdk,
        session_store=MemorySessionStore(),
    )
    invocation_runtime = InvocationRuntime()
    await invocation_runtime.register_provider("claude", provider)
    actor = PrincipalRef("parent", PrincipalKind.APPLICATION)
    delegation_runtime = DelegationRuntime(
        invocation_runtime,
        MemoryInteractionChannelStore(),
        session_log=JsonlSessionLog(JsonlEventLog(tmp_path / "claude-sessions.jsonl")),
        composition_id="claude-delegation-test",
    )
    handle = await delegation_runtime.submit(
        DelegationRequest(
            delegation_id="delegation-claude-live-control",
            idempotency_key="delegation-claude-live-control-key",
            initiator=actor,
            controller=actor,
            scope=ScopeRef("scope-1"),
            capability_id=AGENT_CAPABILITY_ID,
            operation=AGENT_OPERATION_INVOKE,
            input={"prompt": "start", "cwd": str(tmp_path), "sandbox": "read_only"},
            provider_id="claude",
            model="claude-sonnet-4-5",
            effort="high",
            output_schema=OUTPUT_SCHEMA,
            constraints={"network_policy": "deny"},
            mode=DelegationMode.CONTINUABLE,
        )
    )
    snapshot = await handle.snapshot()
    session_id = cast(str, snapshot.ref.session_id)
    activation_id = cast(str, snapshot.current_activation_id)

    steered = await handle.dispatch_message(
        MessageDispatchRequest(
            dispatch_id="claude-live-steer",
            delegation_id=handle.delegation_id,
            idempotency_key="claude-live-steer-key",
            message_id="claude-live-steer-message",
            actor=actor,
            session_id=session_id,
            expected_activation_id=activation_id,
            delivery=MessageDispatchMode.APPEND,
            message_type=MessageType.INSTRUCTION,
            payload={"instruction": "focus on the state race"},
        )
    )
    interrupted = await handle.dispatch_message(
        MessageDispatchRequest(
            dispatch_id="claude-live-interrupt",
            delegation_id=handle.delegation_id,
            idempotency_key="claude-live-interrupt-key",
            message_id="claude-live-interrupt-message",
            actor=actor,
            session_id=session_id,
            expected_activation_id=activation_id,
            delivery=MessageDispatchMode.INTERRUPT_CONTINUE,
            message_type=MessageType.INSTRUCTION,
            payload={
                "prompt": "continue in a new turn",
                "cwd": str(tmp_path),
                "sandbox": "read_only",
            },
        )
    )
    report = await handle.wait()

    assert steered.status is MessageDispatchStatus.COMPLETED
    assert steered.applied_strategy is MessageDispatchStrategy.STEERED_CURRENT_ACTIVATION
    assert first_client.queried == ["start", "focus on the state race"]
    assert interrupted.status is MessageDispatchStatus.COMPLETED
    assert interrupted.applied_strategy is MessageDispatchStrategy.INTERRUPTED_AND_CONTINUED
    assert sdk.options[0].session_id is not None
    assert sdk.options[1].resume == sdk.options[0].session_id
    assert report.status is DelegationStatus.COMPLETED
    assert report.output == {"answer": "continued"}
    await delegation_runtime.stop()
    await invocation_runtime.stop()


@pytest.mark.asyncio
async def test_claude_incomplete_stream_requires_reconciliation(tmp_path: Path) -> None:
    provider, _ = _provider(_Client(()))
    runtime = InvocationRuntime()
    await runtime.register_provider("claude", provider)

    result = await (
        await runtime.submit(_request("inv-incomplete", tmp_path), provider_id="claude")
    ).wait()

    assert result.status is InvocationStatus.RECONCILIATION_REQUIRED
    assert result.error_code == "agent.claude_stream_incomplete"
    await runtime.stop()


@pytest.mark.asyncio
async def test_claude_identity_mismatch_requires_reconciliation(tmp_path: Path) -> None:
    client = _Client((ResultMessage(result="done", session_id="different"),))
    provider, _ = _provider(client)

    result = await (
        await provider.start(_request("inv-mismatch", tmp_path, output_schema=None))
    ).wait()

    assert result.status is InvocationStatus.RECONCILIATION_REQUIRED
    assert result.error_code == "agent.claude_session_identity_changed"


@pytest.mark.asyncio
async def test_claude_network_deny_fails_closed(tmp_path: Path) -> None:
    sdk = _Sdk(_Client(()))
    provider = ClaudeAgentProvider(
        ClaudeProviderConfig(model_ids=("claude-sonnet-4-5",), network_deny_enforced=False),
        sdk=sdk,
        session_store=MemorySessionStore(),
    )

    with pytest.raises(ProviderExecutionError) as raised:
        await provider.start(_request("inv-network", tmp_path))

    assert raised.value.code == "agent.network_policy_unenforced"
    assert sdk.creations == 0


@pytest.mark.asyncio
async def test_claude_tool_policy_rejects_paths_outside_workspace(tmp_path: Path) -> None:
    provider, sdk = _provider(_Client(()))
    prepared = await provider.prepare_session(_request("inv-policy", tmp_path))
    assert sdk.options is not None and sdk.options.tool_policy is not None

    assert await sdk.options.tool_policy("Read", {"file_path": str(tmp_path / "a.txt")})
    assert not await sdk.options.tool_policy("Write", {"file_path": str(tmp_path / "a.txt")})
    assert not await sdk.options.tool_policy(
        "Read", {"file_path": str(tmp_path.parent / "outside.txt")}
    )
    await prepared.close()


@pytest.mark.asyncio
async def test_claude_module_binds_profile_session_store() -> None:
    from misaka_claude_provider import ClaudeAgentModule
    from misaka_invocation_runtime import InvocationRuntimeModule
    from misaka_kernel import Host
    from misaka_session_capability import MemorySessionStoreModule

    runtime_module = InvocationRuntimeModule()
    session_module = MemorySessionStoreModule()
    claude_module = ClaudeAgentModule()
    host = Host()
    host.add_module(runtime_module)
    host.add_module(session_module)
    host.add_module(claude_module)

    await host.start()
    try:
        assert claude_module.provider.session_store is session_module.store
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_claude_reconcile_exposes_native_identity(tmp_path: Path) -> None:
    session = SessionRef("claude", "session-reconcile")
    client = _Client((), wait_for_interrupt=True)
    provider, _ = _provider(client)
    handle = await provider.start(
        _request("inv-reconcile", tmp_path, session_ref=session, output_schema=None)
    )

    reconciled = await handle.reconcile()
    assert reconciled.status is ReconcileStatus.RUNNING
    assert reconciled.provider_session_id == "session-reconcile"
    await handle.cancel("cleanup")
    await handle.wait()

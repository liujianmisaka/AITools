from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from misaka_a2a_capability import (
    A2AAgentCard,
    A2ASkill,
    TaskEvent,
    TaskRequest,
    TaskResult,
    TaskSnapshot,
    TaskStatus,
)
from misaka_a2a_provider import A2AInvocationProvider
from misaka_agent_capability import AGENT_CAPABILITY_ID, AGENT_OPERATION_INVOKE
from misaka_codex_provider import CodexAgentProvider, CodexProviderConfig
from misaka_codex_provider.native import (
    NativeClient,
    NativeNotification,
    NativeThread,
    NativeTurn,
)
from misaka_fake_agent import FakeAgentProvider, FakeAgentScenario
from misaka_invocation_contracts import (
    CapabilityFeature,
    CompletionBoundary,
    InvocationRequest,
    InvocationStatus,
    ReconcileStatus,
)
from misaka_invocation_runtime import InvocationProvider
from misaka_kernel_contracts import JsonObject
from misaka_session_capability import MemorySessionStore

_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
    "additionalProperties": False,
}
_COMMON_FEATURES = frozenset(
    {
        CapabilityFeature.STRUCTURED_OUTPUT,
        CapabilityFeature.STREAMING,
        CapabilityFeature.CANCELLATION,
    }
)


@dataclass(frozen=True, slots=True)
class _ProviderContractCase:
    provider: InvocationProvider
    request: InvocationRequest


type _ProviderFactory = Callable[[Path, bool], _ProviderContractCase]


def _request(invocation_id: str, cwd: Path) -> InvocationRequest:
    return InvocationRequest(
        invocation_id=invocation_id,
        capability_id=AGENT_CAPABILITY_ID,
        operation=AGENT_OPERATION_INVOKE,
        input={
            "prompt": "Return the contract JSON result",
            "cwd": str(cwd),
            "sandbox": "read_only",
        },
        idempotency_key=f"provider-contract:{invocation_id}",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        required_features=_COMMON_FEATURES,
        output_schema=_OUTPUT_SCHEMA,
        policy_context={"network_policy": "deny"},
        model="contract/model",
        effort="high",
    )


def _fake_case(tmp_path: Path, cancellation: bool) -> _ProviderContractCase:
    scenario = FakeAgentScenario(
        output={"answer": "ok"},
        events=({"type": "progress"},),
        delay_seconds=0.01 if cancellation else 0,
    )
    return _ProviderContractCase(
        provider=FakeAgentProvider(scenario),
        request=_request("provider-contract-fake", tmp_path),
    )


def _codex_case(tmp_path: Path, cancellation: bool) -> _ProviderContractCase:
    turn = _ContractCodexTurn(
        notifications=(
            _ContractCodexNotification(
                "turn/completed",
                {"turn": {"status": "interrupted"}},
            ),
        )
        if cancellation
        else (
            _ContractCodexNotification(
                "item/completed",
                {
                    "item": {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": '{"answer":"ok"}',
                    }
                },
            ),
            _ContractCodexNotification(
                "turn/completed",
                {"turn": {"status": "completed"}},
            ),
        ),
        wait_for_interrupt=cancellation,
    )
    provider = CodexAgentProvider(
        CodexProviderConfig(network_deny_enforced=True),
        sdk=_ContractCodexSdk(_ContractCodexClient(_ContractCodexThread(turn))),
        session_store=MemorySessionStore(),
    )
    return _ProviderContractCase(
        provider=provider,
        request=_request("provider-contract-codex", tmp_path),
    )


def _a2a_case(tmp_path: Path, cancellation: bool) -> _ProviderContractCase:
    del cancellation
    return _ProviderContractCase(
        provider=A2AInvocationProvider(_ContractRemoteClient()),
        request=_request("provider-contract-a2a", tmp_path),
    )


@dataclass(slots=True)
class _ContractCodexNotification:
    method: str
    payload: object


@dataclass(slots=True)
class _ContractCodexTurn:
    notifications: tuple[_ContractCodexNotification, ...]
    wait_for_interrupt: bool = False
    id: str = "contract-turn"
    interrupted: asyncio.Event = field(default_factory=asyncio.Event)

    async def stream(self) -> AsyncIterator[NativeNotification]:
        if self.wait_for_interrupt:
            await self.interrupted.wait()
        for notification in self.notifications:
            yield notification

    async def steer(self, input: str) -> object:
        del input
        return {"turnId": self.id}

    async def interrupt(self) -> object:
        self.interrupted.set()
        return {"requested": True}


@dataclass(slots=True)
class _ContractCodexThread:
    turn_handle: _ContractCodexTurn
    id: str = "contract-thread"

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
        del input, approval_mode, cwd, effort, model, output_schema, sandbox
        return self.turn_handle


@dataclass(slots=True)
class _ContractCodexClient:
    thread: _ContractCodexThread
    closed: bool = False

    async def __aenter__(self) -> NativeClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exc_type, exc, traceback
        self.closed = True

    async def thread_start(
        self,
        *,
        approval_mode: object,
        cwd: str,
        ephemeral: bool,
        model: str,
        sandbox: object,
    ) -> NativeThread:
        del approval_mode, cwd, ephemeral, model, sandbox
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
        del thread_id, approval_mode, cwd, model, sandbox
        return self.thread

    async def models(self, *, include_hidden: bool = False) -> object:
        del include_hidden
        return {"data": [], "next_cursor": None}


class _ContractCodexSdk:
    def __init__(self, client: _ContractCodexClient) -> None:
        self.client = client

    def create_client(self) -> NativeClient:
        return self.client

    @staticmethod
    def approval_deny_all() -> object:
        return "deny_all"

    @staticmethod
    def sandbox(value: str) -> object:
        return value

    @staticmethod
    def effort(value: str) -> object:
        return value


class _ContractRemoteHandle:
    def __init__(self, request: TaskRequest) -> None:
        self.task_id = request.task_id
        self.request = request
        self.cancelled = False
        self.closed = False

    @property
    def invocation_id(self) -> None:
        return None

    @property
    def delegation_id(self) -> None:
        return None

    @property
    def activation_id(self) -> None:
        return None

    async def events(self, *, start_sequence: int = 1) -> AsyncIterator[TaskEvent]:
        if start_sequence <= 1 and not self.cancelled:
            yield TaskEvent(
                task_id=self.task_id,
                sequence=1,
                status=TaskStatus.WORKING,
                payload={"type": "progress"},
            )

    async def wait(self) -> TaskResult:
        if self.cancelled:
            return TaskResult(
                task_id=self.task_id,
                invocation_id=None,
                status=TaskStatus.CANCELLED,
            )
        return TaskResult(
            task_id=self.task_id,
            invocation_id=None,
            status=TaskStatus.COMPLETED,
            output={"answer": "ok"},
        )

    async def cancel(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("cancellation reason must not be empty")
        self.cancelled = True

    async def close(self) -> None:
        self.closed = True


class _ContractRemoteClient:
    def __init__(self) -> None:
        self.requests: list[TaskRequest] = []
        self.handles: dict[str, _ContractRemoteHandle] = {}

    async def describe(self) -> A2AAgentCard:
        return A2AAgentCard(
            agent_id="provider-contract-agent",
            name="Provider Contract Agent",
            description="A2A fixture for the shared InvocationProvider contract",
            version="1.0.0",
            skills=(
                A2ASkill(
                    skill_id="agent.invoke",
                    name="Invoke",
                    description="Run an agent invocation",
                    capability_id=AGENT_CAPABILITY_ID,
                    operation=AGENT_OPERATION_INVOKE,
                    output_schema=_OUTPUT_SCHEMA,
                    features=_COMMON_FEATURES,
                    required_task_fields=frozenset({"model", "effort"}),
                ),
            ),
            features=_COMMON_FEATURES,
        )

    async def submit(self, request: TaskRequest) -> _ContractRemoteHandle:
        self.requests.append(request)
        handle = _ContractRemoteHandle(request)
        self.handles[request.task_id] = handle
        return handle

    async def get(self, task_id: str) -> TaskSnapshot:
        handle = self.handles[task_id]
        result = await handle.wait()
        return TaskSnapshot(
            request=handle.request,
            fingerprint="provider-contract",
            status=result.status,
            invocation_id=None,
            delegation_id=None,
            activation_id=None,
            events=(),
            result=result,
        )

    async def close(self) -> None:
        return None


@pytest.mark.parametrize(
    "factory",
    (_fake_case, _codex_case, _a2a_case),
    ids=("fake", "codex", "a2a"),
)
@pytest.mark.asyncio
async def test_invocation_providers_share_success_contract(
    tmp_path: Path,
    factory: _ProviderFactory,
) -> None:
    case = factory(tmp_path, False)

    descriptor = await case.provider.describe()
    assert descriptor.capability_id == AGENT_CAPABILITY_ID
    assert any(operation.name == AGENT_OPERATION_INVOKE for operation in descriptor.operations)
    assert _COMMON_FEATURES <= descriptor.features

    handle = await case.provider.start(case.request)
    try:
        events = [event async for event in handle.events()]
        result = await handle.wait()
        reconciliation = await handle.reconcile()

        assert events
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        assert all(event.invocation_id == case.request.invocation_id for event in events)
        assert all(event.status is InvocationStatus.RUNNING for event in events)
        assert result.invocation_id == case.request.invocation_id
        assert result.status is InvocationStatus.SUCCEEDED
        assert result.output == {"answer": "ok"}
        assert reconciliation.status is ReconcileStatus.SUCCEEDED
    finally:
        await handle.close()


@pytest.mark.parametrize(
    "factory",
    (_fake_case, _codex_case, _a2a_case),
    ids=("fake", "codex", "a2a"),
)
@pytest.mark.asyncio
async def test_invocation_providers_share_cancellation_contract(
    tmp_path: Path,
    factory: _ProviderFactory,
) -> None:
    case = factory(tmp_path, True)
    handle = await case.provider.start(case.request)
    try:
        with pytest.raises(ValueError, match=r"empty|must not"):
            await handle.cancel("  ")

        await handle.cancel("provider contract cancellation")
        result = await handle.wait()
        reconciliation = await handle.reconcile()

        assert result.invocation_id == case.request.invocation_id
        assert result.status is InvocationStatus.CANCELLED
        assert reconciliation.status is ReconcileStatus.CANCELLED
    finally:
        await handle.close()

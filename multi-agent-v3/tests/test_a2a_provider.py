from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, cast

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
from misaka_a2a_provider import A2AInvocationProvider, A2AProviderConfig
from misaka_invocation_contracts import (
    CapabilityFeature,
    CompletionBoundary,
    InvocationEvent,
    InvocationRequest,
    InvocationStatus,
    ProviderExecutionRef,
    ReconcileStatus,
)
from misaka_invocation_runtime import InvocationRuntime, ProviderContractError


def _card() -> A2AAgentCard:
    features = frozenset(
        {
            CapabilityFeature.STRUCTURED_OUTPUT,
            CapabilityFeature.STREAMING,
            CapabilityFeature.CANCELLATION,
            CapabilityFeature.RESUME,
        }
    )
    return A2AAgentCard(
        agent_id="remote-agent",
        name="Remote Agent",
        description="Fake remote A2A endpoint",
        version="1.2.0",
        skills=(
            A2ASkill(
                skill_id="agent.invoke",
                name="Invoke",
                description="Run an agent invocation",
                capability_id="agent.invocation",
                operation="invoke",
                output_schema={"type": "object"},
                features=features,
                required_task_fields=frozenset({"model", "effort"}),
            ),
        ),
        features=features,
    )


def _request(invocation_id: str = "invocation-1") -> InvocationRequest:
    return InvocationRequest(
        invocation_id=invocation_id,
        capability_id="agent.invocation",
        operation="invoke",
        input={"prompt": "inspect", "cwd": "D:/dev/project"},
        idempotency_key=f"idempotency-{invocation_id}",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        output_schema={"type": "object"},
        model="remote/model",
        effort="high",
    )


class _FakeRemoteHandle:
    def __init__(
        self,
        task_id: str,
        events: tuple[TaskEvent, ...],
        result: TaskResult,
    ) -> None:
        self.task_id = task_id
        self.events_history = events
        self._result = result
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
        for event in self.events_history:
            if event.sequence >= start_sequence:
                yield event

    async def wait(self) -> TaskResult:
        return self._result

    async def cancel(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("reason must not be empty")
        self.cancelled = True

    async def close(self) -> None:
        self.closed = True


class _FakeRemoteClient:
    def __init__(self) -> None:
        self.requests: list[TaskRequest] = []
        self.handles: dict[str, _FakeRemoteHandle] = {}
        self.closed = False

    async def describe(self) -> A2AAgentCard:
        return _card()

    async def submit(self, request: TaskRequest) -> _FakeRemoteHandle:
        self.requests.append(request)
        events = (
            TaskEvent(
                task_id=request.task_id,
                sequence=1,
                status=TaskStatus.WORKING,
                payload={"step": 1},
            ),
        )
        result = TaskResult(
            task_id=request.task_id,
            invocation_id=None,
            status=TaskStatus.COMPLETED,
            output={"answer": "remote-ok"},
        )
        handle = _FakeRemoteHandle(request.task_id, events, result)
        self.handles[request.task_id] = handle
        return handle

    async def get(self, task_id: str) -> TaskSnapshot:
        request = self.requests[-1]
        handle = self.handles[task_id]
        result = await handle.wait()
        return TaskSnapshot(
            request=request,
            fingerprint="fingerprint",
            status=result.status,
            invocation_id=None,
            delegation_id=None,
            activation_id=None,
            events=tuple(handle.events_history),
            result=result,
        )

    async def close(self) -> None:
        self.closed = True


class _ProviderIdentity(Protocol):
    provider_operation_id: str
    provider_session_id: str


@pytest.mark.asyncio
async def test_a2a_provider_negotiates_card_and_maps_invocation() -> None:
    remote = _FakeRemoteClient()
    provider = A2AInvocationProvider(
        remote,
        config=A2AProviderConfig(provider_id="remote-a2a", remote_provider_id="remote-agent"),
    )

    descriptor = await provider.describe()
    assert descriptor.version == "1.2.0"
    assert descriptor.capability_id == "agent.invocation"
    assert CapabilityFeature.STREAMING in descriptor.features
    assert CapabilityFeature.RESUME in descriptor.features

    invocation = _request()
    handle = await provider.start(invocation)
    events = [event async for event in handle.events()]
    result = await handle.wait()

    assert events == [
        InvocationEvent(
            invocation_id="invocation-1",
            sequence=1,
            status=InvocationStatus.RUNNING,
            payload={"step": 1},
            occurred_at=events[0].occurred_at,
        )
    ]
    assert result.status is InvocationStatus.SUCCEEDED
    assert result.output == {"answer": "remote-ok"}
    identity = cast(_ProviderIdentity, handle)
    assert identity.provider_operation_id == "misaka-a2a-task:invocation-1"
    assert identity.provider_session_id == "misaka-a2a-context:invocation-1"
    assert remote.requests[0].provider_id == "remote-agent"
    assert remote.requests[0].input["cwd"] == "D:/dev/project"
    assert remote.requests[0].metadata["sourceInvocationId"] == "invocation-1"


@pytest.mark.asyncio
async def test_a2a_provider_rejects_missing_required_task_field_before_submit() -> None:
    remote = _FakeRemoteClient()
    provider = A2AInvocationProvider(remote)
    request = _request()
    request = InvocationRequest(
        invocation_id=request.invocation_id,
        capability_id=request.capability_id,
        operation=request.operation,
        input=request.input,
        idempotency_key=request.idempotency_key,
        completion_boundary=request.completion_boundary,
        output_schema=request.output_schema,
        effort=request.effort,
    )

    with pytest.raises(ProviderContractError, match="remote A2A skill requires: model"):
        await provider.start(request)
    assert remote.requests == []


@pytest.mark.asyncio
async def test_a2a_provider_can_be_registered_in_invocation_runtime() -> None:
    remote = _FakeRemoteClient()
    provider = A2AInvocationProvider(remote, config=A2AProviderConfig(provider_id="remote-a2a"))
    runtime = InvocationRuntime()
    await runtime.register_provider("remote-a2a", provider)

    handle = await runtime.submit(_request("invocation-runtime"), provider_id="remote-a2a")
    result = await handle.wait()

    assert result.status is InvocationStatus.SUCCEEDED
    assert result.output == {"answer": "remote-ok"}
    await runtime.stop()
    assert remote.closed is False


@pytest.mark.asyncio
async def test_a2a_provider_reconciles_persisted_remote_task() -> None:
    remote = _FakeRemoteClient()
    provider = A2AInvocationProvider(remote)
    invocation = _request("invocation-reconcile")
    await provider.start(invocation)

    result = await provider.reconcile_persisted(
        invocation,
        ProviderExecutionRef(
            provider_id="a2a",
            provider_epoch=1,
            provider_operation_id="misaka-a2a-task:invocation-reconcile",
            external_start_attempted=True,
        ),
    )

    assert result.status is ReconcileStatus.SUCCEEDED
    assert result.output == {"answer": "remote-ok"}

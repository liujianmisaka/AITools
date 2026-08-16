from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from multi_agent_v2.packages.agent_runtime import (
    AgentCompletedEvent,
    AgentErrorInfo,
    AgentEvent,
    AgentExecutionIdentity,
    AgentMessageDeltaEvent,
    AgentPolicyContext,
    AgentReconcileRequest,
    AgentResumeRequest,
    AgentRuntimeError,
    AgentStartedEvent,
    AgentStartRequest,
    AgentStreamContractError,
    FakeRuntime,
    FakeScenario,
    ReconcileResult,
    WorkspaceLease,
    validate_agent_stream,
)
from multi_agent_v2.packages.workflow_dsl.ir import StrictSchemaIr


def _output_schema() -> StrictSchemaIr:
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    canonical = json.dumps(schema, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return StrictSchemaIr(
        canonical=canonical,
        sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def _request(
    root: Path,
    *,
    execution_id: str = "workflow-1:agent:1",
    model: str = "fake/model",
    effort: str = "high",
) -> AgentStartRequest:
    return AgentStartRequest(
        identity=AgentExecutionIdentity(
            execution_id=execution_id,
            workflow_instance_id="workflow-1",
            node_id="agent",
            activation=1,
            attempt=1,
            idempotency_key=execution_id,
        ),
        provider="fake",
        model=model,
        effort=effort,
        workspace=WorkspaceLease(
            lease_id="workspace-lease-1",
            workspace_id="repo",
            root=root.resolve(),
            access_mode="read_only",
            isolated=False,
        ),
        prompt="Answer the supplied input.",
        resolved_inputs={"question": "one plus one"},
        output_schema=_output_schema(),
        timeout_ms=10_000,
        policy=AgentPolicyContext(sandbox_mode="read_only"),
    )


async def _collect(events: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    return [event async for event in events]


def test_request_requires_explicit_model_and_effort(tmp_path: Path) -> None:
    request = _request(tmp_path)
    raw = request.model_dump(mode="python")
    del raw["model"]

    with pytest.raises(ValidationError):
        AgentStartRequest.model_validate(raw)

    raw = request.model_dump(mode="python")
    del raw["effort"]
    with pytest.raises(ValidationError):
        AgentStartRequest.model_validate(raw)


@pytest.mark.parametrize("field", ["model", "effort"])
def test_request_rejects_blank_model_and_effort(tmp_path: Path, field: str) -> None:
    raw = _request(tmp_path).model_dump(mode="python")
    raw[field] = "   "

    with pytest.raises(ValidationError):
        AgentStartRequest.model_validate(raw)


@pytest.mark.asyncio
async def test_prepare_start_stream_is_deterministic_and_reconcilable(tmp_path: Path) -> None:
    request = _request(tmp_path)
    runtime = FakeRuntime(
        scenarios={
            request.identity.execution_id: FakeScenario(
                deltas=("a", "b"),
                output={"answer": "ab"},
            )
        }
    )

    session = await runtime.prepare_session(request)
    assert session.provider_session_id == f"fake-session:{request.identity.execution_id}"
    assert await runtime.reconcile(
        AgentReconcileRequest(execution_id=request.identity.execution_id)
    ) == ReconcileResult(
        execution_id=request.identity.execution_id,
        status="prepared",
        provider_session_id=session.provider_session_id,
        attachable=False,
    )

    turn = await runtime.start_turn(request, session)
    events = await _collect(
        validate_agent_stream(
            runtime.stream(turn),
            execution_id=request.identity.execution_id,
            provider_session_id=turn.provider_session_id,
        )
    )

    assert [event.sequence for event in events] == [1, 2, 3, 4]
    assert [event.kind for event in events] == [
        "started",
        "message_delta",
        "message_delta",
        "completed",
    ]
    assert isinstance(events[-1], AgentCompletedEvent)
    assert events[-1].output == {"answer": "ab"}
    reconciled = await runtime.reconcile(
        AgentReconcileRequest(execution_id=request.identity.execution_id)
    )
    assert reconciled.status == "succeeded"
    assert reconciled.output == {"answer": "ab"}
    assert reconciled.last_sequence == 4


@pytest.mark.asyncio
async def test_prepare_and_start_are_idempotent_for_same_request(tmp_path: Path) -> None:
    request = _request(tmp_path)
    runtime = FakeRuntime()

    first_session = await runtime.prepare_session(request)
    second_session = await runtime.prepare_session(request)
    first_turn = await runtime.start_turn(request, first_session)
    second_turn = await runtime.start_turn(request, second_session)

    assert first_session == second_session
    assert first_turn == second_turn
    assert runtime.prepare_calls == [request]
    assert runtime.start_turn_calls == [request]


@pytest.mark.asyncio
async def test_temporal_attempt_is_not_part_of_logical_request_identity(tmp_path: Path) -> None:
    request = _request(tmp_path)
    retry = request.model_copy(
        update={"identity": request.identity.model_copy(update={"attempt": 2})}
    )
    runtime = FakeRuntime()

    first = await runtime.prepare_session(request)
    second = await runtime.prepare_session(retry)

    assert first == second
    assert runtime.prepare_calls == [request]


@pytest.mark.asyncio
async def test_same_execution_rejects_different_request(tmp_path: Path) -> None:
    request = _request(tmp_path)
    runtime = FakeRuntime()
    await runtime.prepare_session(request)

    with pytest.raises(AgentRuntimeError) as captured:
        await runtime.prepare_session(request.model_copy(update={"prompt": "different"}))

    assert captured.value.code == "agent.idempotency_conflict"


@pytest.mark.asyncio
async def test_resume_uses_supplied_provider_session(tmp_path: Path) -> None:
    start = _request(tmp_path)
    resume = AgentResumeRequest(
        **start.model_dump(mode="python", exclude={"session_mode"}),
        provider_session_id="existing-session",
    )
    runtime = FakeRuntime()

    session = await runtime.prepare_session(resume)
    turn = await runtime.start_turn(resume, session)

    assert session.provider_session_id == "existing-session"
    assert turn.provider_session_id == "existing-session"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "effort", "code"),
    [
        ("unknown/model", "high", "agent.model_unsupported"),
        ("fake/model", "impossible", "agent.effort_unsupported"),
    ],
)
async def test_fake_runtime_validates_model_catalog_selection(
    tmp_path: Path,
    model: str,
    effort: str,
    code: str,
) -> None:
    runtime = FakeRuntime()

    with pytest.raises(AgentRuntimeError) as captured:
        await runtime.prepare_session(_request(tmp_path, model=model, effort=effort))

    assert captured.value.code == code


@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_stream_finishes_cancelled(tmp_path: Path) -> None:
    request = _request(tmp_path)
    runtime = FakeRuntime(
        scenarios={request.identity.execution_id: FakeScenario(delay_seconds=2.0)}
    )
    session = await runtime.prepare_session(request)
    turn = await runtime.start_turn(request, session)
    stream_task = asyncio.create_task(
        _collect(
            validate_agent_stream(
                runtime.stream(turn),
                execution_id=request.identity.execution_id,
                provider_session_id=turn.provider_session_id,
            )
        )
    )
    await asyncio.sleep(0)

    first = await runtime.cancel(turn)
    second = await runtime.cancel(turn)
    events = await stream_task

    assert first.status == "requested"
    assert second.status == "requested"
    assert runtime.cancel_count == 1
    assert events[-1].kind == "cancelled"
    assert (await runtime.cancel(turn)).status == "already_terminal"
    assert (
        await runtime.reconcile(AgentReconcileRequest(execution_id=request.identity.execution_id))
    ).status == "cancelled"


@pytest.mark.asyncio
async def test_steer_only_accepts_a_running_turn(tmp_path: Path) -> None:
    request = _request(tmp_path)
    runtime = FakeRuntime(
        scenarios={
            request.identity.execution_id: FakeScenario(output={"answer": "finished"}),
        }
    )
    session = await runtime.prepare_session(request)
    turn = await runtime.start_turn(request, session)

    await runtime.steer(turn, "continue")
    await _collect(
        validate_agent_stream(
            runtime.stream(turn),
            execution_id=request.identity.execution_id,
            provider_session_id=turn.provider_session_id,
        )
    )

    assert runtime.steer_calls == [(request.identity.execution_id, "continue")]
    with pytest.raises(AgentRuntimeError) as captured:
        await runtime.steer(turn, "too late")
    assert captured.value.code == "agent.turn_not_running"


@pytest.mark.asyncio
async def test_failed_and_incomplete_scenarios_reconcile_conservatively(tmp_path: Path) -> None:
    failed_request = _request(tmp_path, execution_id="workflow-1:failed:1")
    incomplete_request = _request(tmp_path, execution_id="workflow-1:incomplete:1")
    runtime = FakeRuntime(
        scenarios={
            failed_request.identity.execution_id: FakeScenario(
                error=AgentErrorInfo(
                    code="fake.failure",
                    message="planned failure",
                    retryable=True,
                )
            ),
            incomplete_request.identity.execution_id: FakeScenario(incomplete_stream=True),
        }
    )

    failed_session = await runtime.prepare_session(failed_request)
    failed_turn = await runtime.start_turn(failed_request, failed_session)
    failed_events = await _collect(
        validate_agent_stream(
            runtime.stream(failed_turn),
            execution_id=failed_request.identity.execution_id,
            provider_session_id=failed_turn.provider_session_id,
        )
    )
    assert failed_events[-1].kind == "failed"
    assert (
        await runtime.reconcile(
            AgentReconcileRequest(execution_id=failed_request.identity.execution_id)
        )
    ).status == "failed"

    incomplete_session = await runtime.prepare_session(incomplete_request)
    incomplete_turn = await runtime.start_turn(incomplete_request, incomplete_session)
    with pytest.raises(AgentStreamContractError):
        await _collect(
            validate_agent_stream(
                runtime.stream(incomplete_turn),
                execution_id=incomplete_request.identity.execution_id,
                provider_session_id=incomplete_turn.provider_session_id,
            )
        )
    assert (
        await runtime.reconcile(
            AgentReconcileRequest(execution_id=incomplete_request.identity.execution_id)
        )
    ).status == "uncertain"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        ReconcileResult(execution_id="override", status="not_found"),
        ReconcileResult(
            execution_id="override",
            status="prepared",
            provider_session_id="session",
            attachable=True,
        ),
        ReconcileResult(
            execution_id="override",
            status="running",
            provider_session_id="session",
            attachable=True,
            last_sequence=2,
        ),
        ReconcileResult(
            execution_id="override",
            status="succeeded",
            provider_session_id="session",
            output={"answer": "done"},
            last_sequence=3,
        ),
        ReconcileResult(
            execution_id="override",
            status="failed",
            provider_session_id="session",
            error=AgentErrorInfo(code="failed", message="failed"),
            last_sequence=3,
        ),
        ReconcileResult(
            execution_id="override",
            status="cancelled",
            provider_session_id="session",
            last_sequence=3,
        ),
        ReconcileResult(
            execution_id="override",
            status="uncertain",
            provider_session_id="session",
            last_sequence=1,
        ),
    ],
)
async def test_fake_reconcile_can_script_every_contract_status(
    result: ReconcileResult,
) -> None:
    runtime = FakeRuntime(reconcile_overrides={"override": result})

    assert await runtime.reconcile(AgentReconcileRequest(execution_id="override")) == result


async def _events(*items: AgentEvent) -> AsyncIterator[AgentEvent]:
    for item in items:
        yield item


def _started(execution_id: str, sequence: int) -> AgentStartedEvent:
    return AgentStartedEvent(
        execution_id=execution_id,
        sequence=sequence,
        provider_session_id="session",
        model="fake/model",
        effort="high",
    )


def _completed(execution_id: str, sequence: int) -> AgentCompletedEvent:
    return AgentCompletedEvent(
        execution_id=execution_id,
        sequence=sequence,
        provider_session_id="session",
        output={"answer": "done"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stream",
    [
        _events(_started("wrong", 1), _completed("wrong", 2)),
        _events(
            AgentStartedEvent(
                execution_id="expected",
                sequence=1,
                provider_session_id="wrong-session",
                model="fake/model",
                effort="high",
            )
        ),
        _events(_started("expected", 2), _completed("expected", 3)),
        _events(_started("expected", 1)),
        _events(
            _started("expected", 1),
            _completed("expected", 2),
            AgentMessageDeltaEvent(
                execution_id="expected",
                sequence=3,
                provider_session_id="session",
                text="late",
            ),
        ),
    ],
)
async def test_stream_contract_rejects_identity_sequence_terminal_violations(
    stream: AsyncIterator[AgentEvent],
) -> None:
    with pytest.raises(AgentStreamContractError):
        await _collect(
            validate_agent_stream(
                stream,
                execution_id="expected",
                provider_session_id="session",
            )
        )

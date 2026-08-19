from __future__ import annotations

import pytest
from misaka_agent_capability import AGENT_CAPABILITY_ID
from misaka_coordinator_adapters import InvocationExecutionPlan
from misaka_coordinator_runtime import ExecutionPlan, ExecutionStatus
from misaka_coordinator_workflow import (
    DAGCoordinator,
    DAGDefinition,
    DAGNode,
    StateMachineCoordinator,
    StateMachineDefinition,
    StateTransition,
    WorkflowContext,
    WorkflowStateError,
    WorkflowStatus,
)
from misaka_fake_agent import FakeAgentProvider, FakeAgentScenario, FakeFailure
from misaka_invocation_contracts import CompletionBoundary, InvocationRequest
from misaka_invocation_runtime import InvocationRuntime


def _request(invocation_id: str) -> InvocationRequest:
    return InvocationRequest(
        invocation_id=invocation_id,
        capability_id=AGENT_CAPABILITY_ID,
        operation="invoke",
        input={"prompt": invocation_id},
        idempotency_key=invocation_id,
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        output_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
        model="fake/model",
        effort="high",
    )


async def _runtime(scenario: FakeAgentScenario | None = None) -> InvocationRuntime:
    runtime = InvocationRuntime(cancellation_timeout_seconds=0.5, shutdown_timeout_seconds=0.5)
    await runtime.register_provider("fake", FakeAgentProvider(scenario))
    return runtime


def _plan(runtime: InvocationRuntime, request: InvocationRequest) -> InvocationExecutionPlan:
    return InvocationExecutionPlan(runtime, request, provider_id="fake")


@pytest.mark.asyncio
async def test_dag_runs_ready_nodes_in_parallel_and_passes_outputs_to_dependents() -> None:
    runtime = await _runtime(FakeAgentScenario(output={"answer": "done"}, delay_seconds=0.02))
    seen: list[dict[str, object]] = []

    async def first(context: WorkflowContext) -> InvocationExecutionPlan:
        return _plan(runtime, _request(f"{context.run_id}-{context.node_id}"))

    async def second(context: WorkflowContext) -> InvocationExecutionPlan:
        seen.append(dict(context.outputs))
        return _plan(runtime, _request(f"{context.run_id}-{context.node_id}"))

    coordinator = DAGCoordinator(max_concurrency=2)
    result = await coordinator.run(
        "run-dag",
        DAGDefinition(
            (
                DAGNode("first", first),
                DAGNode("second", second, depends_on=("first",)),
                DAGNode("parallel", first),
            )
        ),
    )
    assert result.status is WorkflowStatus.SUCCEEDED
    assert set(result.node_results) == {"first", "second", "parallel"}
    assert seen and "first" in seen[0]
    await runtime.stop()


def test_dag_rejects_cycles() -> None:
    async def factory(context: WorkflowContext) -> ExecutionPlan:
        del context
        raise AssertionError("cyclic DAG must be rejected before a plan is requested")

    with pytest.raises(ValueError, match="cycle"):
        DAGDefinition(
            (
                DAGNode("a", factory, depends_on=("b",)),
                DAGNode("b", factory, depends_on=("a",)),
            )
        )


@pytest.mark.asyncio
async def test_state_machine_commits_action_only_after_success() -> None:
    runtime = await _runtime()

    async def action(context: WorkflowContext) -> InvocationExecutionPlan:
        return _plan(runtime, _request(f"{context.run_id}-approve"))

    definition = StateMachineDefinition(
        initial_state="pending",
        states=frozenset({"pending", "approved", "rejected"}),
        terminal_states=frozenset({"approved", "rejected"}),
        transitions=(
            StateTransition("pending", "approve", "approved", action),
            StateTransition("pending", "reject", "rejected"),
        ),
    )
    coordinator = StateMachineCoordinator()
    initial = coordinator.start("run-state", definition)
    assert initial.state == "pending"
    approved = await coordinator.dispatch("run-state", definition, "approve")
    assert approved.state == "approved"
    assert approved.outputs["approved"].status is ExecutionStatus.SUCCEEDED
    with pytest.raises(WorkflowStateError, match="terminal"):
        await coordinator.dispatch("run-state", definition, "reject")
    await runtime.stop()


@pytest.mark.asyncio
async def test_dag_surfaces_failure_without_retrying_or_hiding_it() -> None:
    runtime = await _runtime(FakeAgentScenario(failure=FakeFailure("fake.failed", "node failed")))

    async def factory(context: WorkflowContext) -> InvocationExecutionPlan:
        return _plan(runtime, _request(f"{context.run_id}-{context.node_id}"))

    result = await DAGCoordinator().run(
        "run-failed",
        DAGDefinition((DAGNode("node", factory),)),
    )
    assert result.status is WorkflowStatus.FAILED
    assert result.error_message == "node failed"
    await runtime.stop()

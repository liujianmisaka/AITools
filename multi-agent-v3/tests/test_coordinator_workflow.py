from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from misaka_agent_capability import AGENT_CAPABILITY_ID
from misaka_coordinator_adapters import InvocationExecutionPlan
from misaka_coordinator_runtime import (
    ExecutionEvent,
    ExecutionPlan,
    ExecutionResult,
    ExecutionStatus,
    ReconciliationResult,
    ReconciliationState,
)
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


class _WorkflowHandle:
    def __init__(
        self,
        execution_id: str,
        result: ExecutionResult,
        *,
        wait_for: asyncio.Event | None = None,
    ) -> None:
        self.execution_id = execution_id
        self.activation_id = f"{execution_id}:activation:1"
        self.result = result
        self.wait_for = wait_for
        self.wait_started = asyncio.Event()
        self.cancel_reason: str | None = None

    def events(self, *, start_sequence: int = 1) -> AsyncIterator[ExecutionEvent]:
        async def _events() -> AsyncIterator[ExecutionEvent]:
            if False:
                yield ExecutionEvent(self.execution_id, 1, self.result.status)

        return _events()

    async def wait(self) -> ExecutionResult:
        self.wait_started.set()
        if self.wait_for is not None:
            await self.wait_for.wait()
        return self.result

    async def cancel(self, reason: str) -> None:
        self.cancel_reason = reason

    async def reconcile(self) -> ReconciliationResult:
        state = {
            ExecutionStatus.SUCCEEDED: ReconciliationState.SUCCEEDED,
            ExecutionStatus.FAILED: ReconciliationState.FAILED,
            ExecutionStatus.CANCELLED: ReconciliationState.CANCELLED,
            ExecutionStatus.RECONCILIATION_REQUIRED: ReconciliationState.UNREACHABLE,
        }[self.result.status]
        return ReconciliationResult(state=state)


class _WorkflowPlan:
    def __init__(self, handle: _WorkflowHandle) -> None:
        self.execution_id = handle.execution_id
        self.fingerprint = handle.execution_id
        self.handle = handle

    async def start(self, *, attempt: int = 1) -> _WorkflowHandle:
        if attempt != 1:
            raise ValueError("workflow test plan only supports one attempt")
        return self.handle


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


@pytest.mark.asyncio
async def test_dag_fail_fast_cancels_active_sibling_execution() -> None:
    release_slow = asyncio.Event()
    slow = _WorkflowHandle(
        "slow",
        ExecutionResult("slow", ExecutionStatus.SUCCEEDED, activation_id="slow:activation:1"),
        wait_for=release_slow,
    )
    failure = _WorkflowHandle(
        "failure",
        ExecutionResult(
            "failure",
            ExecutionStatus.FAILED,
            activation_id="failure:activation:1",
            error_message="first node failed",
        ),
        wait_for=slow.wait_started,
    )

    async def slow_factory(context: WorkflowContext) -> _WorkflowPlan:
        del context
        return _WorkflowPlan(slow)

    async def failure_factory(context: WorkflowContext) -> _WorkflowPlan:
        del context
        return _WorkflowPlan(failure)

    result = await DAGCoordinator(max_concurrency=2, fail_fast=True).run(
        "run-fail-fast",
        DAGDefinition(
            (
                DAGNode("failure", failure_factory),
                DAGNode("slow", slow_factory),
            )
        ),
    )

    assert result.status is WorkflowStatus.FAILED
    assert slow.cancel_reason == "DAG fail-fast"


@pytest.mark.asyncio
async def test_dag_reconciliation_failure_has_priority_over_ordinary_failure() -> None:
    async def failed_factory(context: WorkflowContext) -> _WorkflowPlan:
        del context
        return _WorkflowPlan(
            _WorkflowHandle(
                "failed",
                ExecutionResult(
                    "failed",
                    ExecutionStatus.FAILED,
                    activation_id="failed:activation:1",
                    error_message="ordinary failure",
                ),
            )
        )

    async def uncertain_factory(context: WorkflowContext) -> _WorkflowPlan:
        del context
        return _WorkflowPlan(
            _WorkflowHandle(
                "uncertain",
                ExecutionResult(
                    "uncertain",
                    ExecutionStatus.RECONCILIATION_REQUIRED,
                    activation_id="uncertain:activation:1",
                    error_message="external state is unknown",
                ),
            )
        )

    result = await DAGCoordinator(max_concurrency=2, fail_fast=False).run(
        "run-priority",
        DAGDefinition(
            (
                DAGNode("failed", failed_factory),
                DAGNode("uncertain", uncertain_factory),
            )
        ),
    )

    assert result.status is WorkflowStatus.RECONCILIATION_REQUIRED

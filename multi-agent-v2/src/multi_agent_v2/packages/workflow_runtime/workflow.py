from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta
from typing import cast

from temporalio import exceptions as temporal_exceptions
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.workflow import ActivityCancellationType

from multi_agent_v2.packages.workflow_dsl.ir import (
    ActivityExecutionIr,
    AgentExecutionIr,
    ApprovalExecutionIr,
    DecisionExecutionIr,
    JoinExecutionIr,
    NodeIr,
    TimerExecutionIr,
)
from multi_agent_v2.packages.workflow_runtime.messages import (
    ApprovalCommand,
    CommandResult,
    NodeActivityRequest,
    NodeActivityResult,
    WorkflowResult,
    WorkflowRunInput,
)
from multi_agent_v2.packages.workflow_runtime.reducer import (
    WorkflowInvariantError,
    command_consumed,
    command_fingerprint,
    complete_node,
    evaluate_node_decision,
    initial_state,
    join_output,
    ready_node_ids,
    record_command,
    resolve_inputs,
    resolve_provider_session_id,
    settle_dag,
    snapshot,
    start_node,
)
from multi_agent_v2.packages.workflow_runtime.state import (
    RuntimeErrorInfo,
    WorkflowRuntimeState,
    WorkflowSnapshot,
)

AGENT_TASK_QUEUE = "agent-windows-v2"
ORCHESTRATION_TASK_QUEUE = "orchestration-v2"
_AGENT_ACTIVITY = "agent.execute.v1"
_REGISTERED_ACTIVITY = "registered-activity.execute.v1"


@workflow.defn(name="WorkflowInstanceWorkflow")
class WorkflowInstanceWorkflow:
    def __init__(self) -> None:
        self._run_input: WorkflowRunInput | None = None
        self._state: WorkflowRuntimeState | None = None
        self._approval_decisions: dict[str, ApprovalCommand] = {}

    @workflow.run
    async def run(self, run_input: WorkflowRunInput) -> WorkflowResult:
        self._run_input = run_input
        self._state = run_input.carried_state or initial_state(
            run_input.plan,
            generation=run_input.generation,
        )
        handles: dict[str, asyncio.Future[NodeActivityResult]] = {}

        try:
            while self._require_state().status == "running":
                self._state = settle_dag(
                    run_input.plan,
                    self._require_state(),
                    run_input.workflow_input,
                )
                if self._require_state().status != "running":
                    break

                ready = ready_node_ids(
                    run_input.plan,
                    self._require_state(),
                    run_input.workflow_input,
                )
                available_slots = max(run_input.plan.max_concurrency - len(handles), 0)
                immediate_progress = False
                for node_id in ready[:available_slots]:
                    node = self._node(node_id)
                    waiting_approval = isinstance(node.execution, ApprovalExecutionIr)
                    self._state = start_node(
                        run_input.plan,
                        self._require_state(),
                        node_id,
                        waiting_approval=waiting_approval,
                    )
                    if self._require_state().status != "running":
                        immediate_progress = True
                        break

                    if isinstance(node.execution, DecisionExecutionIr):
                        decision = evaluate_node_decision(
                            node,
                            self._require_state(),
                            run_input.workflow_input,
                        )
                        self._state = complete_node(
                            run_input.plan,
                            self._require_state(),
                            run_input.workflow_input,
                            node_id,
                            "succeeded",
                            output={"result": decision},
                        )
                        immediate_progress = True
                    elif isinstance(node.execution, JoinExecutionIr):
                        output = join_output(
                            run_input.plan,
                            self._require_state(),
                            run_input.workflow_input,
                            node_id,
                        )
                        self._state = complete_node(
                            run_input.plan,
                            self._require_state(),
                            run_input.workflow_input,
                            node_id,
                            "succeeded",
                            output=output,
                        )
                        immediate_progress = True
                    else:
                        handles[node_id] = self._start_wait(node)

                if immediate_progress:
                    if not handles:
                        await self._continue_as_new_if_needed()
                    continue

                if handles:
                    await workflow.wait(handles.values(), return_when=asyncio.FIRST_COMPLETED)
                    completed_ids = sorted(
                        node_id for node_id, handle in handles.items() if handle.done()
                    )
                    for node_id in completed_ids:
                        result = await self._activity_result(handles.pop(node_id), node_id)
                        self._validate_activity_result(node_id, result)
                        error = None
                        if result.outcome != "succeeded":
                            error = RuntimeErrorInfo(
                                code=result.error_code or f"node.{result.outcome}",
                                message=result.error_message or "node execution did not succeed",
                            )
                        self._state = complete_node(
                            run_input.plan,
                            self._require_state(),
                            run_input.workflow_input,
                            node_id,
                            result.outcome,
                            output=result.output,
                            error=error,
                        )
                    if not handles:
                        await self._continue_as_new_if_needed()
                    continue

                raise ApplicationError(
                    "workflow has no ready or waiting nodes",
                    type="WorkflowInvariantViolated",
                    non_retryable=True,
                )
        except asyncio.CancelledError:
            await self._cancel_handles(handles)
            raise
        except ApplicationError:
            await self._cancel_handles(handles)
            raise
        except WorkflowInvariantError as exc:
            await self._cancel_handles(handles)
            raise ApplicationError(
                "workflow runtime invariant was violated",
                type="WorkflowInvariantViolated",
                non_retryable=True,
            ) from exc
        except ValueError as exc:
            await self._cancel_handles(handles)
            raise ApplicationError(
                "workflow expression did not return the required type",
                type="WorkflowExpressionContractViolated",
                non_retryable=True,
            ) from exc

        await self._cancel_handles(handles)

        await workflow.wait_condition(workflow.all_handlers_finished)
        state = self._require_state()
        if state.status == "running":
            raise ApplicationError(
                "workflow returned before reaching a terminal state",
                type="WorkflowInvariantViolated",
                non_retryable=True,
            )
        return WorkflowResult(
            status=state.status,
            output=state.result,
            error_code=state.error.code if state.error else None,
            error_message=state.error.message if state.error else None,
        )

    @workflow.query(name="state.v1")
    def get_snapshot(self) -> WorkflowSnapshot:
        return snapshot(self._require_state())

    @workflow.query(name="approvals.pending.v1")
    def get_pending_approvals(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                item.node_id
                for item in self._require_state().nodes
                if item.status == "waiting_approval"
            )
        )

    @workflow.update(name="approval.decide.v1")
    def decide_approval(self, command: ApprovalCommand) -> CommandResult:
        fingerprint = command.model_dump_json()
        if command_consumed(self._require_state(), command.command_id):
            return CommandResult(
                command_id=command.command_id,
                accepted=False,
                state_version=self._require_state().state_version,
            )
        key = self._approval_key(command.node_id, command.activation)
        self._approval_decisions[key] = command
        self._state = record_command(self._require_state(), command.command_id, fingerprint)
        return CommandResult(
            command_id=command.command_id,
            accepted=True,
            state_version=self._require_state().state_version,
        )

    @decide_approval.validator
    def validate_approval(self, command: ApprovalCommand) -> None:
        self._validate_command_id(command.command_id)
        fingerprint = command.model_dump_json()
        previous = command_fingerprint(self._require_state(), command.command_id)
        if previous is not None:
            if previous != fingerprint:
                raise ApplicationError(
                    "command ID was already used with different approval data",
                    type="CommandConflict",
                    non_retryable=True,
                )
            return
        key = self._approval_key(command.node_id, command.activation)
        if key in self._approval_decisions:
            raise ApplicationError(
                "approval already has a pending decision",
                type="ApprovalDecisionConflict",
                non_retryable=True,
            )
        node = self._node_state(command.node_id)
        if node.status != "waiting_approval" or node.activation != command.activation:
            raise ApplicationError(
                "approval target is not currently waiting",
                type="ApprovalNotWaiting",
                non_retryable=True,
            )

    @workflow.signal(name="approval.submit.v1")
    def submit_approval(self, command: ApprovalCommand) -> None:
        if command_consumed(self._require_state(), command.command_id):
            return
        key = self._approval_key(command.node_id, command.activation)
        try:
            node = self._node_state(command.node_id)
        except ApplicationError:
            return
        if (
            node.status != "waiting_approval"
            or node.activation != command.activation
            or key in self._approval_decisions
        ):
            return
        self._approval_decisions[key] = command
        self._state = record_command(
            self._require_state(), command.command_id, command.model_dump_json()
        )

    def _start_wait(self, node: NodeIr) -> asyncio.Future[NodeActivityResult]:
        execution = node.execution
        if isinstance(execution, TimerExecutionIr):
            return asyncio.create_task(self._wait_timer(node.id, execution))
        if isinstance(execution, ApprovalExecutionIr):
            return asyncio.create_task(self._wait_approval(node.id, execution))
        if not isinstance(execution, (AgentExecutionIr, ActivityExecutionIr)):
            raise ApplicationError(
                "unsupported executable node",
                type="WorkflowInvariantViolated",
                non_retryable=True,
            )
        state = self._node_state(node.id)
        workflow_id = workflow.info().workflow_id
        activity_id = f"{node.id}:{state.activation}"
        execution_id = self._execution_id(node.id, state.activation)
        request = NodeActivityRequest(
            workflow_instance_id=workflow_id,
            plan_hash=self._require_input().plan.plan_hash,
            node_id=node.id,
            activation=state.activation,
            execution_id=execution_id,
            idempotency_key=execution_id,
            resolved_inputs=resolve_inputs(
                node,
                self._require_state(),
                self._require_input().workflow_input,
            ),
            provider_session_id=resolve_provider_session_id(
                node,
                self._require_state(),
                self._require_input().workflow_input,
            ),
            execution=execution,
            output_schema=node.output_schema,
        )
        retry = execution.retry
        if isinstance(execution, AgentExecutionIr):
            activity_name = _AGENT_ACTIVITY
            task_queue = AGENT_TASK_QUEUE
        else:
            activity_name = _REGISTERED_ACTIVITY
            task_queue = ORCHESTRATION_TASK_QUEUE
        handle = workflow.start_activity(
            activity_name,
            request,
            task_queue=task_queue,
            result_type=NodeActivityResult,
            start_to_close_timeout=timedelta(milliseconds=execution.timeout_ms),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                maximum_attempts=retry.maximum_attempts,
                initial_interval=timedelta(milliseconds=retry.initial_interval_ms),
                maximum_interval=timedelta(milliseconds=retry.maximum_interval_ms),
            ),
            cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            activity_id=activity_id,
            summary=f"node:{node.id}",
        )
        return cast(asyncio.Future[NodeActivityResult], handle)

    async def _wait_timer(
        self,
        node_id: str,
        execution: TimerExecutionIr,
    ) -> NodeActivityResult:
        activation = self._node_state(node_id).activation
        execution_id = self._execution_id(node_id, activation)
        await workflow.sleep(
            timedelta(milliseconds=execution.delay_ms),
            summary=f"timer:{execution_id}",
        )
        return NodeActivityResult(
            execution_id=execution_id,
            outcome="succeeded",
            output={"fired": True},
        )

    async def _wait_approval(
        self,
        node_id: str,
        execution: ApprovalExecutionIr,
    ) -> NodeActivityResult:
        activation = self._node_state(node_id).activation
        execution_id = self._execution_id(node_id, activation)
        key = self._approval_key(node_id, activation)
        try:
            await workflow.wait_condition(
                lambda: key in self._approval_decisions,
                timeout=timedelta(milliseconds=execution.timeout_ms),
                timeout_summary=f"approval:{execution_id}",
            )
        except TimeoutError:
            return NodeActivityResult(
                execution_id=execution_id,
                outcome="timed_out",
                error_code="approval.timed_out",
                error_message="approval timed out",
            )
        command = self._approval_decisions.pop(key)
        return NodeActivityResult(
            execution_id=execution_id,
            outcome="succeeded",
            output={
                "decision": command.decision,
                "operatorLabel": command.operator_label,
                "reason": command.reason,
            },
        )

    async def _activity_result(
        self,
        handle: asyncio.Future[NodeActivityResult],
        node_id: str,
    ) -> NodeActivityResult:
        try:
            return await handle
        except asyncio.CancelledError:
            raise
        except temporal_exceptions.ActivityError as exc:
            execution = self._node(node_id).execution
            reconciliation_required = isinstance(execution, AgentExecutionIr) and (
                isinstance(exc.cause, temporal_exceptions.TimeoutError)
                or (
                    isinstance(exc.cause, temporal_exceptions.ApplicationError)
                    and exc.cause.type == "AgentReconciliationRequired"
                )
            )
            if reconciliation_required:
                return NodeActivityResult(
                    execution_id=self._execution_id(
                        node_id,
                        self._node_state(node_id).activation,
                    ),
                    outcome="reconciliation_required",
                    error_code="agent.reconciliation_required",
                    error_message="agent execution requires reconciliation",
                )
            if isinstance(exc.cause, temporal_exceptions.TimeoutError):
                return NodeActivityResult(
                    execution_id=self._execution_id(
                        node_id,
                        self._node_state(node_id).activation,
                    ),
                    outcome="timed_out",
                    error_code="node.activity_timed_out",
                    error_message="node activity timed out",
                )
            return NodeActivityResult(
                execution_id=self._execution_id(
                    node_id,
                    self._node_state(node_id).activation,
                ),
                outcome="failed",
                error_code="node.activity_failed",
                error_message="node activity failed",
            )
        except Exception as exc:
            raise ApplicationError(
                "node activity returned an unsupported failure",
                type="ActivityResultFailureUnsupported",
                non_retryable=True,
            ) from exc

    def _validate_activity_result(self, node_id: str, result: NodeActivityResult) -> None:
        node_state = self._node_state(node_id)
        expected_execution_id = self._execution_id(node_id, node_state.activation)
        if result.execution_id != expected_execution_id:
            raise ApplicationError(
                "activity result execution identity does not match the active node",
                type="ActivityResultIdentityMismatch",
                non_retryable=True,
            )
        if result.outcome != "succeeded":
            return
        node = self._node(node_id)
        if not isinstance(node.execution, (AgentExecutionIr, ActivityExecutionIr)):
            return
        expected_schema_hash = node.output_schema.sha256
        if result.output is None or result.output_schema_sha256 != expected_schema_hash:
            raise ApplicationError(
                "activity result has not satisfied the compiled output contract",
                type="ActivityResultContractMismatch",
                non_retryable=True,
            )

    @staticmethod
    async def _cancel_handles(handles: dict[str, asyncio.Future[NodeActivityResult]]) -> None:
        if not handles:
            return
        for handle in handles.values():
            handle.cancel()
        await workflow.wait(handles.values())
        for handle in handles.values():
            try:
                await handle
            except (asyncio.CancelledError, Exception):
                pass
        handles.clear()

    async def _continue_as_new_if_needed(self) -> None:
        run_input = self._require_input()
        state = self._require_state()
        if state.status != "running":
            return
        info = workflow.info()
        activation_checkpoint = (
            run_input.plan.continue_as_new_every is not None
            and state.total_activations > 0
            and state.total_activations % run_input.plan.continue_as_new_every == 0
        )
        history_limit = (
            info.get_current_history_length() >= run_input.history_policy.maximum_events
            or info.get_current_history_size() >= run_input.history_policy.maximum_bytes
            or info.is_continue_as_new_suggested()
        )
        if not activation_checkpoint and not history_limit:
            return
        await workflow.wait_condition(workflow.all_handlers_finished)
        next_generation = state.generation + 1
        carried_state = state.model_copy(update={"generation": next_generation})
        workflow.continue_as_new(
            run_input.model_copy(
                update={"carried_state": carried_state, "generation": next_generation}
            )
        )

    @staticmethod
    def _validate_command_id(command_id: str) -> None:
        if not command_id.strip() or len(command_id) > 128:
            raise ApplicationError(
                "command ID must be between 1 and 128 non-blank characters",
                type="InvalidCommandId",
                non_retryable=True,
            )

    def _node(self, node_id: str) -> NodeIr:
        return next(node for node in self._require_input().plan.nodes if node.id == node_id)

    def _node_state(self, node_id: str):  # return type inferred from runtime state
        try:
            return next(item for item in self._require_state().nodes if item.node_id == node_id)
        except StopIteration as exc:
            raise ApplicationError(
                "unknown workflow node",
                type="UnknownNode",
                non_retryable=True,
            ) from exc

    def _require_state(self) -> WorkflowRuntimeState:
        if self._state is None:
            raise ApplicationError(
                "workflow state is not initialized",
                type="WorkflowInvariantViolated",
                non_retryable=True,
            )
        return self._state

    def _require_input(self) -> WorkflowRunInput:
        if self._run_input is None:
            raise ApplicationError(
                "workflow input is not initialized",
                type="WorkflowInvariantViolated",
                non_retryable=True,
            )
        return self._run_input

    @staticmethod
    def _approval_key(node_id: str, activation: int) -> str:
        return f"{node_id}:{activation}"

    @staticmethod
    def _execution_id(node_id: str, activation: int) -> str:
        workflow_id = workflow.info().workflow_id
        readable = f"{workflow_id}:{node_id}:{activation}"
        if len(readable) <= 512:
            return readable
        workflow_hash = hashlib.sha256(workflow_id.encode()).hexdigest()
        return f"workflow:{workflow_hash}:{node_id}:{activation}"

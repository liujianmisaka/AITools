from __future__ import annotations

import asyncio
from dataclasses import replace

from misaka_coordinator_runtime import ExecutionResult, ExecutionStatus, start_execution

from misaka_coordinator_workflow.contracts import (
    StateMachineDefinition,
    StateMachineSnapshot,
    StateTransition,
    WorkflowContext,
)
from misaka_coordinator_workflow.errors import WorkflowStateError


class StateMachineCoordinator:
    def __init__(self) -> None:
        self._snapshots: dict[str, StateMachineSnapshot] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def start(self, run_id: str, definition: StateMachineDefinition) -> StateMachineSnapshot:
        if not run_id.strip():
            raise ValueError("run_id must not be empty")
        if run_id in self._snapshots:
            raise WorkflowStateError("workflow.run_exists", f"run {run_id} already exists")
        snapshot = StateMachineSnapshot(run_id, definition.initial_state)
        self._snapshots[run_id] = snapshot
        self._locks[run_id] = asyncio.Lock()
        return snapshot

    async def dispatch(
        self,
        run_id: str,
        definition: StateMachineDefinition,
        event: str,
    ) -> StateMachineSnapshot:
        lock = self._locks.get(run_id)
        if lock is None:
            self._snapshot(run_id)
            raise AssertionError("state machine lock was not initialized")
        async with lock:
            snapshot = self._snapshot(run_id)
            if snapshot.state in definition.terminal_states:
                raise WorkflowStateError("workflow.terminal", f"run {run_id} is in terminal state")
            transition = _find_transition(definition, snapshot.state, event)
            if transition is None:
                raise WorkflowStateError(
                    "workflow.event_unhandled",
                    f"event {event} is not handled in state {snapshot.state}",
                )
            outputs: dict[str, ExecutionResult] = dict(snapshot.outputs)
            if transition.plan_factory is not None:
                plan = await transition.plan_factory(
                    WorkflowContext(run_id, transition.target, outputs)
                )
                if plan is None:
                    raise WorkflowStateError(
                        "workflow.transition_rejected", "transition did not produce a plan"
                    )
                result = await (
                    await start_execution(
                        plan,
                        attempt=1,
                        cancellation_reason="state machine transition cancelled during start",
                    )
                ).wait()
                if result.status is not ExecutionStatus.SUCCEEDED:
                    raise WorkflowStateError(
                        "workflow.transition_failed",
                        result.error_message or f"transition action ended as {result.status.value}",
                    )
                outputs[transition.target] = result
            updated = replace(snapshot, state=transition.target, outputs=outputs)
            self._snapshots[run_id] = updated
            return updated

    def snapshot(self, run_id: str) -> StateMachineSnapshot:
        return self._snapshot(run_id)

    def _snapshot(self, run_id: str) -> StateMachineSnapshot:
        try:
            return self._snapshots[run_id]
        except KeyError as exc:
            raise WorkflowStateError("workflow.not_found", f"run {run_id} was not found") from exc


def _find_transition(
    definition: StateMachineDefinition,
    source: str,
    event: str,
) -> StateTransition | None:
    return next(
        (
            transition
            for transition in definition.transitions
            if transition.source == source and transition.event == event
        ),
        None,
    )

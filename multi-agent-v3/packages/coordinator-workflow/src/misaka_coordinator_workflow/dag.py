from __future__ import annotations

import asyncio

from misaka_coordinator_runtime import ExecutionHandle, ExecutionResult, ExecutionStatus

from misaka_coordinator_workflow.contracts import (
    DAGDefinition,
    DAGNode,
    WorkflowContext,
    WorkflowRunResult,
    WorkflowStatus,
)
from misaka_coordinator_workflow.errors import WorkflowStateError


class DAGCoordinator:
    def __init__(
        self,
        *,
        max_concurrency: int = 4,
        fail_fast: bool = True,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least one")
        self._max_concurrency = max_concurrency
        self._fail_fast = fail_fast
        self._active: dict[str, tuple[ExecutionHandle, asyncio.Task[ExecutionResult]]] = {}

    async def run(self, run_id: str, definition: DAGDefinition) -> WorkflowRunResult:
        if not run_id.strip():
            raise ValueError("run_id must not be empty")
        node_by_id = {node.node_id: node for node in definition.nodes}
        outputs: dict[str, ExecutionResult] = {}
        results: dict[str, ExecutionResult] = {}
        pending = set(node_by_id)
        semaphore = asyncio.Semaphore(self._max_concurrency)
        failure: tuple[WorkflowStatus, str] | None = None

        while pending:
            ready = {
                node_id
                for node_id in pending
                if set(node_by_id[node_id].depends_on) <= set(outputs)
            }
            if not ready:
                if failure is not None:
                    break
                raise WorkflowStateError("workflow.deadlock", "DAG has no executable node")
            tasks: dict[str, asyncio.Task[ExecutionResult]] = {
                node_id: asyncio.create_task(
                    self._run_node(run_id, node_by_id[node_id], results, semaphore)
                )
                for node_id in ready
            }
            task_to_node = {task: node_id for node_id, task in tasks.items()}
            remaining: set[asyncio.Task[ExecutionResult]] = set(tasks.values())
            while remaining:
                done, remaining = await asyncio.wait(
                    remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    node_id = task_to_node[task]
                    pending.remove(node_id)
                    try:
                        outcome = task.result()
                    except asyncio.CancelledError:
                        continue
                    except Exception as exc:
                        failure = (WorkflowStatus.RECONCILIATION_REQUIRED, str(exc))
                        continue
                    results[node_id] = outcome
                    if outcome.status is ExecutionStatus.SUCCEEDED:
                        outputs[node_id] = outcome
                        continue
                    if outcome.status is ExecutionStatus.CANCELLED:
                        failure = (
                            WorkflowStatus.CANCELLED,
                            outcome.error_message or "node cancelled",
                        )
                    elif outcome.status is ExecutionStatus.RECONCILIATION_REQUIRED:
                        failure = (
                            WorkflowStatus.RECONCILIATION_REQUIRED,
                            outcome.error_message or "node requires reconciliation",
                        )
                    else:
                        failure = (
                            WorkflowStatus.FAILED,
                            outcome.error_message or "node failed",
                        )
                if failure is not None and self._fail_fast:
                    for task in remaining:
                        task.cancel()
                    if remaining:
                        await asyncio.gather(*remaining, return_exceptions=True)
                    await self._cancel_active("DAG fail-fast")
                    remaining.clear()
                    break

        if failure is not None:
            return WorkflowRunResult(run_id, failure[0], results, str(failure[1]))
        return WorkflowRunResult(run_id, WorkflowStatus.SUCCEEDED, results)

    async def _run_node(
        self,
        run_id: str,
        node: DAGNode,
        results: dict[str, ExecutionResult],
        semaphore: asyncio.Semaphore,
    ) -> ExecutionResult:
        async with semaphore:
            context = WorkflowContext(run_id, node.node_id, results)
            plan = await node.plan_factory(context)
            if plan is None:
                raise WorkflowStateError(
                    "workflow.node_rejected", f"node {node.node_id} did not produce a plan"
                )
            handle = await plan.start(attempt=1)
            task = asyncio.create_task(handle.wait())
            self._active[node.node_id] = (handle, task)
            try:
                return await task
            except asyncio.CancelledError:
                await handle.cancel("DAG node execution cancelled")
                raise
            finally:
                self._active.pop(node.node_id, None)

    async def _cancel_active(self, reason: str) -> None:
        await asyncio.gather(
            *(handle.cancel(reason) for handle, _ in self._active.values()),
            return_exceptions=True,
        )

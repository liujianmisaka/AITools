from __future__ import annotations

import asyncio

from misaka_invocation_contracts import InvocationResult, InvocationStatus
from misaka_invocation_runtime import InvocationRuntime, RuntimeInvocationHandle

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
        runtime: InvocationRuntime,
        *,
        max_concurrency: int = 4,
        fail_fast: bool = True,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least one")
        self._runtime = runtime
        self._max_concurrency = max_concurrency
        self._fail_fast = fail_fast
        self._active: dict[str, tuple[RuntimeInvocationHandle, asyncio.Task[InvocationResult]]] = {}

    async def run(self, run_id: str, definition: DAGDefinition) -> WorkflowRunResult:
        if not run_id.strip():
            raise ValueError("run_id must not be empty")
        node_by_id = {node.node_id: node for node in definition.nodes}
        outputs: dict[str, InvocationResult] = {}
        results: dict[str, InvocationResult] = {}
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
            tasks = {
                asyncio.create_task(
                    self._run_node(
                        run_id,
                        node_by_id[node_id],
                        results,
                        semaphore,
                    )
                ): node_id
                for node_id in ready
            }
            node_results = await asyncio.gather(*tasks, return_exceptions=True)
            for task, node_id in tasks.items():
                pending.remove(node_id)
                outcome = node_results[list(tasks).index(task)]
                if isinstance(outcome, BaseException):
                    failure = (WorkflowStatus.RECONCILIATION_REQUIRED, str(outcome))
                    continue
                results[node_id] = outcome
                if outcome.status is InvocationStatus.SUCCEEDED:
                    outputs[node_id] = outcome
                    continue
                if outcome.status is InvocationStatus.CANCELLED:
                    failure = (WorkflowStatus.CANCELLED, outcome.error_message or "node cancelled")
                elif outcome.status is InvocationStatus.RECONCILIATION_REQUIRED:
                    failure = (
                        WorkflowStatus.RECONCILIATION_REQUIRED,
                        outcome.error_message or "node requires reconciliation",
                    )
                else:
                    failure = (WorkflowStatus.FAILED, outcome.error_message or "node failed")
            if failure is not None and self._fail_fast:
                await self._cancel_active("DAG fail-fast")
                break

        if failure is not None:
            return WorkflowRunResult(run_id, failure[0], results, failure[1])
        return WorkflowRunResult(run_id, WorkflowStatus.SUCCEEDED, results)

    async def _run_node(
        self,
        run_id: str,
        node: DAGNode,
        results: dict[str, InvocationResult],
        semaphore: asyncio.Semaphore,
    ) -> InvocationResult:
        async with semaphore:
            context = WorkflowContext(run_id, node.node_id, results)
            request = await node.request_factory(context)
            if request is None:
                raise WorkflowStateError(
                    "workflow.node_rejected", f"node {node.node_id} did not produce a request"
                )
            handle = await self._runtime.submit(request)
            task = asyncio.create_task(handle.wait())
            self._active[node.node_id] = (handle, task)
            try:
                return await task
            finally:
                self._active.pop(node.node_id, None)

    async def _cancel_active(self, reason: str) -> None:
        await asyncio.gather(
            *(handle.cancel(reason) for handle, _ in self._active.values()),
            return_exceptions=True,
        )

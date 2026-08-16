from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence

from pydantic import BaseModel

from multi_agent.domain.models import (
    EventKind,
    FailurePolicy,
    OrchestrationKind,
    ProviderEvent,
    TriggerEventInput,
    TaskInstanceStatus,
    TaskSpec,
    TERMINAL_TASK_INSTANCE_STATUSES,
    WorkflowDefinition,
    WorkflowInstanceStatus,
)
from multi_agent.orchestration.contracts import (
    OrchestrationModel,
    OrchestrationRuntimeContext,
    WorkItemSeed,
)


class DagOrchestrationModel(OrchestrationModel[WorkflowDefinition]):
    kind = OrchestrationKind.dag.value
    definition_schema_version = 1

    def parse_definition(
        self,
        value: BaseModel | Mapping[str, object],
    ) -> WorkflowDefinition:
        if isinstance(value, WorkflowDefinition):
            return value
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="python")
        return WorkflowDefinition.model_validate(value)

    def validate_definition(
        self,
        definition: WorkflowDefinition,
        *,
        validate_agent_task: Callable[[TaskSpec], None],
    ) -> None:
        for task in definition.tasks:
            validate_agent_task(task)

    def display_name(self, definition: WorkflowDefinition) -> str:
        return definition.name

    def definition_id(self, definition: WorkflowDefinition) -> str:
        return definition.id

    def definition_version(self, definition: WorkflowDefinition) -> int:
        return definition.version

    def with_definition_version(
        self,
        definition: WorkflowDefinition,
        version: int,
    ) -> WorkflowDefinition:
        return definition.model_copy(update={"version": version})

    def materialize_work_items(
        self,
        definition: WorkflowDefinition,
    ) -> Sequence[WorkItemSeed]:
        return [
            WorkItemSeed(
                logical_key=task.id,
                executor_kind="agent",
                spec=task.model_dump(mode="json"),
            )
            for task in definition.tasks
        ]

    async def run(
        self,
        definition: WorkflowDefinition,
        context: OrchestrationRuntimeContext,
    ) -> None:
        instance_id = context.instance_id
        store = context.store
        active: dict[str, asyncio.Task[None]] = {}
        instance_semaphore = asyncio.Semaphore(definition.max_concurrency)

        async def emit_status(new_status: WorkflowInstanceStatus) -> None:
            previous = store.get_instance(instance_id)
            store.set_instance_status(
                instance_id,
                new_status,
                internal_event=TriggerEventInput(
                    source_type="internal",
                    event_type="workflow.instance.status_changed",
                    event_version=1,
                    source_key=instance_id,
                    dedup_key=(
                        f"workflow-instance-status:{instance_id}:"
                        f"{new_status.value}:{previous['revision'] + 1}"
                    ),
                    payload={
                        "workflow_instance_id": instance_id,
                        "old_status": str(previous["status"]),
                        "new_status": new_status.value,
                        "revision": previous["revision"] + 1,
                        "error": previous["error"],
                    },
                ),
            )
            current = store.get_instance(instance_id)
            emit = context.emit_instance_status_changed
            if emit is None:
                return
            await emit(
                instance_id,
                str(previous["status"]),
                current["status"],
                int(current["revision"]),
                current["error"],
            )

        try:
            await emit_status(WorkflowInstanceStatus.running)
            while True:
                rows = {
                    row["logical_key"]: row
                    for row in store.list_work_items(instance_id)
                }
                failed_exists = any(
                    row["status"] == TaskInstanceStatus.failed.value
                    for row in rows.values()
                )

                for task_spec in definition.tasks:
                    row = rows[task_spec.id]
                    if row["status"] != TaskInstanceStatus.pending.value:
                        continue
                    dependency_statuses = [
                        rows[item]["status"] for item in task_spec.depends_on
                    ]
                    if any(
                        status
                        in {
                            TaskInstanceStatus.failed.value,
                            TaskInstanceStatus.cancelled.value,
                            TaskInstanceStatus.interrupted.value,
                            TaskInstanceStatus.blocked.value,
                        }
                        for status in dependency_statuses
                    ):
                        store.set_work_item_status(
                            instance_id,
                            task_spec.id,
                            TaskInstanceStatus.blocked,
                            error_code="dependency_failed",
                            error_message=(
                                "one or more dependencies did not succeed"
                            ),
                        )
                    elif all(
                        status == TaskInstanceStatus.succeeded.value
                        for status in dependency_statuses
                    ):
                        if (
                            definition.failure_policy == FailurePolicy.fail_fast
                            and failed_exists
                        ):
                            store.set_work_item_status(
                                instance_id,
                                task_spec.id,
                                TaskInstanceStatus.blocked,
                                error_code="fail_fast",
                                error_message=(
                                    "workflow stopped scheduling after a failure"
                                ),
                            )
                        else:
                            store.set_work_item_status(
                                instance_id,
                                task_spec.id,
                                TaskInstanceStatus.ready,
                            )

                rows = {
                    row["logical_key"]: row
                    for row in store.list_work_items(instance_id)
                }
                for task_spec in definition.tasks:
                    if rows[task_spec.id]["status"] != TaskInstanceStatus.ready.value:
                        continue
                    if task_spec.id in active:
                        continue
                    await instance_semaphore.acquire()
                    child = asyncio.create_task(
                        context.execute_agent_task(instance_id, task_spec),
                        name=f"multi-agent-task-{instance_id}-{task_spec.id}",
                    )
                    child.add_done_callback(
                        lambda _task, sem=instance_semaphore: sem.release()
                    )
                    active[task_spec.id] = child

                done_keys = [key for key, task in active.items() if task.done()]
                for key in done_keys:
                    task = active.pop(key)
                    try:
                        task.result()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        row = store.get_work_item(instance_id, key)
                        if (
                            TaskInstanceStatus(row["status"])
                            not in TERMINAL_TASK_INSTANCE_STATUSES
                        ):
                            store.set_work_item_status(
                                instance_id,
                                key,
                                TaskInstanceStatus.failed,
                                error_code="execution_kernel_error",
                                error_message=str(exc),
                            )
                            store.append_event(
                                instance_id=instance_id,
                                work_item_id=row["id"],
                                event=ProviderEvent(
                                    kind=EventKind.failed,
                                    summary=str(exc),
                                    payload={"code": "execution_kernel_error"},
                                    raw_event_type=(
                                        "orchestrator.work_item_failed"
                                    ),
                                ),
                            )

                rows = store.list_work_items(instance_id)
                if all(
                    TaskInstanceStatus(row["status"])
                    in TERMINAL_TASK_INSTANCE_STATUSES
                    for row in rows
                ):
                    break
                if active:
                    await asyncio.wait(
                        active.values(), return_when=asyncio.FIRST_COMPLETED
                    )
                else:
                    await asyncio.sleep(0)

            statuses = {
                TaskInstanceStatus(row["status"])
                for row in store.list_work_items(instance_id)
            }
            if (
                TaskInstanceStatus.failed in statuses
                or TaskInstanceStatus.blocked in statuses
            ):
                final_status = WorkflowInstanceStatus.failed
            elif TaskInstanceStatus.interrupted in statuses:
                final_status = WorkflowInstanceStatus.interrupted
            elif TaskInstanceStatus.cancelled in statuses:
                final_status = WorkflowInstanceStatus.cancelled
            else:
                final_status = WorkflowInstanceStatus.succeeded
            await emit_status(final_status)
            store.append_event(
                instance_id=instance_id,
                event=ProviderEvent(
                    kind=(
                        EventKind.completed
                        if final_status == WorkflowInstanceStatus.succeeded
                        else EventKind.failed
                    ),
                    summary=f"Workflow {final_status.value}",
                    payload={"status": final_status.value},
                    raw_event_type="orchestrator.instance_finished",
                ),
            )
        except asyncio.CancelledError:
            children = list(active.values())
            for child in children:
                child.cancel()
            if children:
                await asyncio.gather(*children, return_exceptions=True)
            terminal_status = (
                TaskInstanceStatus.interrupted
                if context.is_closing()
                else TaskInstanceStatus.cancelled
            )
            instance_status = (
                WorkflowInstanceStatus.interrupted
                if context.is_closing()
                else WorkflowInstanceStatus.cancelled
            )
            for row in store.list_work_items(instance_id):
                if (
                    TaskInstanceStatus(row["status"])
                    not in TERMINAL_TASK_INSTANCE_STATUSES
                ):
                    store.set_work_item_status(
                        instance_id,
                        row["logical_key"],
                        terminal_status,
                        activation_number=row["activation_number"],
                    )
            rejected = store.reject_pending_approvals_for_instance_with_ids(
                instance_id,
                decided_by="system:shutdown" if context.is_closing() else "system:cancel",
                reason=(
                    "execution was interrupted during service shutdown"
                    if context.is_closing()
                    else "execution was cancelled"
                ),
            )
            if context.emit_approval_updated is not None:
                for approval_id in rejected["approval_ids"]:
                    await context.emit_approval_updated(approval_id)
            await emit_status(instance_status)
            store.append_event(
                instance_id=instance_id,
                event=ProviderEvent(
                    kind=EventKind.cancelled,
                    summary=f"Workflow {instance_status.value}",
                    payload={"status": instance_status.value},
                    raw_event_type="orchestrator.instance_cancelled",
                ),
            )
            raise

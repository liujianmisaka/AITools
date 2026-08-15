from __future__ import annotations

from multi_agent.domain.models import TriggerEventInput
from multi_agent.storage.sqlite import SQLiteStore
from multi_agent.triggers.service import TriggerService


class InternalEventPublisher:
    """Publishes application-owned events into the durable trigger inbox.

    Publishing is best-effort by design: business state is committed first by
    the engine/store, so a notification failure must not roll back or hide the
    underlying workflow transition.
    """

    def __init__(self, *, store: SQLiteStore, triggers: TriggerService) -> None:
        self.store = store
        self.triggers = triggers

    async def workflow_instance_created(self, instance_id: str) -> None:
        instance = self.store.get_instance(instance_id)
        await self._publish(
            TriggerEventInput(
                source_type="internal",
                event_type="workflow.instance.created",
                event_version=1,
                source_key=instance_id,
                dedup_key=f"workflow-instance-created:{instance_id}",
                payload={
                    "workflow_instance_id": instance_id,
                    "template_id": instance["template_id"],
                    "template_version": instance["template_version"],
                    "source": instance["source"],
                    "kind": instance["kind"],
                    "cause_type": instance["cause_type"],
                    "status": instance["status"],
                    "revision": instance["revision"],
                    "trigger_binding_id": instance["trigger_binding_id"],
                    "trigger_event_id": instance["trigger_event_id"],
                },
            )
        )

    async def workflow_instance_status_changed(
        self,
        instance_id: str,
        *,
        old_status: str,
        new_status: str,
        revision: int,
        error: str | None = None,
    ) -> None:
        await self._publish(
            TriggerEventInput(
                source_type="internal",
                event_type="workflow.instance.status_changed",
                event_version=1,
                source_key=instance_id,
                dedup_key=(
                    f"workflow-instance-status:{instance_id}:"
                    f"{new_status}:{revision}"
                ),
                payload={
                    "workflow_instance_id": instance_id,
                    "old_status": old_status,
                    "new_status": new_status,
                    "revision": revision,
                    "error": error,
                },
            )
        )

    async def approval_updated(self, approval_id: str) -> None:
        approval = self.store.get_approval(approval_id)
        await self._publish(
            TriggerEventInput(
                source_type="internal",
                event_type="approval.updated",
                event_version=1,
                source_key=approval_id,
                dedup_key=f"approval-updated:{approval_id}:{approval['status']}",
                payload={
                    "approval_id": approval_id,
                    "workflow_instance_id": approval["workflow_instance_id"],
                    "work_item_id": approval["work_item_id"],
                    "status": approval["status"],
                    "decided_by": approval["decided_by"],
                    "reason": approval["reason"],
                },
            )
        )

    async def _publish(self, event: TriggerEventInput) -> None:
        try:
            await self.triggers.publish_internal(event)
        except Exception:
            # The application state transition that caused this event is
            # already durable and must not be undone by notification errors.
            return


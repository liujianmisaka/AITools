from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from misaka_persistence_contracts import DurableConflict
from misaka_persistence_jsonl import JsonlEventLog

from misaka_control_plane.models import TriggerSubmission


@dataclass(frozen=True, slots=True)
class TriggerRecord:
    definition: TriggerSubmission
    created_at: datetime


class JsonlTriggerRegistry:
    _STREAM = "control.triggers"

    def __init__(self, log: JsonlEventLog) -> None:
        self._log = log
        self._triggers: dict[str, TriggerRecord] = {}
        self._deliveries: dict[tuple[str, str], str] = {}
        self._loaded = False

    async def open(self) -> None:
        if self._loaded:
            return
        for event in await self._log.read(self._STREAM):
            if event.event_type == "trigger.created":
                definition = TriggerSubmission.model_validate(event.payload["definition"])
                self._triggers[definition.trigger_id] = TriggerRecord(
                    definition,
                    datetime.fromisoformat(str(event.payload["created_at"])),
                )
            elif event.event_type == "trigger.delivery":
                trigger_id = str(event.payload["trigger_id"])
                event_id = str(event.payload["event_id"])
                self._deliveries[(trigger_id, event_id)] = str(event.payload["instance_id"])
            else:
                raise DurableConflict("control.unknown_trigger_event", event.event_type)
        self._loaded = True

    async def register(self, definition: TriggerSubmission) -> TriggerRecord:
        await self.open()
        existing = self._triggers.get(definition.trigger_id)
        if existing is not None:
            if existing.definition.model_dump(mode="json") != definition.model_dump(mode="json"):
                raise DurableConflict("control.trigger_conflict", definition.trigger_id)
            return existing
        created_at = datetime.now(UTC)
        await self._log.append(
            self._STREAM,
            f"trigger-created:{definition.trigger_id}",
            "trigger.created",
            {
                "definition": definition.model_dump(mode="json"),
                "created_at": created_at.isoformat(),
            },
        )
        record = TriggerRecord(definition, created_at)
        self._triggers[definition.trigger_id] = record
        return record

    async def list(self) -> tuple[TriggerRecord, ...]:
        await self.open()
        return tuple(sorted(self._triggers.values(), key=lambda item: item.definition.trigger_id))

    async def matching(self, event_type: str) -> tuple[TriggerRecord, ...]:
        await self.open()
        return tuple(
            record
            for record in self._triggers.values()
            if record.definition.enabled and record.definition.event_type == event_type
        )

    async def delivery(self, trigger_id: str, event_id: str) -> str | None:
        await self.open()
        return self._deliveries.get((trigger_id, event_id))

    async def record_delivery(
        self,
        trigger_id: str,
        event_id: str,
        instance_id: str,
    ) -> str:
        await self.open()
        existing = self._deliveries.get((trigger_id, event_id))
        if existing is not None:
            if existing != instance_id:
                raise DurableConflict("control.trigger_delivery_conflict", event_id)
            return existing
        await self._log.append(
            self._STREAM,
            f"trigger-delivery:{trigger_id}:{event_id}",
            "trigger.delivery",
            {
                "trigger_id": trigger_id,
                "event_id": event_id,
                "instance_id": instance_id,
            },
        )
        self._deliveries[(trigger_id, event_id)] = instance_id
        return instance_id

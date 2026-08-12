from __future__ import annotations

import asyncio
from typing import Any, Protocol

from multi_agent.domain.errors import TriggerEventProcessingError
from multi_agent.domain.models import (
    TriggerBindingDefinition,
    TriggerConcurrencyPolicy,
    TriggerDeliveryStatus,
    TriggerEventInput,
    TriggerEventStatus,
)
from multi_agent.storage.sqlite import SQLiteStore
from multi_agent.triggers.events import EventTypeRegistry
from multi_agent.triggers.sources import EventSourceRegistry


class TriggerTarget(Protocol):
    async def instantiate_template(
        self,
        template_id: str,
        *,
        input_data: dict[str, Any] | None = None,
        trigger_binding_id: str | None = None,
        trigger_event_id: str | None = None,
    ) -> dict[str, Any]: ...


class TriggerService:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        sources: EventSourceRegistry,
        event_types: EventTypeRegistry,
        target: TriggerTarget,
    ) -> None:
        self.store = store
        self.sources = sources
        self.event_types = event_types
        self.target = target
        self._event_locks: dict[str, asyncio.Lock] = {}
        self._binding_locks: dict[str, asyncio.Lock] = {}
        self._poll_locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def recover_pending_deliveries(self) -> int:
        pending = self.store.list_pending_trigger_deliveries()
        event_ids = {delivery["trigger_event_id"] for delivery in pending}
        for event_id in event_ids:
            await self._process_event(event_id)
        return len(pending)

    def create_binding(
        self,
        binding: TriggerBindingDefinition,
    ) -> dict[str, Any]:
        self.event_types.validate_binding(
            source_type=binding.source_type,
            event_type=binding.event_type,
            event_version=binding.event_version,
        )
        self.sources.get(binding.source_type).validate_binding(binding)
        return self.store.create_trigger_binding(binding)

    def update_binding(
        self,
        binding_id: str,
        binding: TriggerBindingDefinition,
    ) -> dict[str, Any]:
        self.event_types.validate_binding(
            source_type=binding.source_type,
            event_type=binding.event_type,
            event_version=binding.event_version,
        )
        self.sources.get(binding.source_type).validate_binding(binding)
        return self.store.update_trigger_binding(binding_id, binding)

    def validate_poll_binding(self, binding_id: str) -> dict[str, Any]:
        binding = self.store.get_trigger_binding(binding_id)
        if not binding["enabled"]:
            raise TriggerEventProcessingError(
                f"trigger binding {binding_id!r} is disabled"
            )
        source = self.sources.get(binding["source_type"])
        if source.delivery_mode not in {"poll", "hybrid"}:
            raise TriggerEventProcessingError(
                f"event source {source.source_type!r} is not pollable"
            )
        return binding

    async def publish(self, event: TriggerEventInput) -> dict[str, Any]:
        source = self.sources.get(event.source_type)
        if source.delivery_mode not in {"push", "hybrid"}:
            raise TriggerEventProcessingError(
                f"event source {event.source_type!r} does not accept pushed events"
            )
        return await self._ingest(self.event_types.validate_event(event))

    async def _ingest(self, event: TriggerEventInput) -> dict[str, Any]:
        record, created = self.store.create_trigger_event(event)
        await self._process_event(record["id"])
        result = self.store.get_trigger_event(record["id"])
        return {
            **result,
            "deduplicated": not created,
            "deliveries": self.store.list_trigger_deliveries(record["id"]),
        }

    async def retry(self, event_id: str) -> dict[str, Any]:
        self.store.retry_trigger_event(event_id)
        await self._process_event(event_id)
        event = self.store.get_trigger_event(event_id)
        return {
            **event,
            "deliveries": self.store.list_trigger_deliveries(event_id),
        }

    async def poll_binding(self, binding_id: str) -> dict[str, Any]:
        binding = self.validate_poll_binding(binding_id)
        source = self.sources.get(binding["source_type"])
        state_key = binding["source_key"] or binding["id"]
        lock = self._poll_locks.setdefault(
            (source.source_type, state_key),
            asyncio.Lock(),
        )
        async with lock:
            state = self.store.get_trigger_source_state(
                source.source_type, state_key
            )
            result = await source.poll(
                binding,
                None if state is None else state["cursor"],
            )
            published = []
            for event in result.events:
                event = self.event_types.validate_event(event)
                if event.source_type != source.source_type:
                    raise TriggerEventProcessingError(
                        f"event source {source.source_type!r} emitted mismatched "
                        f"source_type {event.source_type!r}"
                    )
                if binding["source_key"] is not None and (
                    event.source_key != binding["source_key"]
                ):
                    raise TriggerEventProcessingError(
                        f"event source {source.source_type!r} emitted mismatched "
                        "source_key"
                    )
                published.append(await self._ingest(event))
            self.store.set_trigger_source_state(
                source.source_type,
                state_key,
                result.cursor,
            )
            return {"published": published, "cursor": result.cursor}

    async def _process_event(self, event_id: str) -> None:
        lock = self._event_locks.setdefault(event_id, asyncio.Lock())
        try:
            async with lock:
                event = self.store.get_trigger_event(event_id)
                bindings = self.store.list_matching_trigger_bindings(
                    source_type=event["source_type"],
                    event_type=event["event_type"],
                    event_version=event["event_version"],
                    source_key=event["source_key"],
                )
                for binding in bindings:
                    if self._matches_filter(
                        event["payload"], binding["event_filter"]
                    ):
                        self.store.create_trigger_delivery(
                            event_id=event_id,
                            binding_id=binding["id"],
                        )

                failures: list[str] = []
                deliveries = self.store.list_trigger_deliveries(event_id)
                for delivery in deliveries:
                    if delivery["status"] != TriggerDeliveryStatus.pending.value:
                        if delivery["status"] == TriggerDeliveryStatus.failed.value:
                            failures.append(delivery["error"] or "delivery failed")
                        continue
                    try:
                        await self._deliver(event, delivery)
                    except Exception as exc:
                        failures.append(str(exc))
                        self.store.finish_trigger_delivery(
                            delivery["id"],
                            TriggerDeliveryStatus.failed,
                            error=str(exc),
                        )
                self.store.set_trigger_event_status(
                    event_id,
                    (
                        TriggerEventStatus.failed
                        if failures
                        else TriggerEventStatus.processed
                    ),
                    error="; ".join(failures) if failures else None,
                )
        finally:
            self._event_locks.pop(event_id, None)

    async def _deliver(
        self,
        event: dict[str, Any],
        delivery: dict[str, Any],
    ) -> None:
        binding_id = delivery["trigger_binding_id"]
        lock = self._binding_locks.setdefault(binding_id, asyncio.Lock())
        async with lock:
            await self._deliver_locked(event, delivery)

    async def _deliver_locked(
        self,
        event: dict[str, Any],
        delivery: dict[str, Any],
    ) -> None:
        binding = delivery["binding_snapshot"]
        existing = self.store.find_instance_by_trigger(
            binding["id"], event["id"]
        )
        if existing is not None:
            self.store.finish_trigger_delivery(
                delivery["id"],
                TriggerDeliveryStatus.delivered,
                instance_id=existing["id"],
                reason="existing idempotent instance",
            )
            return
        if (
            binding["concurrency_policy"]
            == TriggerConcurrencyPolicy.skip_if_running.value
            and self.store.has_active_instance_for_template(binding["template_id"])
        ):
            self.store.finish_trigger_delivery(
                delivery["id"],
                TriggerDeliveryStatus.skipped,
                reason="template already has a queued or running instance",
            )
            return
        instance = await self.target.instantiate_template(
            binding["template_id"],
            input_data=self._map_input(
                event["payload"], binding["input_mapping"]
            ),
            trigger_binding_id=binding["id"],
            trigger_event_id=event["id"],
        )
        self.store.finish_trigger_delivery(
            delivery["id"],
            TriggerDeliveryStatus.delivered,
            instance_id=instance["id"],
        )

    @classmethod
    def _matches_filter(
        cls,
        payload: dict[str, Any],
        event_filter: dict[str, Any],
    ) -> bool:
        for path, expected in event_filter.items():
            try:
                actual = cls._resolve_path(payload, path)
            except TriggerEventProcessingError:
                return False
            if actual != expected:
                return False
        return True

    @classmethod
    def _map_input(
        cls,
        payload: dict[str, Any],
        mapping: dict[str, str],
    ) -> dict[str, Any]:
        if not mapping:
            return payload
        result: dict[str, Any] = {}
        for output_key, source_path in mapping.items():
            if source_path == "$":
                result[output_key] = payload
            else:
                result[output_key] = cls._resolve_path(payload, source_path)
        return result

    @staticmethod
    def _resolve_path(payload: dict[str, Any], path: str) -> Any:
        current: Any = payload
        normalized = path.removeprefix("payload.")
        if normalized == "payload" or normalized == "$":
            return payload
        for part in normalized.split("."):
            if not part or not isinstance(current, dict) or part not in current:
                raise TriggerEventProcessingError(
                    f"event payload path does not exist: {path}"
                )
            current = current[part]
        return current

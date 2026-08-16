from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Mapping
from typing import Any, Protocol

from multi_agent.domain.errors import (
    TriggerBindingConflictError,
    TriggerEventProcessingError,
    WebhookEndpointNotFoundError,
    WebhookPayloadError,
    WebhookSignatureError,
)
from multi_agent.domain.models import (
    TriggerBindingDefinition,
    TriggerConcurrencyPolicy,
    TriggerDeliveryStatus,
    TriggerEventInput,
    TriggerEventStatus,
    WebhookSourceConfig,
)
from multi_agent.storage.sqlite import SQLiteStore
from multi_agent.triggers.events import EventTypeRegistry
from multi_agent.triggers.sources import (
    EventSourceRegistry,
    WebhookEventSource,
)


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
        self._max_internal_cascade_depth = 1
        self._binding_locks: dict[str, asyncio.Lock] = {}
        self._binding_lock_owners: dict[str, asyncio.Task[Any]] = {}
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
        self._validate_binding_source(binding, exclude_id=binding.id)
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
        self._validate_binding_source(binding, exclude_id=binding_id)
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
        return await self.publish_with_trust(event, trusted=False)

    async def publish_internal(self, event: TriggerEventInput) -> dict[str, Any]:
        event = self.event_types.validate_event(event)
        source = self.sources.get(event.source_type)
        if source.delivery_mode not in {"push", "hybrid"}:
            raise TriggerEventProcessingError(
                f"event source {event.source_type!r} does not accept pushed events"
            )
        outbox = self.store.enqueue_internal_event(event)
        try:
            result = await self._ingest(event)
        except Exception as exc:
            self.store.mark_internal_event_failed(
                outbox["id"], str(exc)
            )
            raise
        self.store.mark_internal_event_published(outbox["id"])
        return result

    async def recover_internal_outbox(self) -> int:
        recovered = 0
        while True:
            batch = self.store.list_recoverable_internal_events(limit=500)
            if not batch:
                return recovered
            for outbox in batch:
                event = TriggerEventInput(
                    source_type=outbox["source_type"],
                    event_type=outbox["event_type"],
                    event_version=outbox["event_version"],
                    source_key=outbox["source_key"],
                    dedup_key=outbox["dedup_key"],
                    payload=outbox["payload"],
                )
                try:
                    await self._ingest(
                        self.event_types.validate_event(event)
                    )
                except Exception as exc:
                    self.store.mark_internal_event_failed(
                        outbox["id"], str(exc)
                    )
                    continue
                self.store.mark_internal_event_published(outbox["id"])
                recovered += 1

    async def publish_with_trust(
        self,
        event: TriggerEventInput,
        *,
        trusted: bool,
    ) -> dict[str, Any]:
        source = self.sources.get(event.source_type)
        if source.delivery_mode not in {"push", "hybrid"}:
            raise TriggerEventProcessingError(
                f"event source {event.source_type!r} does not accept pushed events"
            )
        if not trusted and not source.external_push_enabled:
            raise TriggerEventProcessingError(
                f"event source {event.source_type!r} only accepts internal "
                "application events"
            )
        return await self._ingest(self.event_types.validate_event(event))

    def _validate_binding_source(
        self,
        binding: TriggerBindingDefinition,
        *,
        exclude_id: str | None,
    ) -> None:
        source = self.sources.get(binding.source_type)
        source.validate_binding(binding)
        if source.unique_source_key and binding.source_key is not None:
            existing = self.store.find_trigger_binding_by_source_key(
                binding.source_type,
                binding.source_key,
                exclude_id=exclude_id,
            )
            if existing is not None:
                raise TriggerBindingConflictError(
                    f"event source {binding.source_type!r} already has an "
                    f"active binding for source_key {binding.source_key!r}"
                )

    def webhook_payload_limit(self, endpoint_key: str) -> int:
        binding = self.store.find_trigger_binding_by_source_key(
            "webhook", endpoint_key
        )
        if binding is None or not binding["enabled"]:
            raise WebhookEndpointNotFoundError(
                f"webhook endpoint not found: {endpoint_key}"
            )
        try:
            config = WebhookSourceConfig.model_validate(
                binding["source_config"]
            )
        except Exception as exc:
            raise WebhookPayloadError(
                f"invalid webhook binding configuration: {exc}"
            ) from exc
        return config.max_payload_bytes

    async def receive_webhook(
        self,
        endpoint_key: str,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
        client_ip: str | None,
    ) -> dict[str, Any]:
        binding = self.store.find_trigger_binding_by_source_key(
            "webhook", endpoint_key
        )
        if binding is None or not binding["enabled"]:
            raise WebhookEndpointNotFoundError(
                f"webhook endpoint not found: {endpoint_key}"
            )
        try:
            config = WebhookSourceConfig.model_validate(
                binding["source_config"]
            )
        except Exception as exc:
            raise WebhookPayloadError(
                f"invalid webhook binding configuration: {exc}"
            ) from exc
        source = self.sources.get("webhook")
        if not isinstance(source, WebhookEventSource):
            raise WebhookPayloadError("webhook source driver is misconfigured")
        try:
            secret = source.resolve_secret(config)
        except TriggerEventProcessingError as exc:
            raise WebhookSignatureError(str(exc)) from exc
        if not source.client_allowed(client_ip=client_ip, config=config):
            raise WebhookSignatureError(
                "webhook client IP is not allowed"
            )
        try:
            source.verify_signature(
                raw_body=raw_body,
                headers=headers,
                config=config,
                secret=secret,
            )
        except TriggerEventProcessingError as exc:
            raise WebhookSignatureError(str(exc)) from exc
        content_type = source.header_value(headers, "content-type")
        try:
            payload = source.payload_from_body(
                raw_body=raw_body,
                content_type=content_type,
                config=config,
            )
        except (TriggerEventProcessingError, UnicodeDecodeError) as exc:
            raise WebhookPayloadError(str(exc)) from exc
        header_value = (
            source.header_value(headers, config.dedup_header)
            if config.dedup_header
            else None
        )
        prefix = f"webhook:{endpoint_key}:"
        max_suffix_length = 500 - len(prefix)
        if header_value:
            suffix = header_value.strip()
            if len(suffix) > max_suffix_length:
                suffix = hashlib.sha256(
                    suffix.encode("utf-8")
                ).hexdigest()
        else:
            canonical_payload = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            payload_hash = hashlib.sha256(
                canonical_payload.encode("utf-8")
            ).hexdigest()
            if config.dedup_window_seconds > 0:
                bucket = int(time.time()) // config.dedup_window_seconds
                suffix = f"{payload_hash}:{bucket}"
            else:
                suffix = payload_hash
        dedup_key = prefix + suffix[:max_suffix_length]
        event = TriggerEventInput(
            source_type="webhook",
            event_type="webhook.received",
            event_version=1,
            source_key=endpoint_key,
            dedup_key=dedup_key,
            payload=payload,
        )
        return await self.publish(event)

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

                failures: list[tuple[str, str, str]] = []
                deliveries = self.store.list_trigger_deliveries(event_id)
                for delivery in deliveries:
                    if delivery["status"] != TriggerDeliveryStatus.pending.value:
                        if delivery["status"] == TriggerDeliveryStatus.failed.value:
                            failures.append(
                                (
                                    delivery["trigger_binding_id"],
                                    delivery["id"],
                                    delivery["error"] or "delivery failed",
                                )
                            )
                        continue
                    try:
                        await self._deliver(event, delivery)
                    except Exception as exc:
                        delivery_error = str(exc)
                        failures.append(
                            (
                                delivery["trigger_binding_id"],
                                delivery["id"],
                                delivery_error,
                            )
                        )
                        self.store.finish_trigger_delivery(
                            delivery["id"],
                            TriggerDeliveryStatus.failed,
                            error=delivery_error,
                            internal_event=TriggerEventInput(
                                source_type="internal",
                                event_type="trigger.delivery.failed",
                                event_version=1,
                                source_key=delivery["trigger_binding_id"],
                                dedup_key=(
                                    "trigger-delivery-failed:"
                                    f"{delivery['id']}"
                                ),
                                payload={
                                    "trigger_event_id": event_id,
                                    "trigger_binding_id": delivery[
                                        "trigger_binding_id"
                                    ],
                                    "delivery_id": delivery["id"],
                                    "error": delivery_error,
                                },
                            ),
                        )
                self.store.set_trigger_event_status(
                    event_id,
                    (
                        TriggerEventStatus.failed
                        if failures
                        else TriggerEventStatus.processed
                    ),
                    error=(
                        "; ".join(error for _, _, error in failures)
                        if failures
                        else None
                    ),
                )
                if event["event_type"] != "trigger.delivery.failed":
                    for binding_id, delivery_id, error in failures:
                        await self._publish_delivery_failure(
                            trigger_event_id=event_id,
                            trigger_binding_id=binding_id,
                            delivery_id=delivery_id,
                            error=error,
                        )
        finally:
            self._event_locks.pop(event_id, None)

    async def _deliver(
        self,
        event: dict[str, Any],
        delivery: dict[str, Any],
    ) -> None:
        binding_id = delivery["trigger_binding_id"]
        current = asyncio.current_task()
        if current is not None and self._binding_lock_owners.get(binding_id) is current:
            # A delivery can synchronously create another workflow whose
            # internal events match the same binding. asyncio.Lock is not
            # reentrant, so the owner task is allowed to continue directly;
            # the loop guard still terminates self-triggering chains.
            await self._deliver_locked(event, delivery)
            return
        lock = self._binding_locks.setdefault(binding_id, asyncio.Lock())
        async with lock:
            if current is not None:
                self._binding_lock_owners[binding_id] = current
            try:
                await self._deliver_locked(event, delivery)
            finally:
                self._binding_lock_owners.pop(binding_id, None)

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
        loop_reason = self._internal_loop_guard(event, binding)
        if loop_reason is not None:
            self.store.finish_trigger_delivery(
                delivery["id"],
                TriggerDeliveryStatus.skipped,
                reason=loop_reason,
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

    def _internal_loop_guard(
        self,
        event: dict[str, Any],
        binding: dict[str, Any],
    ) -> str | None:
        if event["source_type"] != "internal":
            return None
        source_instance_id = event["payload"].get("workflow_instance_id")
        if source_instance_id:
            try:
                source_instance = self.store.get_instance(source_instance_id)
            except Exception:
                source_instance = None
            if (
                source_instance is not None
                and source_instance.get("template_id") == binding["template_id"]
            ):
                return "internal_self_trigger_prevented"
        cascade_depth = self._internal_cascade_depth(event)
        if cascade_depth > self._max_internal_cascade_depth:
            return (
                "max_internal_cascade_depth_exceeded: "
                f"{cascade_depth} > {self._max_internal_cascade_depth}"
            )
        return None

    def _internal_cascade_depth(self, event: dict[str, Any]) -> int:
        workflow_instance_id = event["payload"].get("workflow_instance_id")
        if not isinstance(workflow_instance_id, str):
            return 0
        depth = 0
        current_id: str | None = workflow_instance_id
        seen: set[str] = set()
        while current_id is not None and current_id not in seen:
            seen.add(current_id)
            try:
                instance = self.store.get_instance(current_id)
            except Exception:
                break
            cause_event_id = instance.get("trigger_event_id")
            if not cause_event_id:
                break
            try:
                cause = self.store.get_trigger_event(cause_event_id)
            except Exception:
                break
            if cause.get("source_type") == "internal":
                depth += 1
            cause_instance_id = cause.get("payload", {}).get(
                "workflow_instance_id"
            )
            if not isinstance(cause_instance_id, str):
                break
            current_id = cause_instance_id
        return depth

    async def _publish_delivery_failure(
        self,
        *,
        trigger_event_id: str,
        trigger_binding_id: str,
        delivery_id: str,
        error: str,
    ) -> None:
        try:
            await self.publish_internal(
                TriggerEventInput(
                    source_type="internal",
                    event_type="trigger.delivery.failed",
                    event_version=1,
                    source_key=trigger_binding_id,
                    dedup_key=f"trigger-delivery-failed:{delivery_id}",
                    payload={
                        "trigger_event_id": trigger_event_id,
                        "trigger_binding_id": trigger_binding_id,
                        "delivery_id": delivery_id,
                        "error": error or "delivery failed",
                    },
                )
            )
        except Exception:
            # The original delivery failure is already durable; an internal
            # notification failure must not mask the primary event outcome.
            return

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

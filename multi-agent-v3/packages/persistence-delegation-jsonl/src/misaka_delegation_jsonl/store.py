from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

from misaka_delegation_capability import DelegationConflict, DelegationNotFound
from misaka_delegation_contracts import (
    DelegationMode,
    DelegationRef,
    DelegationReport,
    DelegationRequest,
    DelegationSnapshot,
    DelegationStatus,
)
from misaka_delegation_runtime.store import MemoryDelegationStore
from misaka_interaction_contracts import PrincipalKind, PrincipalRef, ScopeRef
from misaka_kernel_contracts import JsonObject
from misaka_persistence_contracts import DurableCorruption
from misaka_persistence_jsonl import JsonlEventLog


class JsonlDelegationStore:
    """Rebuildable Delegation facts backed by the generic JSONL event log."""

    _STREAM_PREFIX = "delegation:"

    def __init__(self, log: JsonlEventLog) -> None:
        self._log = log
        self._memory = MemoryDelegationStore()
        self._lock = asyncio.Lock()
        self._opened = False

    async def open(self) -> None:
        async with self._lock:
            if self._opened:
                return
            try:
                for event in await self._log.all_events():
                    if not event.stream_id.startswith(self._STREAM_PREFIX):
                        continue
                    await self._apply_event(event.stream_id, event.event_type, event.payload)
            except DurableCorruption:
                raise
            except Exception as exc:
                raise DurableCorruption(
                    "delegation.invalid_fact",
                    "delegation JSONL facts have an invalid shape or transition",
                ) from exc
            self._opened = True

    async def create(
        self, request: DelegationRequest, ref: DelegationRef
    ) -> tuple[DelegationSnapshot, bool]:
        await self.open()
        async with self._lock:
            try:
                existing = await self._memory.snapshot(request.delegation_id)
            except DelegationNotFound:
                existing = None
            if existing is not None:
                if existing.request != request or existing.ref != ref:
                    raise DelegationConflict(
                        "delegation.id_conflict",
                        f"delegation {request.delegation_id} has different facts",
                    )
                return existing, False
            created_at = datetime.now(UTC)
            await self._log.append(
                self._stream(request.delegation_id),
                "delegation-created",
                "delegation.created",
                {"request": _encode_request(request), "ref": _encode_ref(ref)},
                occurred_at=created_at,
            )
            return await self._memory.create(request, ref)

    async def snapshot(self, delegation_id: str) -> DelegationSnapshot:
        await self.open()
        return await self._memory.snapshot(delegation_id)

    async def bind_ref(self, delegation_id: str, ref: DelegationRef) -> DelegationSnapshot:
        await self.open()
        async with self._lock:
            current = await self._memory.snapshot(delegation_id)
            if current.ref == ref:
                return current
            await self._log.append(
                self._stream(delegation_id),
                f"ref-bound:{ref.session_id}:{ref.channel_id}",
                "delegation.ref_bound",
                {"ref": _encode_ref(ref)},
            )
            return await self._memory.bind_ref(delegation_id, ref)

    async def claim_continuation(
        self,
        delegation_id: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> bool:
        await self.open()
        async with self._lock:
            claimed = await self._memory.claim_continuation(
                delegation_id,
                idempotency_key,
                fingerprint,
            )
            if not claimed:
                return False
            await self._log.append(
                self._stream(delegation_id),
                f"continuation:{idempotency_key}",
                "delegation.continuation_claimed",
                {"idempotency_key": idempotency_key, "fingerprint": fingerprint},
            )
            return True

    async def activate(
        self,
        delegation_id: str,
        invocation_id: str,
        activation_id: str,
    ) -> DelegationSnapshot:
        await self.open()
        async with self._lock:
            await self._log.append(
                self._stream(delegation_id),
                f"activation:{activation_id}",
                "delegation.activation_started",
                {
                    "invocation_id": invocation_id,
                    "activation_id": activation_id,
                },
            )
            return await self._memory.activate(
                delegation_id,
                invocation_id,
                activation_id,
            )

    async def finalize(self, delegation_id: str, report: DelegationReport) -> DelegationSnapshot:
        await self.open()
        async with self._lock:
            current = await self._memory.snapshot(delegation_id)
            if current.report == report:
                return current
            await self._log.append(
                self._stream(delegation_id),
                f"report:{len(current.report_history) + 1}",
                "delegation.finalized",
                {"report": _encode_report(report)},
                occurred_at=report.created_at,
            )
            return await self._memory.finalize(delegation_id, report)

    async def wait_terminal(self, delegation_id: str) -> DelegationReport:
        await self.open()
        return await self._memory.wait_terminal(delegation_id)

    @classmethod
    def _stream(cls, delegation_id: str) -> str:
        if not delegation_id.strip():
            raise ValueError("delegation_id must not be empty")
        return f"{cls._STREAM_PREFIX}{delegation_id}"

    async def _apply_event(
        self,
        stream_id: str,
        event_type: str,
        payload: JsonObject,
    ) -> None:
        delegation_id = stream_id.removeprefix(self._STREAM_PREFIX)
        if not delegation_id.strip():
            raise DurableCorruption(
                "delegation.id_empty",
                "delegation event stream has an empty delegation id",
            )
        if event_type == "delegation.created":
            request = _decode_request(_required_object(payload, "request"))
            ref = _decode_ref(_required_object(payload, "ref"))
            if request.delegation_id != delegation_id or ref.delegation_id != delegation_id:
                raise DurableCorruption(
                    "delegation.id_mismatch",
                    "delegation creation fact does not match its stream",
                )
            try:
                await self._memory.snapshot(delegation_id)
            except DelegationNotFound:
                await self._memory.create(request, ref)
                return
            raise DurableCorruption(
                "delegation.duplicate_creation",
                f"delegation {delegation_id} has duplicate creation facts",
            )
        if event_type == "delegation.ref_bound":
            ref = _decode_ref(_required_object(payload, "ref"))
            if ref.delegation_id != delegation_id:
                raise DurableCorruption(
                    "delegation.ref_id_mismatch",
                    "delegation ref fact does not match its stream",
                )
            await self._memory.bind_ref(delegation_id, ref)
            return
        if event_type == "delegation.continuation_claimed":
            await self._memory.claim_continuation(
                delegation_id,
                _required_string(payload, "idempotency_key"),
                _required_string(payload, "fingerprint"),
            )
            return
        if event_type == "delegation.activation_started":
            await self._memory.activate(
                delegation_id,
                _required_string(payload, "invocation_id"),
                _required_string(payload, "activation_id"),
            )
            return
        if event_type == "delegation.finalized":
            report = _decode_report(_required_object(payload, "report"))
            if report.delegation_id != delegation_id:
                raise DurableCorruption(
                    "delegation.report_id_mismatch",
                    "delegation report fact does not match its stream",
                )
            current = await self._memory.snapshot(delegation_id)
            if current.report == report:
                raise DurableCorruption(
                    "delegation.duplicate_report",
                    f"delegation {delegation_id} has duplicate report facts",
                )
            await self._memory.finalize(delegation_id, report)
            return
        raise DurableCorruption(
            "delegation.event_type_unknown",
            f"unknown delegation event type {event_type}",
        )


def _encode_request(request: DelegationRequest) -> JsonObject:
    return {
        "delegation_id": request.delegation_id,
        "idempotency_key": request.idempotency_key,
        "initiator": _encode_principal(request.initiator),
        "controller": _encode_principal(request.controller),
        "scope": _encode_scope(request.scope),
        "capability_id": request.capability_id,
        "operation": request.operation,
        "input": request.input,
        "provider_id": request.provider_id,
        "model": request.model,
        "effort": request.effort,
        "output_schema": request.output_schema,
        "mode": request.mode.value,
        "parent_delegation_id": request.parent_delegation_id,
        "session_id": request.session_id,
        "channel_id": request.channel_id,
        "decision_ref": (
            {
                "proposal_id": request.decision_ref.proposal_id,
                "revision": request.decision_ref.revision,
            }
            if request.decision_ref is not None
            else None
        ),
        "required_features": list(request.required_features),
        "constraints": request.constraints,
    }


def _decode_request(payload: JsonObject) -> DelegationRequest:
    decision_payload = payload.get("decision_ref")
    decision_ref = None
    if decision_payload is not None:
        decision_object = _required_object_value(decision_payload, "decision_ref")
        from misaka_interaction_contracts import DecisionRef

        decision_ref = DecisionRef(
            _required_string(decision_object, "proposal_id"),
            _required_int(decision_object, "revision"),
        )
    return DelegationRequest(
        delegation_id=_required_string(payload, "delegation_id"),
        idempotency_key=_required_string(payload, "idempotency_key"),
        initiator=_decode_principal(_required_object(payload, "initiator")),
        controller=_decode_principal(_required_object(payload, "controller")),
        scope=_decode_scope(_required_object(payload, "scope")),
        capability_id=_required_string(payload, "capability_id"),
        operation=_required_string(payload, "operation"),
        input=_required_object(payload, "input"),
        provider_id=_optional_string(payload.get("provider_id")),
        model=_optional_string(payload.get("model")),
        effort=_optional_string(payload.get("effort")),
        output_schema=(
            _required_object(payload, "output_schema")
            if payload.get("output_schema") is not None
            else None
        ),
        mode=DelegationMode(_required_string(payload, "mode")),
        parent_delegation_id=_optional_string(payload.get("parent_delegation_id")),
        session_id=_optional_string(payload.get("session_id")),
        channel_id=_optional_string(payload.get("channel_id")),
        decision_ref=decision_ref,
        required_features=frozenset(_required_string_list(payload, "required_features")),
        constraints=_required_object(payload, "constraints"),
    )


def _encode_ref(ref: DelegationRef) -> JsonObject:
    return {
        "delegation_id": ref.delegation_id,
        "session_id": ref.session_id,
        "channel_id": ref.channel_id,
        "parent_delegation_id": ref.parent_delegation_id,
    }


def _decode_ref(payload: JsonObject) -> DelegationRef:
    return DelegationRef(
        delegation_id=_required_string(payload, "delegation_id"),
        session_id=_optional_string(payload.get("session_id")),
        channel_id=_optional_string(payload.get("channel_id")),
        parent_delegation_id=_optional_string(payload.get("parent_delegation_id")),
    )


def _encode_report(report: DelegationReport) -> JsonObject:
    return {
        "delegation_id": report.delegation_id,
        "status": report.status.value,
        "output": report.output,
        "artifact_ids": list(report.artifact_ids),
        "error_code": report.error_code,
        "error_message": report.error_message,
        "source_invocation_id": report.source_invocation_id,
        "source_activation_id": report.source_activation_id,
        "created_at": report.created_at.isoformat(),
    }


def _decode_report(payload: JsonObject) -> DelegationReport:
    return DelegationReport(
        delegation_id=_required_string(payload, "delegation_id"),
        status=DelegationStatus(_required_string(payload, "status")),
        output=payload.get("output"),
        artifact_ids=tuple(_required_string_list(payload, "artifact_ids")),
        error_code=_optional_string(payload.get("error_code")),
        error_message=_optional_string(payload.get("error_message")),
        source_invocation_id=_optional_string(payload.get("source_invocation_id")),
        source_activation_id=_optional_string(payload.get("source_activation_id")),
        created_at=datetime.fromisoformat(_required_string(payload, "created_at")),
    )


def _encode_principal(principal: PrincipalRef) -> JsonObject:
    return {
        "principal_id": principal.principal_id,
        "kind": principal.kind.value,
        "display_name": principal.display_name,
    }


def _decode_principal(payload: JsonObject) -> PrincipalRef:
    return PrincipalRef(
        principal_id=_required_string(payload, "principal_id"),
        kind=PrincipalKind(_required_string(payload, "kind")),
        display_name=_optional_string(payload.get("display_name")) or "",
    )


def _encode_scope(scope: ScopeRef) -> JsonObject:
    return {"scope_id": scope.scope_id, "parent_scope_id": scope.parent_scope_id}


def _decode_scope(payload: JsonObject) -> ScopeRef:
    return ScopeRef(
        scope_id=_required_string(payload, "scope_id"),
        parent_scope_id=_optional_string(payload.get("parent_scope_id")),
    )


def _required_object(payload: JsonObject, name: str) -> JsonObject:
    value = payload.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return cast(JsonObject, value)


def _required_object_value(value: object, name: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return cast(JsonObject, value)


def _required_string(payload: JsonObject, name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("optional string field has an invalid value")
    return value


def _required_string_list(payload: JsonObject, name: str) -> list[str]:
    value = payload.get(name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a string array")
    return cast(list[str], value)


def _required_int(payload: JsonObject, name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value

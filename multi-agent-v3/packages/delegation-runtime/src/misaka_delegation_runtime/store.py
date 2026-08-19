from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass

from misaka_delegation_capability import (
    DelegationConflict,
    DelegationNotFound,
    DelegationStateError,
)
from misaka_delegation_contracts import (
    DelegationMode,
    DelegationRef,
    DelegationReport,
    DelegationRequest,
    DelegationSnapshot,
    DelegationStatus,
)
from misaka_interaction_contracts import PrincipalRef, ScopeRef
from misaka_kernel_contracts import JsonObject, JsonValue


@dataclass(slots=True)
class _StoredDelegation:
    request: DelegationRequest
    ref: DelegationRef
    fingerprint: str
    status: DelegationStatus
    revision: int
    child_refs: tuple[DelegationRef, ...]
    report: DelegationReport | None
    report_history: tuple[DelegationReport, ...]
    current_invocation_id: str | None
    current_activation_id: str | None
    activation_count: int
    condition: asyncio.Condition


class MemoryDelegationStore:
    """Concurrency-safe delegation facts for the local profile."""

    def __init__(self) -> None:
        self._records: dict[str, _StoredDelegation] = {}
        self._idempotency: dict[str, str] = {}
        self._continuations: dict[str, tuple[str, str]] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        request: DelegationRequest,
        ref: DelegationRef,
    ) -> tuple[DelegationSnapshot, bool]:
        fingerprint = delegation_request_fingerprint(request)
        async with self._lock:
            existing_id = self._idempotency.get(request.idempotency_key)
            if existing_id is not None:
                existing = self._records[existing_id]
                if (
                    existing.fingerprint != fingerprint
                    or existing.ref.delegation_id != ref.delegation_id
                ):
                    raise DelegationConflict(
                        "delegation.idempotency_conflict",
                        f"idempotency key {request.idempotency_key} has a different delegation",
                    )
                return _snapshot(existing), False
            existing = self._records.get(request.delegation_id)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise DelegationConflict(
                        "delegation.id_conflict",
                        f"delegation {request.delegation_id} has a different request",
                    )
                return _snapshot(existing), False
            record = _StoredDelegation(
                request=request,
                ref=ref,
                fingerprint=fingerprint,
                status=DelegationStatus.PROPOSED,
                revision=1,
                child_refs=(),
                report=None,
                report_history=(),
                current_invocation_id=None,
                current_activation_id=None,
                activation_count=0,
                condition=asyncio.Condition(),
            )
            self._records[request.delegation_id] = record
            self._idempotency[request.idempotency_key] = request.delegation_id
            return _snapshot(record), True

    async def snapshot(self, delegation_id: str) -> DelegationSnapshot:
        record = self._record(delegation_id)
        async with record.condition:
            return _snapshot(record)

    async def bind_ref(self, delegation_id: str, ref: DelegationRef) -> DelegationSnapshot:
        record = self._record(delegation_id)
        async with record.condition:
            if record.ref.session_id not in (None, ref.session_id):
                raise DelegationConflict(
                    "delegation.session_conflict",
                    f"delegation {delegation_id} is bound to another session",
                )
            if record.ref.channel_id not in (None, ref.channel_id):
                raise DelegationConflict(
                    "delegation.channel_conflict",
                    f"delegation {delegation_id} is bound to another channel",
                )
            if record.ref == ref:
                return _snapshot(record)
            record.ref = ref
            record.revision += 1
            record.condition.notify_all()
            return _snapshot(record)

    async def claim_continuation(
        self,
        delegation_id: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> bool:
        if not idempotency_key.strip():
            raise ValueError("continuation idempotency key must not be empty")
        self._record(delegation_id)
        async with self._lock:
            existing = self._continuations.get(idempotency_key)
            if existing is not None:
                existing_delegation, existing_fingerprint = existing
                if existing_delegation != delegation_id or existing_fingerprint != fingerprint:
                    raise DelegationConflict(
                        "delegation.continuation_conflict",
                        f"continuation key {idempotency_key} has a different request",
                    )
                return False
            self._continuations[idempotency_key] = (delegation_id, fingerprint)
            return True

    async def activate(
        self,
        delegation_id: str,
        invocation_id: str,
        activation_id: str,
    ) -> DelegationSnapshot:
        if not invocation_id.strip():
            raise ValueError("invocation_id must not be empty")
        if not activation_id.strip():
            raise ValueError("activation_id must not be empty")
        if invocation_id == activation_id:
            raise ValueError("invocation_id and activation_id must be distinct")
        record = self._record(delegation_id)
        async with record.condition:
            if record.current_invocation_id is not None or record.current_activation_id is not None:
                raise DelegationStateError(
                    "delegation.activation_active",
                    f"delegation {delegation_id} already has a live activation",
                )
            if record.report is not None and record.request.mode is DelegationMode.ONE_SHOT:
                raise DelegationStateError(
                    "delegation.one_shot_terminal",
                    f"one-shot delegation {delegation_id} is already terminal",
                )
            if record.status not in {
                DelegationStatus.PROPOSED,
                DelegationStatus.ADMITTED,
                DelegationStatus.PREPARING,
                DelegationStatus.COMPLETED,
                DelegationStatus.FAILED,
                DelegationStatus.CANCELLED,
            }:
                raise DelegationStateError(
                    "delegation.activation_state_invalid",
                    f"delegation {delegation_id} cannot activate from {record.status.value}",
                )
            record.status = DelegationStatus.ACTIVE
            if record.report is not None:
                record.report = None
            record.current_invocation_id = invocation_id
            record.current_activation_id = activation_id
            record.activation_count += 1
            record.revision += 1
            record.condition.notify_all()
            return _snapshot(record)

    async def finalize(self, delegation_id: str, report: DelegationReport) -> DelegationSnapshot:
        record = self._record(delegation_id)
        if report.delegation_id != delegation_id:
            raise DelegationConflict(
                "delegation.report_id_mismatch",
                "delegation report belongs to another delegation",
            )
        async with record.condition:
            if record.report is not None:
                if record.report != report:
                    raise DelegationConflict(
                        "delegation.terminal_conflict",
                        f"delegation {delegation_id} has a different report",
                    )
                return _snapshot(record)
            if record.status not in {
                DelegationStatus.PROPOSED,
                DelegationStatus.ADMITTED,
                DelegationStatus.PREPARING,
                DelegationStatus.ACTIVE,
                DelegationStatus.WAITING_INPUT,
                DelegationStatus.RECONCILING,
                DelegationStatus.RECONCILIATION_REQUIRED,
            }:
                raise DelegationStateError(
                    "delegation.finalize_state_invalid",
                    f"delegation {delegation_id} cannot finalize from {record.status.value}",
                )
            if record.status in {
                DelegationStatus.PROPOSED,
                DelegationStatus.ADMITTED,
                DelegationStatus.PREPARING,
            } and report.status not in {
                DelegationStatus.REJECTED,
                DelegationStatus.FAILED,
                DelegationStatus.RECONCILIATION_REQUIRED,
            }:
                raise DelegationStateError(
                    "delegation.pre_activation_report_invalid",
                    "pre-activation delegation can only be rejected or failed",
                )
            if record.current_invocation_id is not None:
                if (
                    report.source_invocation_id != record.current_invocation_id
                    or report.source_activation_id != record.current_activation_id
                ):
                    raise DelegationConflict(
                        "delegation.report_execution_identity_mismatch",
                        "delegation report belongs to another invocation or activation",
                    )
            record.status = report.status
            record.report = report
            record.report_history += (report,)
            record.current_invocation_id = None
            record.current_activation_id = None
            record.revision += 1
            record.condition.notify_all()
            return _snapshot(record)

    async def wait_terminal(self, delegation_id: str) -> DelegationReport:
        record = self._record(delegation_id)
        async with record.condition:
            while record.report is None:
                await record.condition.wait()
            return record.report

    def _record(self, delegation_id: str) -> _StoredDelegation:
        if not delegation_id.strip():
            raise ValueError("delegation_id must not be empty")
        try:
            return self._records[delegation_id]
        except KeyError as exc:
            raise DelegationNotFound(
                "delegation.not_found",
                f"delegation {delegation_id} was not found",
            ) from exc


def _snapshot(record: _StoredDelegation) -> DelegationSnapshot:
    return DelegationSnapshot(
        ref=record.ref,
        request=record.request,
        status=record.status,
        revision=record.revision,
        child_refs=record.child_refs,
        report=record.report,
        report_history=record.report_history,
        current_invocation_id=record.current_invocation_id,
        current_activation_id=record.current_activation_id,
        activation_count=record.activation_count,
    )


def delegation_request_fingerprint(request: DelegationRequest) -> str:
    payload: JsonObject = {
        "delegation_id": request.delegation_id,
        "idempotency_key": request.idempotency_key,
        "initiator": _principal_payload(request.initiator),
        "controller": _principal_payload(request.controller),
        "scope": _scope_payload(request.scope),
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
        "required_features": list[JsonValue](sorted(request.required_features)),
        "constraints": request.constraints,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _principal_payload(principal: PrincipalRef) -> JsonObject:
    return {
        "principal_id": principal.principal_id,
        "kind": principal.kind.value,
        "display_name": principal.display_name,
    }


def _scope_payload(scope: ScopeRef) -> JsonObject:
    return {"scope_id": scope.scope_id, "parent_scope_id": scope.parent_scope_id}

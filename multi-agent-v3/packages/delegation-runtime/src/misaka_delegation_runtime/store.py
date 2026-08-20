from __future__ import annotations

import asyncio
from dataclasses import dataclass

from misaka_delegation_capability import (
    DelegationCapabilityRejected,
    DelegationConflict,
    DelegationNotFound,
    DelegationStateError,
)
from misaka_delegation_contracts import (
    DelegationAdmission,
    DelegationIntent,
    DelegationMode,
    DelegationRef,
    DelegationReport,
    DelegationRequest,
    DelegationSnapshot,
    DelegationStatus,
    delegation_request_fingerprint,
)


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
    admission: DelegationAdmission | None
    condition: asyncio.Condition


class MemoryDelegationStore:
    """Concurrency-safe delegation facts for the local profile."""

    def __init__(self) -> None:
        self._records: dict[str, _StoredDelegation] = {}
        self._idempotency: dict[str, str] = {}
        self._continuations: dict[tuple[str, str], str] = {}
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
                admission=None,
                condition=asyncio.Condition(),
            )
            self._records[request.delegation_id] = record
            self._idempotency[request.idempotency_key] = request.delegation_id
            return _snapshot(record), True

    async def snapshot(self, delegation_id: str) -> DelegationSnapshot:
        record = self._record(delegation_id)
        async with record.condition:
            return _snapshot(record)

    async def record_admission(
        self, delegation_id: str, admission: DelegationAdmission
    ) -> DelegationSnapshot:
        record = self._record(delegation_id)
        async with record.condition:
            if record.admission is not None:
                if record.admission != admission:
                    raise DelegationConflict(
                        "delegation.admission_conflict",
                        f"delegation {delegation_id} has a different admission",
                    )
                return _snapshot(record)
            if record.status is not DelegationStatus.PROPOSED:
                raise DelegationStateError(
                    "delegation.admission_state_invalid",
                    f"delegation {delegation_id} cannot be admitted from {record.status.value}",
                )
            record.admission = admission
            if admission.allowed:
                record.status = DelegationStatus.ADMITTED
            record.revision += 1
            record.condition.notify_all()
            return _snapshot(record)

    async def attach_child(
        self, parent_delegation_id: str, child_ref: DelegationRef
    ) -> DelegationSnapshot:
        record = self._record(parent_delegation_id)
        if child_ref.delegation_id == parent_delegation_id:
            raise DelegationConflict(
                "delegation.self_child",
                "a delegation cannot attach itself as a child",
            )
        if child_ref.parent_delegation_id != parent_delegation_id:
            raise DelegationConflict(
                "delegation.child_parent_mismatch",
                "child reference does not point to the parent delegation",
            )
        async with record.condition:
            existing = next(
                (ref for ref in record.child_refs if ref.delegation_id == child_ref.delegation_id),
                None,
            )
            if existing is not None:
                if existing != child_ref:
                    raise DelegationConflict(
                        "delegation.child_conflict",
                        f"child {child_ref.delegation_id} has different ownership facts",
                    )
                return _snapshot(record)
            if record.report is not None:
                raise DelegationStateError(
                    "delegation.parent_terminal",
                    f"delegation {parent_delegation_id} is already terminal",
                )
            if len(record.child_refs) >= record.request.policy.budget.fan_out_limit:
                raise DelegationCapabilityRejected(
                    "delegation.fan_out_exceeded",
                    f"parent delegation {parent_delegation_id} exceeded its fan-out budget",
                )
            live_statuses = {
                DelegationStatus.ADMITTED,
                DelegationStatus.PREPARING,
                DelegationStatus.ACTIVE,
                DelegationStatus.WAITING_INPUT,
                DelegationStatus.RECONCILING,
            }
            live_children = sum(
                1
                for ref in record.child_refs
                if (child := self._records.get(ref.delegation_id)) is not None
                and child.status in live_statuses
            )
            if live_children >= record.request.policy.budget.max_concurrent_children:
                raise DelegationCapabilityRejected(
                    "delegation.concurrent_children_exceeded",
                    (
                        f"parent delegation {parent_delegation_id} exceeded its "
                        "concurrent child budget"
                    ),
                )
            record.child_refs += (child_ref,)
            record.revision += 1
            record.condition.notify_all()
            return _snapshot(record)

    async def mark_waiting_input(self, delegation_id: str, message_id: str) -> DelegationSnapshot:
        if not message_id.strip():
            raise ValueError("message_id must not be empty")
        record = self._record(delegation_id)
        async with record.condition:
            if record.current_invocation_id is not None:
                raise DelegationStateError(
                    "delegation.activation_active",
                    "a live activation must be paused before waiting for input",
                )
            if record.status not in {
                DelegationStatus.COMPLETED,
                DelegationStatus.WAITING_INPUT,
            }:
                raise DelegationStateError(
                    "delegation.waiting_input_state_invalid",
                    f"delegation {delegation_id} cannot wait for input from {record.status.value}",
                )
            if record.status is DelegationStatus.COMPLETED:
                # The previous report remains available in report_history, but the
                # current operation is non-terminal until the next reply activates.
                record.report = None
            record.status = DelegationStatus.WAITING_INPUT
            record.revision += 1
            record.condition.notify_all()
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
            key = (delegation_id, idempotency_key)
            existing = self._continuations.get(key)
            if existing is not None:
                if existing != fingerprint:
                    raise DelegationConflict(
                        "delegation.continuation_conflict",
                        f"continuation key {idempotency_key} has a different request",
                    )
                return False
            self._continuations[key] = fingerprint
            return True

    async def continuation_fingerprint(
        self, delegation_id: str, idempotency_key: str
    ) -> str | None:
        self._record(delegation_id)
        async with self._lock:
            return self._continuations.get((delegation_id, idempotency_key))

    async def begin_activation(
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
                DelegationStatus.ADMITTED,
                DelegationStatus.COMPLETED,
                DelegationStatus.FAILED,
                DelegationStatus.CANCELLED,
                DelegationStatus.WAITING_INPUT,
            }:
                raise DelegationStateError(
                    "delegation.activation_state_invalid",
                    f"delegation {delegation_id} cannot activate from {record.status.value}",
                )
            if record.admission is None or not record.admission.allowed:
                raise DelegationStateError(
                    "delegation.not_admitted",
                    f"delegation {delegation_id} has not passed admission",
                )
            if record.activation_count >= record.request.policy.budget.max_activations:
                raise DelegationStateError(
                    "delegation.activation_budget_exceeded",
                    f"delegation {delegation_id} exceeded its activation budget",
                )
            record.status = DelegationStatus.PREPARING
            if record.report is not None:
                record.report = None
            record.current_invocation_id = invocation_id
            record.current_activation_id = activation_id
            record.activation_count += 1
            record.revision += 1
            record.condition.notify_all()
            return _snapshot(record)

    async def mark_activation_active(
        self, delegation_id: str, invocation_id: str, activation_id: str
    ) -> DelegationSnapshot:
        record = self._record(delegation_id)
        async with record.condition:
            if (
                record.status is not DelegationStatus.PREPARING
                or record.current_invocation_id != invocation_id
                or record.current_activation_id != activation_id
            ):
                raise DelegationStateError(
                    "delegation.activation_identity_invalid",
                    f"delegation {delegation_id} activation identity is not preparing",
                )
            record.status = DelegationStatus.ACTIVE
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
                DelegationStatus.CANCELLED,
                DelegationStatus.RECONCILIATION_REQUIRED,
            }:
                raise DelegationStateError(
                    "delegation.pre_activation_report_invalid",
                    (
                        "pre-activation delegation can only be rejected, failed, "
                        "cancelled or require reconciliation"
                    ),
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
        admission=record.admission,
        intent=DelegationIntent(f"{record.request.delegation_id}:intent", record.request),
    )

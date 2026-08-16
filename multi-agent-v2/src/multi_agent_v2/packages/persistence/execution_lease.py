from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal, cast

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from multi_agent_v2.packages.persistence.agent_models import (
    AgentExecutionAttempt,
    AgentExecutionLease,
    ProviderSession,
)

type ExecutionTerminalStatus = Literal[
    "succeeded", "failed", "timed_out", "cancelled", "reconciliation_required"
]
type ExecutionCheckpointStatus = Literal["running", "finalizing", "cancelling"]

_NORMAL_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "timed_out", "cancelled"})
_ALL_TERMINAL_STATUSES = _NORMAL_TERMINAL_STATUSES | {"reconciliation_required"}


class LeaseClaimDisposition(StrEnum):
    ACQUIRED = "acquired"
    BUSY = "busy"
    TERMINAL = "terminal"
    RECONCILIATION_REQUIRED = "reconciliation_required"


@dataclass(frozen=True)
class ExecutionRegistration:
    execution_id: str
    idempotency_key: str
    workflow_instance_id: str
    node_id: str
    activation: int
    plan_hash: str
    request_hash: str
    output_schema_hash: str
    provider: str
    model: str
    effort: str
    workspace_id: str
    access_mode: Literal["read_only", "workspace_write"]
    session_mode: Literal["new", "resume"]


@dataclass(frozen=True)
class ExecutionAttemptRegistration:
    attempt_id: str
    execution_id: str
    temporal_workflow_run_id: str
    temporal_activity_id: str
    temporal_activity_run_id: str | None
    attempt_number: int
    worker_id: str


@dataclass(frozen=True)
class LeaseClaimResult:
    disposition: LeaseClaimDisposition
    execution_id: str
    status: str
    lease_epoch: int
    result_payload: Mapping[str, object] | None = None
    result_artifact_ref: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    reconcile_reason: str | None = None
    provider_session_id: str | None = None
    provider_turn_id: str | None = None
    last_sequence: int = 0


@dataclass(frozen=True)
class ProviderSessionClaim:
    session_key: str
    claim_epoch: int


class ExecutionLeaseError(RuntimeError):
    """Base class for execution coordination failures."""


class ExecutionIdentityConflict(ExecutionLeaseError):
    """The same execution or idempotency key was reused with different immutable input."""


class ExecutionLeaseLost(ExecutionLeaseError):
    """The caller no longer owns the fenced execution lease."""


class ExecutionStateConflict(ExecutionLeaseError):
    """The requested transition is incompatible with the durable execution state."""


class ProviderSessionBusy(ExecutionLeaseError):
    """A provider session is still claimed by another logical execution."""


class ExecutionLeaseRepository:
    """PostgreSQL-backed, fenced coordination for one logical Agent execution."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def claim(
        self,
        registration: ExecutionRegistration,
        *,
        lease_owner: str,
        lease_duration: timedelta,
    ) -> LeaseClaimResult:
        _validate_duration(lease_duration)
        async with self._sessions() as session, session.begin():
            await session.execute(
                pg_insert(AgentExecutionLease)
                .values(
                    execution_id=registration.execution_id,
                    idempotency_key=registration.idempotency_key,
                    workflow_instance_id=registration.workflow_instance_id,
                    node_id=registration.node_id,
                    activation=registration.activation,
                    plan_hash=registration.plan_hash,
                    request_hash=registration.request_hash,
                    output_schema_hash=registration.output_schema_hash,
                    provider=registration.provider,
                    model=registration.model,
                    effort=registration.effort,
                    workspace_id=registration.workspace_id,
                    access_mode=registration.access_mode,
                    session_mode=registration.session_mode,
                    status="registered",
                    lease_epoch=0,
                    last_sequence=0,
                )
                .on_conflict_do_nothing()
            )
            row = await session.scalar(
                select(AgentExecutionLease)
                .where(
                    or_(
                        AgentExecutionLease.execution_id == registration.execution_id,
                        AgentExecutionLease.idempotency_key == registration.idempotency_key,
                    )
                )
                .with_for_update()
            )
            if row is None:
                raise ExecutionStateConflict("execution row was not visible after registration")
            _validate_identity(row, registration)
            now = await _database_now(session)

            if row.status in _NORMAL_TERMINAL_STATUSES:
                return _claim_result(row, LeaseClaimDisposition.TERMINAL)
            if row.status == "reconciliation_required":
                return _claim_result(row, LeaseClaimDisposition.RECONCILIATION_REQUIRED)
            lease_is_active = row.lease_owner is not None and (
                row.lease_expires_at is not None and row.lease_expires_at > now
            )
            if lease_is_active:
                if row.lease_owner != lease_owner:
                    return _claim_result(row, LeaseClaimDisposition.BUSY)
                row.lease_expires_at = now + lease_duration
                row.last_heartbeat_at = now
                row.updated_at = now
                return _claim_result(row, LeaseClaimDisposition.ACQUIRED)
            if row.start_intent_at is not None:
                row.status = "reconciliation_required"
                row.reconcile_reason = "execution lease expired after external start intent"
                row.lease_owner = None
                row.lease_expires_at = None
                row.updated_at = now
                return _claim_result(row, LeaseClaimDisposition.RECONCILIATION_REQUIRED)

            row.lease_epoch += 1
            row.lease_owner = lease_owner
            row.lease_expires_at = now + lease_duration
            row.last_heartbeat_at = now
            row.status = "leased" if row.start_intent_at is None else row.status
            row.updated_at = now
            return _claim_result(row, LeaseClaimDisposition.ACQUIRED)

    async def renew(
        self,
        execution_id: str,
        *,
        lease_owner: str,
        lease_epoch: int,
        lease_duration: timedelta,
    ) -> bool:
        _validate_duration(lease_duration)
        async with self._sessions() as session, session.begin():
            row = await _load_execution(session, execution_id)
            now = await _database_now(session)
            if not _fence_matches(row, lease_owner, lease_epoch, now):
                return False
            row.lease_expires_at = now + lease_duration
            row.last_heartbeat_at = now
            row.updated_at = now
            if row.provider_session_key is not None:
                provider_session = await session.scalar(
                    select(ProviderSession)
                    .where(ProviderSession.session_key == row.provider_session_key)
                    .with_for_update()
                )
                if (
                    provider_session is not None
                    and provider_session.active_execution_id == execution_id
                    and provider_session.claim_owner == lease_owner
                ):
                    provider_session.claim_expires_at = now + lease_duration
                    provider_session.last_seen_at = now
                    provider_session.updated_at = now
            return True

    async def begin_attempt(
        self,
        registration: ExecutionAttemptRegistration,
        *,
        lease_owner: str,
        lease_epoch: int,
    ) -> None:
        async with self._sessions() as session, session.begin():
            row = await _load_execution(session, registration.execution_id)
            now = await _database_now(session)
            _require_fence(row, lease_owner, lease_epoch, now)
            await session.execute(
                pg_insert(AgentExecutionAttempt)
                .values(
                    attempt_id=registration.attempt_id,
                    execution_id=registration.execution_id,
                    temporal_workflow_run_id=registration.temporal_workflow_run_id,
                    temporal_activity_id=registration.temporal_activity_id,
                    temporal_activity_run_id=registration.temporal_activity_run_id,
                    attempt_number=registration.attempt_number,
                    worker_id=registration.worker_id,
                    lease_epoch=lease_epoch,
                    phase="started",
                    status="started",
                    last_sequence=0,
                    heartbeat_at=now,
                    started_at=now,
                )
                .on_conflict_do_nothing()
            )
            attempt = await session.scalar(
                select(AgentExecutionAttempt)
                .where(AgentExecutionAttempt.attempt_id == registration.attempt_id)
                .with_for_update()
            )
            if attempt is None:
                raise ExecutionStateConflict("execution attempt was not visible after insert")
            if (
                attempt.execution_id != registration.execution_id
                or attempt.attempt_number != registration.attempt_number
                or attempt.lease_epoch != lease_epoch
            ):
                raise ExecutionIdentityConflict("execution attempt identity was reused")

    async def record_session(
        self,
        execution_id: str,
        *,
        lease_owner: str,
        lease_epoch: int,
        provider: str,
        native_session_id: str,
        workspace_id: str,
        lease_duration: timedelta,
    ) -> ProviderSessionClaim:
        _validate_duration(lease_duration)
        if not native_session_id.strip():
            raise ValueError("native provider session ID must not be blank")
        session_key = _provider_session_key(provider, native_session_id)
        async with self._sessions() as session, session.begin():
            execution = await _load_execution(session, execution_id)
            now = await _database_now(session)
            _require_fence(execution, lease_owner, lease_epoch, now)
            if execution.status != "starting" or execution.start_intent_at is None:
                raise ExecutionStateConflict(
                    "provider session cannot be recorded before durable external start intent"
                )
            await session.execute(
                pg_insert(ProviderSession)
                .values(
                    session_key=session_key,
                    provider=provider,
                    native_session_id=native_session_id,
                    workspace_id=workspace_id,
                    status="open",
                    claim_epoch=0,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=[ProviderSession.provider, ProviderSession.native_session_id]
                )
            )
            provider_session = await session.scalar(
                select(ProviderSession)
                .where(
                    ProviderSession.provider == provider,
                    ProviderSession.native_session_id == native_session_id,
                )
                .with_for_update()
            )
            if provider_session is None:
                raise ExecutionStateConflict("provider session was not visible after registration")
            if provider_session.workspace_id != workspace_id:
                raise ExecutionIdentityConflict("provider session workspace does not match")
            if provider_session.status == "closed":
                raise ExecutionStateConflict("provider session is closed")
            if provider_session.active_execution_id not in {None, execution_id}:
                raise ProviderSessionBusy("provider session is claimed by another execution")

            if provider_session.active_execution_id != execution_id:
                provider_session.claim_epoch += 1
            provider_session.active_execution_id = execution_id
            provider_session.claim_owner = lease_owner
            provider_session.claim_expires_at = now + lease_duration
            provider_session.last_seen_at = now
            provider_session.updated_at = now
            execution.provider_session_key = provider_session.session_key
            execution.provider_session_id = native_session_id
            execution.status = "starting"
            execution.updated_at = now
            return ProviderSessionClaim(
                session_key=provider_session.session_key,
                claim_epoch=provider_session.claim_epoch,
            )

    async def mark_start_intent(
        self,
        execution_id: str,
        *,
        lease_owner: str,
        lease_epoch: int,
    ) -> datetime:
        async with self._sessions() as session, session.begin():
            row = await _load_execution(session, execution_id)
            now = await _database_now(session)
            _require_fence(row, lease_owner, lease_epoch, now)
            if row.status not in {"leased", "session_prepared", "starting"}:
                raise ExecutionStateConflict(
                    f"cannot record start intent while execution is {row.status}"
                )
            if row.start_intent_at is None:
                row.start_intent_at = now
            row.status = "starting"
            row.updated_at = now
            return row.start_intent_at

    async def checkpoint(
        self,
        execution_id: str,
        *,
        lease_owner: str,
        lease_epoch: int,
        status: ExecutionCheckpointStatus,
        sequence: int,
        native_operation_id: str | None = None,
        process_id: int | None = None,
        process_started_at: datetime | None = None,
        attempt_id: str | None = None,
    ) -> None:
        if sequence < 0:
            raise ValueError("sequence must be non-negative")
        if (process_id is None) != (process_started_at is None):
            raise ValueError("process ID and process start time must be recorded together")
        async with self._sessions() as session, session.begin():
            row = await _load_execution(session, execution_id)
            now = await _database_now(session)
            _require_fence(row, lease_owner, lease_epoch, now)
            if row.start_intent_at is None:
                raise ExecutionStateConflict("execution cannot run before start intent is durable")
            if sequence < row.last_sequence:
                raise ExecutionStateConflict("execution checkpoint sequence moved backwards")
            row.status = status
            row.last_sequence = sequence
            row.last_heartbeat_at = now
            row.updated_at = now
            if native_operation_id is not None:
                row.native_operation_id = native_operation_id
            if process_id is not None:
                row.process_id = process_id
                row.process_started_at = process_started_at
            if attempt_id is not None:
                attempt = await _load_attempt(session, attempt_id, execution_id, lease_epoch)
                attempt.phase = status
                attempt.status = "running"
                attempt.last_sequence = sequence
                attempt.heartbeat_at = now

    async def finalize(
        self,
        execution_id: str,
        *,
        lease_owner: str,
        lease_epoch: int,
        status: ExecutionTerminalStatus,
        result_payload: Mapping[str, object] | None = None,
        result_artifact_ref: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        reconcile_reason: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        async with self._sessions() as session, session.begin():
            row = await _load_execution(session, execution_id)
            now = await _database_now(session)
            if row.status in _ALL_TERMINAL_STATUSES:
                if row.status == status and _terminal_payload_matches(
                    row,
                    result_payload=result_payload,
                    result_artifact_ref=result_artifact_ref,
                    error_code=error_code,
                    error_message=error_message,
                    reconcile_reason=reconcile_reason,
                ):
                    return
                raise ExecutionStateConflict(
                    f"execution is already terminal with different data ({row.status})"
                )
            _require_fence(row, lease_owner, lease_epoch, now)
            if status == "succeeded" and result_payload is None and result_artifact_ref is None:
                raise ValueError("successful execution requires a durable result")
            if status == "reconciliation_required" and not reconcile_reason:
                raise ValueError("reconciliation_required needs an explicit reason")

            row.status = status
            row.result_payload = dict(result_payload) if result_payload is not None else None
            row.result_artifact_ref = result_artifact_ref
            row.error_code = error_code
            row.error_message = error_message
            row.reconcile_reason = reconcile_reason
            row.completed_at = now
            row.updated_at = now
            row.lease_owner = None
            row.lease_expires_at = None

            if attempt_id is not None:
                attempt = await _load_attempt(session, attempt_id, execution_id, lease_epoch)
                attempt.phase = status
                attempt.status = status
                attempt.error_code = error_code
                attempt.error_message = error_message
                attempt.heartbeat_at = now
                attempt.finished_at = now

            if row.provider_session_key is not None:
                provider_session = await session.scalar(
                    select(ProviderSession)
                    .where(ProviderSession.session_key == row.provider_session_key)
                    .with_for_update()
                )
                if (
                    provider_session is not None
                    and provider_session.active_execution_id == execution_id
                ):
                    provider_session.claim_owner = None
                    provider_session.claim_expires_at = None
                    provider_session.last_seen_at = now
                    provider_session.updated_at = now
                    if status == "reconciliation_required":
                        provider_session.status = "unknown"
                    else:
                        provider_session.active_execution_id = None

    async def resolve_reconciliation(
        self,
        execution_id: str,
        *,
        status: Literal["succeeded", "failed", "cancelled"],
        result_payload: Mapping[str, object] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        async with self._sessions() as session, session.begin():
            row = await _load_execution(session, execution_id)
            now = await _database_now(session)
            if row.status == status and _terminal_payload_matches(
                row,
                result_payload=result_payload,
                result_artifact_ref=None,
                error_code=error_code,
                error_message=error_message,
                reconcile_reason=None,
            ):
                return
            if row.status != "reconciliation_required":
                raise ExecutionStateConflict(
                    f"execution is not awaiting reconciliation ({row.status})"
                )
            if status == "succeeded" and result_payload is None:
                raise ValueError("successful reconciliation requires a durable result")
            row.status = status
            row.result_payload = dict(result_payload) if result_payload is not None else None
            row.error_code = error_code
            row.error_message = error_message
            row.reconcile_reason = None
            row.completed_at = now
            row.updated_at = now

            if row.provider_session_key is not None:
                provider_session = await session.scalar(
                    select(ProviderSession)
                    .where(ProviderSession.session_key == row.provider_session_key)
                    .with_for_update()
                )
                if provider_session is not None:
                    provider_session.status = "open"
                    provider_session.active_execution_id = None
                    provider_session.claim_owner = None
                    provider_session.claim_expires_at = None
                    provider_session.last_seen_at = now
                    provider_session.updated_at = now


async def _load_execution(session: AsyncSession, execution_id: str) -> AgentExecutionLease:
    row = await session.scalar(
        select(AgentExecutionLease)
        .where(AgentExecutionLease.execution_id == execution_id)
        .with_for_update()
    )
    if row is None:
        raise ExecutionStateConflict(f"unknown execution {execution_id!r}")
    return row


async def _load_attempt(
    session: AsyncSession,
    attempt_id: str,
    execution_id: str,
    lease_epoch: int,
) -> AgentExecutionAttempt:
    attempt = await session.scalar(
        select(AgentExecutionAttempt)
        .where(AgentExecutionAttempt.attempt_id == attempt_id)
        .with_for_update()
    )
    if attempt is None:
        raise ExecutionStateConflict(f"unknown execution attempt {attempt_id!r}")
    if attempt.execution_id != execution_id or attempt.lease_epoch != lease_epoch:
        raise ExecutionLeaseLost("execution attempt is owned by a different fence")
    return attempt


async def _database_now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime):
        raise ExecutionStateConflict("PostgreSQL did not return a database timestamp")
    return value


def _validate_identity(
    row: AgentExecutionLease,
    registration: ExecutionRegistration,
) -> None:
    if (
        row.execution_id != registration.execution_id
        or row.idempotency_key != registration.idempotency_key
        or row.request_hash != registration.request_hash
        or row.plan_hash != registration.plan_hash
        or row.workflow_instance_id != registration.workflow_instance_id
        or row.node_id != registration.node_id
        or row.activation != registration.activation
    ):
        raise ExecutionIdentityConflict(
            "execution identity or idempotency key was reused with different immutable input"
        )


def _fence_matches(
    row: AgentExecutionLease,
    lease_owner: str,
    lease_epoch: int,
    now: datetime,
) -> bool:
    return (
        row.status not in _ALL_TERMINAL_STATUSES
        and row.lease_owner == lease_owner
        and row.lease_epoch == lease_epoch
        and row.lease_expires_at is not None
        and row.lease_expires_at > now
    )


def _require_fence(
    row: AgentExecutionLease,
    lease_owner: str,
    lease_epoch: int,
    now: datetime,
) -> None:
    if not _fence_matches(row, lease_owner, lease_epoch, now):
        raise ExecutionLeaseLost("execution lease fence is no longer valid")


def _claim_result(
    row: AgentExecutionLease,
    disposition: LeaseClaimDisposition,
) -> LeaseClaimResult:
    payload = cast(Mapping[str, object] | None, row.result_payload)
    return LeaseClaimResult(
        disposition=disposition,
        execution_id=row.execution_id,
        status=row.status,
        lease_epoch=row.lease_epoch,
        result_payload=payload,
        result_artifact_ref=row.result_artifact_ref,
        error_code=row.error_code,
        error_message=row.error_message,
        reconcile_reason=row.reconcile_reason,
        provider_session_id=row.provider_session_id,
        provider_turn_id=row.native_operation_id,
        last_sequence=row.last_sequence,
    )


def _provider_session_key(provider: str, native_session_id: str) -> str:
    return hashlib.sha256(f"{provider}\0{native_session_id}".encode()).hexdigest()


def _terminal_payload_matches(
    row: AgentExecutionLease,
    *,
    result_payload: Mapping[str, object] | None,
    result_artifact_ref: str | None,
    error_code: str | None,
    error_message: str | None,
    reconcile_reason: str | None,
) -> bool:
    expected_payload = dict(result_payload) if result_payload is not None else None
    return (
        row.result_payload == expected_payload
        and row.result_artifact_ref == result_artifact_ref
        and row.error_code == error_code
        and row.error_message == error_message
        and row.reconcile_reason == reconcile_reason
    )


def _validate_duration(value: timedelta) -> None:
    if value <= timedelta(0):
        raise ValueError("lease duration must be positive")

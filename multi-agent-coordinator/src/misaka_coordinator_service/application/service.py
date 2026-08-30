from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import uuid4

from agent_framework import AgentSession

from misaka_coordinator_service.application.autonomy import (
    CoordinatorPolicyApprovalRequired,
    CoordinatorPolicyDeniedError,
)
from misaka_coordinator_service.application.events import (
    CoordinatorEventBridge,
    CoordinatorEventRecoveryError,
    CoordinatorEventUpdate,
)
from misaka_coordinator_service.application.orchestrator import (
    CoordinatorActivationResult,
    CoordinatorCancellationResult,
    CoordinatorMessageResult,
    CoordinatorNodeResult,
    CoordinatorOrchestrator,
    CoordinatorReconciliationResult,
    CoordinatorRetryResult,
)
from misaka_coordinator_service.domain import (
    AutonomyApproval,
    CoordinatorSession,
    Goal,
    GoalStatus,
    InvalidTransitionError,
    PlanNodeStatus,
)
from misaka_coordinator_service.domain._serialization import ensure_text, ensure_text_tuple
from misaka_coordinator_service.execution import (
    DelegationReport,
    DelegationSnapshot,
    JsonValue,
    MessageDelivery,
    ReconciliationStatus,
)
from misaka_coordinator_service.persistence import (
    CoordinatorEventStorePort,
    CoordinatorSessionEvent,
    CoordinatorSessionRecord,
    JsonlCoordinatorEventStore,
    PendingEventActivation,
)

_MONITOR_CHECKPOINT_INTERVAL = 32
_TRANSIENT_MONITOR_EVENT_KINDS = frozenset(
    {
        "output_delta",
        "reasoning_delta",
        "plan_delta",
        "tool_output_delta",
        "command_output_delta",
        "task_progress",
    }
)
_TERMINAL_MONITOR_NODE_STATUSES = frozenset(
    {
        PlanNodeStatus.RECONCILIATION_REQUIRED,
        PlanNodeStatus.FAILED,
        PlanNodeStatus.CANCELLED,
        PlanNodeStatus.ACCEPTED,
    }
)


class CoordinatorServiceError(RuntimeError):
    """Base error for the Coordinator application service."""


class CoordinatorServiceValidationError(CoordinatorServiceError):
    """Raised when an application request violates the service contract."""


class CoordinatorServiceNotFoundError(CoordinatorServiceError):
    """Raised when a requested Coordinator session does not exist."""


class CoordinatorServiceApprovalRequiredError(CoordinatorServiceError):
    """Raised after persisting an approval required by a protected operation."""

    def __init__(self, approval: AutonomyApproval) -> None:
        super().__init__(approval.reason)
        self.approval = approval


@dataclass(frozen=True, slots=True)
class CoordinatorMonitorStatus:
    session_id: str
    node_id: str
    delegation_id: str
    running: bool
    last_error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "node_id": self.node_id,
            "delegation_id": self.delegation_id,
            "running": self.running,
            "last_error": self.last_error,
        }


class CoordinatorSessionStorePort(Protocol):
    def load(self, session_id: str) -> CoordinatorSessionRecord | None: ...

    def save(
        self,
        record: CoordinatorSessionRecord,
        *,
        expected_version: int,
    ) -> CoordinatorSessionRecord: ...

    def list_session_ids(self) -> tuple[str, ...]: ...

    def list_records(self) -> tuple[CoordinatorSessionRecord, ...]: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class CoordinatorActivationRequest:
    session_id: str
    prompt: str
    cwd: str
    cognitive_session_id: str | None = None
    acceptance_criteria: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    activation_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("session_id", "prompt", "cwd"):
            object.__setattr__(self, field_name, ensure_text(getattr(self, field_name), field_name))
        object.__setattr__(
            self,
            "cognitive_session_id",
            None
            if self.cognitive_session_id is None
            else ensure_text(self.cognitive_session_id, "cognitive_session_id"),
        )
        object.__setattr__(
            self,
            "acceptance_criteria",
            ensure_text_tuple(self.acceptance_criteria, "acceptance_criteria"),
        )
        object.__setattr__(
            self,
            "constraints",
            ensure_text_tuple(self.constraints, "constraints"),
        )
        object.__setattr__(
            self,
            "activation_id",
            (
                None
                if self.activation_id is None
                else ensure_text(self.activation_id, "activation_id")
            ),
        )


@dataclass(frozen=True, slots=True)
class CoordinatorServiceActivation:
    result: CoordinatorActivationResult

    def to_dict(self) -> dict[str, object]:
        return {
            "session": self.result.session.to_dict(),
            "outcome": self.result.outcome.value,
            "step_count": self.result.step_count,
            "message": self.result.message,
            "decisions": [decision.to_dict() for decision in self.result.decisions],
            "delegations": [_delegation_payload(snapshot) for snapshot in self.result.delegations],
        }


@dataclass(frozen=True, slots=True)
class CoordinatorApprovalResolution:
    session: CoordinatorSession
    approval: AutonomyApproval

    def to_dict(self) -> dict[str, object]:
        return {
            "session": self.session.to_dict(),
            "approval": self.approval.to_dict(),
        }


class CoordinatorService:
    """Persisted application facade for MAF decisions and V3 execution operations."""

    def __init__(
        self,
        *,
        orchestrator: CoordinatorOrchestrator,
        store: CoordinatorSessionStorePort,
        clock: Callable[[], datetime] | None = None,
        activation_id_factory: Callable[[], str] | None = None,
        event_bridge: CoordinatorEventBridge | None = None,
        event_store: CoordinatorEventStorePort | None = None,
        event_retry_seconds: float = 2.0,
    ) -> None:
        self._orchestrator = orchestrator
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._activation_id_factory = activation_id_factory or (lambda: f"activation-{uuid4().hex}")
        self._event_bridge = event_bridge
        self._event_store = event_store or JsonlCoordinatorEventStore()
        if event_retry_seconds <= 0:
            raise CoordinatorServiceValidationError("event_retry_seconds must be positive")
        self._event_retry_seconds = event_retry_seconds
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = threading.RLock()
        self._monitor_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._monitor_nodes: dict[tuple[str, str], str] = {}
        self._monitor_errors: dict[tuple[str, str], str] = {}
        self._finished_monitors: set[tuple[str, str]] = set()
        self._pending_initial_activations: set[str] = set()
        self._stop_event = asyncio.Event()
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for record in self._store.list_records():
            if record.coordinator_session.archived_at is None:
                self._sync_monitors(record)

    def get(self, session_id: str) -> CoordinatorSessionRecord:
        normalized = ensure_text(session_id, "session_id")
        record = self._store.load(normalized)
        if record is None:
            raise CoordinatorServiceNotFoundError(f"coordinator session {normalized} was not found")
        return record

    def list_session_ids(self, *, archived: bool = False) -> tuple[str, ...]:
        return tuple(record.session_id for record in self.list_sessions(archived=archived))

    def list_sessions(self, *, archived: bool = False) -> tuple[CoordinatorSessionRecord, ...]:
        return tuple(
            record
            for record in self._store.list_records()
            if record.session_id not in self._pending_initial_activations
            and (record.coordinator_session.archived_at is not None) == archived
        )

    def list_events(
        self, session_id: str, *, next_sequence: int = 1
    ) -> tuple[CoordinatorSessionEvent, ...]:
        self.get(session_id)
        return self._event_store.list_events(session_id, next_sequence=next_sequence)

    def stream_events(
        self, session_id: str, *, next_sequence: int = 1
    ) -> AsyncIterator[CoordinatorSessionEvent]:
        self.get(session_id)
        return self._event_store.stream_events(session_id, next_sequence=next_sequence)

    def monitor_statuses(self) -> tuple[CoordinatorMonitorStatus, ...]:
        statuses: list[CoordinatorMonitorStatus] = []
        for key, node_id in sorted(self._monitor_nodes.items()):
            task = self._monitor_tasks.get(key)
            statuses.append(
                CoordinatorMonitorStatus(
                    session_id=key[0],
                    node_id=node_id,
                    delegation_id=key[1],
                    running=(
                        key not in self._finished_monitors and task is not None and not task.done()
                    ),
                    last_error=self._monitor_errors.get(key),
                )
            )
        return tuple(statuses)

    async def activate(self, request: CoordinatorActivationRequest) -> CoordinatorServiceActivation:
        lock = self._lock_for(request.session_id)
        async with lock:
            record = self._store.load(request.session_id)
            new_session = record is None
            if new_session:
                self._pending_initial_activations.add(request.session_id)
            if record is None:
                current, agent_session = self._create_initial_state(request)
                try:
                    record = self._store.save(
                        CoordinatorSessionRecord(
                            coordinator_session=current,
                            agent_session=agent_session,
                            working_directory=request.cwd,
                        ),
                        expected_version=0,
                    )
                except Exception:
                    self._pending_initial_activations.discard(request.session_id)
                    raise
            else:
                current = record.coordinator_session
                agent_session = record.agent_session
                self._validate_existing_session(current, request)
            expected_version = record.version
            activation_id = request.activation_id or ensure_text(
                self._activation_id_factory(), "activation_id"
            )
            started_at = self._now()
            if new_session and not self._event_store.list_events(request.session_id):
                self._append_event(
                    request.session_id,
                    "session.created",
                    {"goal": request.prompt},
                    occurred_at=started_at,
                )
            self._append_event(
                request.session_id,
                "user.message",
                {"message": request.prompt, "activation_id": activation_id},
                occurred_at=started_at,
            )
            self._append_event(
                request.session_id,
                "activation.started",
                {"activation_id": activation_id, "working_directory": request.cwd},
                occurred_at=started_at,
            )
            try:
                result = await self._orchestrator.activate(
                    request.prompt,
                    session=current,
                    agent_session=agent_session,
                    activation_id=activation_id,
                    at=self._now(),
                    cwd=request.cwd,
                )
            except CoordinatorPolicyDeniedError as error:
                if new_session:
                    self._pending_initial_activations.discard(request.session_id)
                self._append_event(
                    request.session_id,
                    "activation.failed",
                    {"activation_id": activation_id, "error": str(error)},
                    occurred_at=self._now(),
                )
                raise CoordinatorServiceValidationError(str(error)) from error
            except Exception as error:
                if new_session:
                    self._pending_initial_activations.discard(request.session_id)
                self._append_event(
                    request.session_id,
                    "activation.failed",
                    {"activation_id": activation_id, "error": str(error)},
                    occurred_at=self._now(),
                )
                raise
            saved = self._store.save(
                CoordinatorSessionRecord(
                    coordinator_session=result.session,
                    agent_session=result.agent_session,
                    working_directory=request.cwd,
                    pending_event_activation=record.pending_event_activation,
                ),
                expected_version=expected_version,
            )
            self._sync_monitors(saved)
            for decision in result.decisions:
                self._append_event(
                    request.session_id,
                    "coordinator.decision",
                    {"activation_id": activation_id, "decision": decision.to_dict()},
                    occurred_at=self._now(),
                )
            self._append_event(
                request.session_id,
                "activation.completed",
                {
                    "activation_id": activation_id,
                    "outcome": result.outcome.value,
                    "message": result.message,
                    "step_count": result.step_count,
                    "delegation_ids": [snapshot.delegation_id for snapshot in result.delegations],
                },
                occurred_at=self._now(),
            )
            if new_session:
                self._pending_initial_activations.discard(request.session_id)
            return CoordinatorServiceActivation(result=result)

    async def send_message(
        self,
        *,
        session_id: str,
        node_id: str,
        message: str,
        delivery: MessageDelivery = MessageDelivery.APPEND,
        expected_activation_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> CoordinatorMessageResult:
        normalized_session_id = ensure_text(session_id, "session_id")
        lock = self._lock_for(normalized_session_id)
        async with lock:
            record = self.get(normalized_session_id)
            self._ensure_session_unarchived(record.coordinator_session)
            result = await self._orchestrator.send_message(
                session=record.coordinator_session,
                node_id=node_id,
                message=message,
                at=self._now(),
                delivery=delivery,
                expected_activation_id=expected_activation_id,
                model=model,
                effort=effort,
            )
            self._save_result(record, result.session, record.agent_session)
            self._append_event(
                normalized_session_id,
                "delegation.message.dispatched",
                {
                    "node_id": node_id,
                    "message": message,
                    "delivery": delivery.value,
                    "dispatch": _dispatch_payload(result),
                },
                occurred_at=self._now(),
            )
            return result

    async def node_snapshots(
        self,
        *,
        session_id: str,
    ) -> tuple[tuple[str, DelegationSnapshot], ...]:
        normalized_session_id = ensure_text(session_id, "session_id")
        lock = self._lock_for(normalized_session_id)
        async with lock:
            record = self.get(normalized_session_id)
            plan = record.coordinator_session.plan
            if plan is None:
                return ()
            node_ids = tuple(
                node.node_id
                for node in plan.nodes
                if node.execution is not None
                and node.status not in _TERMINAL_MONITOR_NODE_STATUSES
            )
        snapshots: list[tuple[str, DelegationSnapshot]] = []
        for node_id in node_ids:
            snapshot = await self._refresh_node_snapshot(
                session_id=normalized_session_id,
                node_id=node_id,
            )
            if snapshot is not None:
                snapshots.append((node_id, snapshot))
        return tuple(snapshots)

    async def continue_node(
        self,
        *,
        session_id: str,
        node_id: str,
        message: str,
        expected_activation_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> CoordinatorMessageResult:
        normalized_session_id = ensure_text(session_id, "session_id")
        lock = self._lock_for(normalized_session_id)
        async with lock:
            record = self.get(normalized_session_id)
            self._ensure_session_unarchived(record.coordinator_session)
            result = await self._orchestrator.continue_node(
                session=record.coordinator_session,
                node_id=node_id,
                message=message,
                at=self._now(),
                expected_activation_id=expected_activation_id,
                model=model,
                effort=effort,
            )
            self._save_result(record, result.session, record.agent_session)
            self._append_event(
                normalized_session_id,
                "delegation.continued",
                {"node_id": node_id, "message": message, "dispatch": _dispatch_payload(result)},
                occurred_at=self._now(),
            )
            return result

    async def cancel_node(
        self,
        *,
        session_id: str,
        node_id: str,
        reason: str,
        request_id: str | None = None,
        idempotency_key: str | None = None,
        expected_activation_id: str | None = None,
    ) -> CoordinatorCancellationResult:
        normalized_session_id = ensure_text(session_id, "session_id")
        lock = self._lock_for(normalized_session_id)
        async with lock:
            record = self.get(normalized_session_id)
            self._ensure_session_unarchived(record.coordinator_session)
            result = await self._orchestrator.cancel_node(
                session=record.coordinator_session,
                node_id=node_id,
                reason=reason,
                at=self._now(),
                request_id=request_id,
                idempotency_key=idempotency_key,
                expected_activation_id=expected_activation_id,
            )
            self._save_result(record, result.session, record.agent_session)
            self._append_event(
                normalized_session_id,
                "delegation.cancelled",
                {
                    "node_id": node_id,
                    "reason": reason,
                    "snapshot": _snapshot_payload(result.snapshot),
                },
                occurred_at=self._now(),
            )
            return result

    async def reconcile_node(
        self,
        *,
        session_id: str,
        node_id: str,
        expected_revision: int,
        status: ReconciliationStatus,
        reason: str,
        output: JsonValue = None,
        request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CoordinatorReconciliationResult:
        normalized_session_id = ensure_text(session_id, "session_id")
        lock = self._lock_for(normalized_session_id)
        async with lock:
            record = self.get(normalized_session_id)
            self._ensure_session_unarchived(record.coordinator_session)
            try:
                result = await self._orchestrator.reconcile_node(
                    session=record.coordinator_session,
                    node_id=node_id,
                    expected_revision=expected_revision,
                    status=status,
                    reason=reason,
                    at=self._now(),
                    output=output,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                )
            except CoordinatorPolicyApprovalRequired as error:
                self._save_result(record, error.session, record.agent_session)
                raise CoordinatorServiceApprovalRequiredError(error.approval) from error
            except CoordinatorPolicyDeniedError as error:
                raise CoordinatorServiceValidationError(str(error)) from error
            self._save_result(record, result.session, record.agent_session)
            self._append_event(
                normalized_session_id,
                "delegation.reconciled",
                {
                    "node_id": node_id,
                    "status": status.value,
                    "reason": reason,
                    "snapshot": _snapshot_payload(result.snapshot),
                },
                occurred_at=self._now(),
            )
            return result

    async def accept_result(
        self,
        *,
        session_id: str,
        node_id: str,
        expected_session_revision: int,
    ) -> CoordinatorNodeResult:
        normalized_session_id = ensure_text(session_id, "session_id")
        if isinstance(expected_session_revision, bool) or expected_session_revision < 0:
            raise CoordinatorServiceValidationError(
                "expected_session_revision must not be negative"
            )
        lock = self._lock_for(normalized_session_id)
        async with lock:
            record = self.get(normalized_session_id)
            self._ensure_session_unarchived(record.coordinator_session)
            if record.coordinator_session.revision != expected_session_revision:
                raise CoordinatorServiceValidationError(
                    "session revision is "
                    f"{record.coordinator_session.revision}, expected {expected_session_revision}"
                )
            result = await self._orchestrator.accept_result(
                session=record.coordinator_session,
                node_id=node_id,
                at=self._now(),
            )
            self._save_result(record, result.session, record.agent_session)
            self._append_event(
                normalized_session_id,
                "delegation.result.accepted",
                {"node_id": node_id},
                occurred_at=self._now(),
            )
            return result

    async def retry_node(
        self,
        *,
        session_id: str,
        node_id: str,
        model: str | None = None,
        effort: str | None = None,
    ) -> CoordinatorRetryResult:
        normalized_session_id = ensure_text(session_id, "session_id")
        lock = self._lock_for(normalized_session_id)
        async with lock:
            record = self.get(normalized_session_id)
            self._ensure_session_unarchived(record.coordinator_session)
            if record.working_directory is None:
                raise CoordinatorServiceValidationError(
                    "Coordinator session has no persisted working directory"
                )
            result = await self._orchestrator.retry_node(
                session=record.coordinator_session,
                node_id=node_id,
                at=self._now(),
                cwd=record.working_directory,
                model=model,
                effort=effort,
            )
            self._save_result(record, result.session, record.agent_session)
            self._append_event(
                normalized_session_id,
                "delegation.retried",
                {
                    "node_id": node_id,
                    "snapshot": _snapshot_payload(result.snapshot),
                },
                occurred_at=self._now(),
            )
            return result

    async def resolve_approval(
        self,
        *,
        session_id: str,
        approval_id: str,
        approved: bool,
        actor_id: str,
        reason: str,
        expected_session_revision: int,
    ) -> CoordinatorApprovalResolution:
        normalized_session_id = ensure_text(session_id, "session_id")
        normalized_approval_id = ensure_text(approval_id, "approval_id")
        if isinstance(expected_session_revision, bool) or expected_session_revision < 0:
            raise CoordinatorServiceValidationError(
                "expected_session_revision must not be negative"
            )
        lock = self._lock_for(normalized_session_id)
        async with lock:
            record = self.get(normalized_session_id)
            session = record.coordinator_session
            self._ensure_session_unarchived(session)
            if session.revision != expected_session_revision:
                raise CoordinatorServiceValidationError(
                    f"session revision is {session.revision}, expected {expected_session_revision}"
                )
            at = self._now()
            autonomy = session.autonomy.resolve_approval(
                normalized_approval_id,
                approved=approved,
                resolved_by=actor_id,
                reason=reason,
                at=at,
            )
            updated = session.update_autonomy(autonomy, at=at)
            self._save_result(record, updated, record.agent_session)
            approval = next(
                item
                for item in updated.autonomy.approvals
                if item.approval_id == normalized_approval_id
            )
            self._append_event(
                normalized_session_id,
                "approval.resolved",
                {"approval": approval.to_dict()},
                occurred_at=at,
            )
            return CoordinatorApprovalResolution(session=updated, approval=approval)

    async def cancel_session(self, session_id: str, *, reason: str) -> CoordinatorSession:
        normalized_session_id = ensure_text(session_id, "session_id")
        normalized_reason = ensure_text(reason, "reason")
        lock = self._lock_for(normalized_session_id)
        async with lock:
            record = self.get(normalized_session_id)
            session = record.coordinator_session
            self._ensure_session_unarchived(session)
            if session.goal is None or session.goal.status is not GoalStatus.ACTIVE:
                raise CoordinatorServiceValidationError("session does not have an active goal")

            active_nodes = (
                ()
                if session.plan is None
                else tuple(
                    node
                    for node in session.plan.nodes
                    if node.status
                    in {
                        PlanNodeStatus.DELEGATED,
                        PlanNodeStatus.AWAITING_EVENT,
                    }
                )
            )
            for node in active_nodes:
                assert node.execution is not None
                at = self._now()
                result = await self._orchestrator.cancel_node(
                    session=record.coordinator_session,
                    node_id=node.node_id,
                    reason=normalized_reason,
                    at=at,
                    idempotency_key=(
                        f"cancel-session:{normalized_session_id}:{node.node_id}:"
                        f"attempt-{node.attempt}"
                    ),
                )
                self._save_result(record, result.session, record.agent_session)
                self._append_event(
                    normalized_session_id,
                    "delegation.cancelled",
                    {
                        "node_id": node.node_id,
                        "reason": normalized_reason,
                        "snapshot": _snapshot_payload(result.snapshot),
                    },
                    occurred_at=at,
                )
                record = self.get(normalized_session_id)

            if record.coordinator_session.has_live_delegations:
                raise CoordinatorServiceValidationError(
                    "cannot cancel the session while a delegation remains active"
                )
            at = self._now()
            cancelled = record.coordinator_session.cancel_goal(at=at)
            self._save_result(record, cancelled, record.agent_session)
            self._append_event(
                normalized_session_id,
                "session.cancelled",
                {"reason": normalized_reason},
                occurred_at=at,
            )
            return cancelled

    async def archive_session(self, session_id: str) -> CoordinatorSession:
        normalized_session_id = ensure_text(session_id, "session_id")
        lock = self._lock_for(normalized_session_id)
        async with lock:
            record = self.get(normalized_session_id)
            blocker = self.archive_blocker(record)
            if blocker == "pending_event_activation":
                raise CoordinatorServiceValidationError(
                    "cannot archive a session with a pending event activation"
                )
            if blocker == "active_work":
                raise CoordinatorServiceValidationError("cannot archive a session with active work")
            at = self._now()
            try:
                archived = record.coordinator_session.archive(at=at)
            except InvalidTransitionError as error:
                raise CoordinatorServiceValidationError(str(error)) from error
            if archived is record.coordinator_session:
                return archived
            self._save_result(record, archived, record.agent_session)
            archived_at = archived.archived_at
            assert archived_at is not None
            self._append_event(
                normalized_session_id,
                "session.archived",
                {"archived_at": archived_at.isoformat()},
                occurred_at=at,
            )
            return archived

    @staticmethod
    def archive_blocker(record: CoordinatorSessionRecord) -> str | None:
        session = record.coordinator_session
        if session.archived_at is not None:
            return None
        if record.pending_event_activation is not None and (
            session.goal is None or session.goal.status is GoalStatus.ACTIVE
        ):
            return "pending_event_activation"
        if not session.can_archive:
            return "active_work"
        return None

    async def unarchive_session(self, session_id: str) -> CoordinatorSession:
        normalized_session_id = ensure_text(session_id, "session_id")
        lock = self._lock_for(normalized_session_id)
        async with lock:
            record = self.get(normalized_session_id)
            at = self._now()
            unarchived = record.coordinator_session.unarchive(at=at)
            if unarchived is record.coordinator_session:
                return unarchived
            self._save_result(record, unarchived, record.agent_session)
            self._append_event(
                normalized_session_id,
                "session.unarchived",
                {},
                occurred_at=at,
            )
            return unarchived

    async def aclose(self) -> None:
        self._stop_event.set()
        tasks = tuple(self._monitor_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._monitor_tasks.clear()
        self._monitor_nodes.clear()
        self._monitor_errors.clear()
        self._finished_monitors.clear()
        self._pending_initial_activations.clear()
        self._started = False
        self._store.close()
        self._event_store.close()

    def close(self) -> None:
        self._stop_event.set()
        for task in self._monitor_tasks.values():
            task.cancel()
        self._monitor_tasks.clear()
        self._monitor_nodes.clear()
        self._monitor_errors.clear()
        self._finished_monitors.clear()
        self._pending_initial_activations.clear()
        self._started = False
        self._store.close()
        self._event_store.close()

    def _create_initial_state(
        self, request: CoordinatorActivationRequest
    ) -> tuple[CoordinatorSession, AgentSession]:
        cognitive_session_id = request.cognitive_session_id or f"maf:{request.session_id}"
        at = self._now()
        goal = Goal(
            goal_id=f"goal:{request.session_id}",
            objective=request.prompt,
            acceptance_criteria=request.acceptance_criteria,
            constraints=request.constraints,
            status=GoalStatus.ACTIVE,
            created_at=at,
            updated_at=at,
        )
        session = CoordinatorSession.create(
            session_id=request.session_id,
            cognitive_session_id=cognitive_session_id,
            at=at,
        ).start_goal(goal, at=at)
        return session, AgentSession(session_id=cognitive_session_id)

    @staticmethod
    def _validate_existing_session(
        session: CoordinatorSession, request: CoordinatorActivationRequest
    ) -> None:
        CoordinatorService._ensure_session_unarchived(session)
        if (
            request.cognitive_session_id is not None
            and request.cognitive_session_id != session.cognitive_session_id
        ):
            raise CoordinatorServiceValidationError(
                "cognitive_session_id does not match the persisted session"
            )

    @staticmethod
    def _ensure_session_unarchived(session: CoordinatorSession) -> None:
        if session.archived_at is not None:
            raise CoordinatorServiceValidationError(
                "Coordinator session is archived; unarchive it before making changes"
            )

    def _save_result(
        self,
        previous: CoordinatorSessionRecord,
        session: CoordinatorSession,
        agent_session: AgentSession,
    ) -> None:
        pending_event_activation = previous.pending_event_activation
        if session.goal is not None and session.goal.status is not GoalStatus.ACTIVE:
            pending_event_activation = None
        saved = self._store.save(
            CoordinatorSessionRecord(
                coordinator_session=session,
                agent_session=agent_session,
                working_directory=previous.working_directory,
                pending_event_activation=pending_event_activation,
            ),
            expected_version=previous.version,
        )
        self._sync_monitors(saved)

    def _sync_monitors(self, record: CoordinatorSessionRecord) -> None:
        if (
            not self._started
            or self._event_bridge is None
            or record.coordinator_session.archived_at is not None
        ):
            return
        plan = record.coordinator_session.plan
        if plan is None:
            return
        for node in plan.nodes:
            if node.execution is None:
                continue
            key = (record.session_id, node.execution.delegation_id)
            self._monitor_nodes[key] = node.node_id
            if (
                node.status in _TERMINAL_MONITOR_NODE_STATUSES
                and record.pending_event_activation is None
            ):
                self._finished_monitors.add(key)
                continue
            task = self._monitor_tasks.get(key)
            if key in self._finished_monitors or (task is not None and not task.done()):
                continue
            self._monitor_tasks[key] = asyncio.create_task(
                self._monitor_loop(record.session_id, node.node_id, node.execution.delegation_id),
                name=f"coordinator-monitor:{record.session_id}:{node.execution.delegation_id}",
            )

    async def _monitor_loop(
        self,
        session_id: str,
        node_id: str,
        delegation_id: str,
    ) -> None:
        key = (session_id, delegation_id)
        while not self._stop_event.is_set():
            try:
                if self._monitor_is_terminal(session_id, node_id):
                    self._finished_monitors.add(key)
                    return
                refreshed = await self._refresh_node_snapshot(
                    session_id=session_id,
                    node_id=node_id,
                )
                if refreshed is not None and refreshed.status.terminal:
                    self._finished_monitors.add(key)
                    return
                finished = await self._consume_monitor(session_id, node_id, delegation_id)
                self._monitor_errors.pop(key, None)
                if finished or key in self._finished_monitors:
                    self._finished_monitors.add(key)
                    return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._monitor_errors[key] = str(error)
                try:
                    refreshed = await self._refresh_node_snapshot(
                        session_id=session_id,
                        node_id=node_id,
                    )
                except Exception:
                    refreshed = None
                terminal_snapshot = refreshed is not None and refreshed.status.terminal
                if terminal_snapshot or self._monitor_is_terminal(session_id, node_id):
                    self._monitor_errors.pop(key, None)
                    self._finished_monitors.add(key)
                    return
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._event_retry_seconds,
                    )
                except TimeoutError:
                    continue

    def _monitor_is_terminal(self, session_id: str, node_id: str) -> bool:
        try:
            record = self.get(session_id)
        except CoordinatorServiceError:
            return False
        if record.coordinator_session.archived_at is not None:
            return True
        if record.pending_event_activation is not None:
            return False
        plan = record.coordinator_session.plan
        if plan is None:
            return False
        for node in plan.nodes:
            if node.node_id == node_id:
                return node.status in _TERMINAL_MONITOR_NODE_STATUSES
        return False

    async def _refresh_node_snapshot(
        self,
        *,
        session_id: str,
        node_id: str,
    ) -> DelegationSnapshot | None:
        normalized_session_id = ensure_text(session_id, "session_id")
        normalized_node_id = ensure_text(node_id, "node_id")
        lock = self._lock_for(normalized_session_id)
        async with lock:
            record = self.get(normalized_session_id)
            plan = record.coordinator_session.plan
            if plan is None:
                return None
            node = next(
                (candidate for candidate in plan.nodes if candidate.node_id == normalized_node_id),
                None,
            )
            if node is None or node.execution is None:
                return None
            expected_version = record.version
            session = record.coordinator_session

        snapshot = await self._orchestrator.inspect_node(
            session=session,
            node_id=normalized_node_id,
        )
        if session.archived_at is not None:
            return snapshot

        async with lock:
            latest = self.get(normalized_session_id)
            if latest.coordinator_session.archived_at is not None:
                return snapshot
            latest_plan = latest.coordinator_session.plan
            latest_node = (
                None
                if latest_plan is None
                else next(
                    (
                        candidate
                        for candidate in latest_plan.nodes
                        if candidate.node_id == normalized_node_id
                    ),
                    None,
                )
            )
            if (
                latest.version != expected_version
                or latest_node is None
                or latest_node.execution != node.execution
            ):
                return snapshot
            updated = self._orchestrator.observe_snapshot(
                session=latest.coordinator_session,
                node_id=normalized_node_id,
                snapshot=snapshot,
                at=self._now(),
            )
            if updated != latest.coordinator_session:
                saved = self._store.save(
                    CoordinatorSessionRecord(
                        coordinator_session=updated,
                        agent_session=latest.agent_session,
                        working_directory=latest.working_directory,
                        pending_event_activation=latest.pending_event_activation,
                    ),
                    expected_version=latest.version,
                )
                self._sync_monitors(saved)
            return snapshot

    async def _consume_monitor(
        self,
        session_id: str,
        node_id: str,
        delegation_id: str,
    ) -> bool:
        if self._event_bridge is None:
            return True
        lock = self._lock_for(session_id)
        async with lock:
            record = self.get(session_id)
            if record.coordinator_session.archived_at is not None:
                return True
            if record.working_directory is None:
                raise CoordinatorEventRecoveryError(
                    "persisted session has no working directory; activate it once manually"
                )
            if record.pending_event_activation is not None:
                activated = await self._activate_pending_event(record)
                self._sync_monitors(activated)
                return False
            expected_version = record.version
            session = record.coordinator_session
            persisted_session = session
            pending_event_activation: PendingEventActivation | None = None
            updates_since_checkpoint = 0
        async for update in self._event_bridge.consume(
            session,
            delegation_id,
            node_id=node_id,
            at=self._now,
        ):
            async with lock:
                latest = self.get(session_id)
                if latest.version != expected_version:
                    return False
                pending = self._pending_event_activation(update)
                self._append_monitor_event(update, session_id=session_id, node_id=node_id)
                persisted_session = update.session
                pending_event_activation = pending
                updates_since_checkpoint += 1
                if not self._should_checkpoint_monitor_update(
                    update,
                    pending=pending,
                    updates_since_checkpoint=updates_since_checkpoint,
                ):
                    continue
                saved = self._store.save(
                    CoordinatorSessionRecord(
                        coordinator_session=update.session,
                        agent_session=latest.agent_session,
                        working_directory=latest.working_directory,
                        pending_event_activation=pending,
                    ),
                    expected_version=latest.version,
                )
                expected_version = saved.version
                persisted_session = saved.coordinator_session
                updates_since_checkpoint = 0
                if pending is not None:
                    activated = await self._activate_pending_event(saved)
                    self._sync_monitors(activated)
                    return False
        if persisted_session != session or pending_event_activation is not None:
            async with lock:
                latest = self.get(session_id)
                if latest.version != expected_version:
                    return False
                saved = self._store.save(
                    CoordinatorSessionRecord(
                        coordinator_session=persisted_session,
                        agent_session=latest.agent_session,
                        working_directory=latest.working_directory,
                        pending_event_activation=pending_event_activation,
                    ),
                    expected_version=latest.version,
                )
                self._sync_monitors(saved)
        return True

    @staticmethod
    def _should_checkpoint_monitor_update(
        update: CoordinatorEventUpdate,
        *,
        pending: PendingEventActivation | None,
        updates_since_checkpoint: int,
    ) -> bool:
        if pending is not None or updates_since_checkpoint >= _MONITOR_CHECKPOINT_INTERVAL:
            return True
        source_event = update.source_event
        if source_event is None:
            return True
        kind = str(source_event.kind).lower()
        if kind not in _TRANSIENT_MONITOR_EVENT_KINDS:
            return True
        status = (source_event.status or "").lower()
        return status in {"completed", "failed", "cancelled", "reconciliation_required"}

    def _append_event(
        self,
        session_id: str,
        event_type: str,
        payload: Mapping[str, object],
        *,
        occurred_at: datetime | None = None,
    ) -> CoordinatorSessionEvent:
        return self._event_store.append(
            session_id,
            event_type,
            cast(Mapping[str, JsonValue], payload),
            occurred_at=occurred_at,
        )

    def _append_monitor_event(
        self,
        update: CoordinatorEventUpdate,
        *,
        session_id: str,
        node_id: str,
    ) -> None:
        if update.coordinator_event is None or update.source_event is None:
            return
        source = update.source_event
        self._append_event(
            session_id,
            "delegation.event",
            {
                "node_id": node_id,
                "delegation_id": update.delegation_id,
                "event": update.coordinator_event.to_dict(),
                "source": {
                    "sequence": source.sequence,
                    "kind": source.kind,
                    "status": source.status,
                    "activation_id": source.activation_id,
                    "invocation_id": source.invocation_id,
                    "payload": source.payload,
                },
                "activation_required": update.activation_required,
            },
            occurred_at=source.occurred_at,
        )

    def _pending_event_activation(
        self, update: CoordinatorEventUpdate
    ) -> PendingEventActivation | None:
        if not update.activation_required:
            return None
        if update.coordinator_event is None or update.source_event is None:
            raise CoordinatorEventRecoveryError(
                "activation-required event update is missing its source context"
            )
        event = update.coordinator_event
        source_event = update.source_event
        return PendingEventActivation(
            delegation_id=update.delegation_id,
            sequence=source_event.sequence,
            activation_id=ensure_text(self._activation_id_factory(), "activation_id"),
            event_type=event.event_type.value,
            event_id=event.event_id,
            external_event_id=event.external_event_id,
            source_event_kind=source_event.kind,
            source_event_status=source_event.status,
            source_event_payload=source_event.payload,
        )

    async def _activate_pending_event(
        self, record: CoordinatorSessionRecord
    ) -> CoordinatorSessionRecord:
        pending = record.pending_event_activation
        if pending is None or self._event_bridge is None:
            return record
        if record.working_directory is None:
            raise CoordinatorEventRecoveryError(
                "persisted session has no working directory; activate it once manually"
            )
        activation = await self._orchestrator.activate(
            self._event_bridge.activation_prompt(
                self._event_activation_prompt(record.coordinator_session),
                event_type=pending.event_type,
                event_id=pending.event_id,
                external_event_id=pending.external_event_id,
                delegation_id=pending.delegation_id,
                source_event_kind=pending.source_event_kind,
                source_event_status=pending.source_event_status,
                source_event_payload=pending.source_event_payload,
            ),
            session=record.coordinator_session,
            agent_session=record.agent_session,
            activation_id=pending.activation_id,
            at=self._now(),
            cwd=record.working_directory,
        )
        return self._store.save(
            CoordinatorSessionRecord(
                coordinator_session=activation.session,
                agent_session=activation.agent_session,
                working_directory=record.working_directory,
                pending_event_activation=None,
            ),
            expected_version=record.version,
        )

    @staticmethod
    def _event_activation_prompt(session: CoordinatorSession) -> str:
        if session.goal is not None:
            return f"Continue the active goal: {session.goal.objective}"
        return "Continue processing the active delegated work"

    def _now(self) -> datetime:
        return self._clock().astimezone(UTC)

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        normalized = ensure_text(session_id, "session_id")
        with self._locks_guard:
            lock = self._locks.get(normalized)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[normalized] = lock
            return lock


def _delegation_payload(snapshot: DelegationSnapshot) -> dict[str, object]:
    # Kept deliberately projection-only: V3 remains the source of execution facts.
    return {
        "delegation_id": snapshot.delegation_id,
        "status": snapshot.status.value,
        "revision": snapshot.revision,
        "session_id": snapshot.session_id,
        "current_activation_id": snapshot.current_activation_id,
        "current_invocation_id": snapshot.current_invocation_id,
        "next_action": snapshot.next_action,
        "timed_out": snapshot.timed_out,
        "waited_ms": snapshot.waited_ms,
    }


def _dispatch_payload(result: CoordinatorMessageResult) -> dict[str, object]:
    dispatch = result.dispatch
    return {
        "dispatch_id": dispatch.dispatch_id,
        "delegation_id": dispatch.delegation_id,
        "status": dispatch.status,
        "revision": dispatch.revision,
        "applied_strategy": dispatch.applied_strategy,
        "previous_activation_id": dispatch.previous_activation_id,
        "current_activation_id": dispatch.current_activation_id,
        "error_code": dispatch.error_code,
        "error_message": dispatch.error_message,
    }


def _snapshot_payload(snapshot: DelegationSnapshot) -> dict[str, object]:
    return {
        "delegation_id": snapshot.delegation_id,
        "status": snapshot.status.value,
        "revision": snapshot.revision,
        "session_id": snapshot.session_id,
        "channel_id": snapshot.channel_id,
        "parent_delegation_id": snapshot.parent_delegation_id,
        "depth": snapshot.depth,
        "child_scope": None,
        "current_activation_id": snapshot.current_activation_id,
        "current_invocation_id": snapshot.current_invocation_id,
        "activation_count": snapshot.activation_count,
        "child_delegation_ids": list(snapshot.child_delegation_ids),
        "report": None if snapshot.report is None else _report_payload(snapshot.report),
        "next_action": snapshot.next_action,
        "timed_out": snapshot.timed_out,
        "waited_ms": snapshot.waited_ms,
    }


def _report_payload(report: DelegationReport) -> dict[str, object]:
    return {
        "status": report.status.value,
        "output": report.output,
        "artifact_ids": list(report.artifact_ids),
        "error_code": report.error_code,
        "error_message": report.error_message,
        "source_invocation_id": report.source_invocation_id,
        "source_activation_id": report.source_activation_id,
        "resolution_reason": None,
        "resolved_by": None,
        "created_at": report.created_at.isoformat(),
    }

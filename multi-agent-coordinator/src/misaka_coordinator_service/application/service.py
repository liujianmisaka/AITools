from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from agent_framework import AgentSession

from misaka_coordinator_service.application.events import (
    CoordinatorEventBridge,
    CoordinatorEventRecoveryError,
    CoordinatorEventUpdate,
)
from misaka_coordinator_service.application.orchestrator import (
    CoordinatorActivationResult,
    CoordinatorCancellationResult,
    CoordinatorMessageResult,
    CoordinatorOrchestrator,
    CoordinatorReconciliationResult,
)
from misaka_coordinator_service.domain import (
    CoordinatorSession,
    Goal,
    GoalStatus,
)
from misaka_coordinator_service.domain._serialization import ensure_text, ensure_text_tuple
from misaka_coordinator_service.execution import (
    DelegationSnapshot,
    JsonValue,
    MessageDelivery,
    ReconciliationStatus,
)
from misaka_coordinator_service.persistence import (
    CoordinatorSessionRecord,
    PendingEventActivation,
)


class CoordinatorServiceError(RuntimeError):
    """Base error for the Coordinator application service."""


class CoordinatorServiceValidationError(CoordinatorServiceError):
    """Raised when an application request violates the service contract."""


class CoordinatorServiceNotFoundError(CoordinatorServiceError):
    """Raised when a requested Coordinator session does not exist."""


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
        event_retry_seconds: float = 2.0,
    ) -> None:
        self._orchestrator = orchestrator
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._activation_id_factory = activation_id_factory or (lambda: f"activation-{uuid4().hex}")
        self._event_bridge = event_bridge
        if event_retry_seconds <= 0:
            raise CoordinatorServiceValidationError("event_retry_seconds must be positive")
        self._event_retry_seconds = event_retry_seconds
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = threading.RLock()
        self._monitor_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._monitor_nodes: dict[tuple[str, str], str] = {}
        self._monitor_errors: dict[tuple[str, str], str] = {}
        self._finished_monitors: set[tuple[str, str]] = set()
        self._stop_event = asyncio.Event()
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for session_id in self._store.list_session_ids():
            record = self._store.load(session_id)
            if record is not None:
                self._sync_monitors(record)

    def get(self, session_id: str) -> CoordinatorSessionRecord:
        normalized = ensure_text(session_id, "session_id")
        record = self._store.load(normalized)
        if record is None:
            raise CoordinatorServiceNotFoundError(f"coordinator session {normalized} was not found")
        return record

    def list_session_ids(self) -> tuple[str, ...]:
        return self._store.list_session_ids()

    def monitor_statuses(self) -> tuple[CoordinatorMonitorStatus, ...]:
        statuses: list[CoordinatorMonitorStatus] = []
        for key, node_id in sorted(self._monitor_nodes.items()):
            task = self._monitor_tasks.get(key)
            statuses.append(
                CoordinatorMonitorStatus(
                    session_id=key[0],
                    node_id=node_id,
                    delegation_id=key[1],
                    running=task is not None and not task.done(),
                    last_error=self._monitor_errors.get(key),
                )
            )
        return tuple(statuses)

    async def activate(self, request: CoordinatorActivationRequest) -> CoordinatorServiceActivation:
        lock = self._lock_for(request.session_id)
        async with lock:
            record = self._store.load(request.session_id)
            if record is None:
                current, agent_session = self._create_initial_state(request)
                expected_version = 0
            else:
                current = record.coordinator_session
                agent_session = record.agent_session
                expected_version = record.version
                self._validate_existing_session(current, request)
            activation_id = request.activation_id or ensure_text(
                self._activation_id_factory(), "activation_id"
            )
            result = await self._orchestrator.activate(
                request.prompt,
                session=current,
                agent_session=agent_session,
                activation_id=activation_id,
                at=self._now(),
                cwd=request.cwd,
            )
            saved = self._store.save(
                CoordinatorSessionRecord(
                    coordinator_session=result.session,
                    agent_session=result.agent_session,
                    working_directory=request.cwd,
                    pending_event_activation=(
                        None if record is None else record.pending_event_activation
                    ),
                ),
                expected_version=expected_version,
            )
            self._sync_monitors(saved)
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
            return result

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
            self._save_result(record, result.session, record.agent_session)
            return result

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
        self._started = False
        self._store.close()

    def close(self) -> None:
        self._stop_event.set()
        for task in self._monitor_tasks.values():
            task.cancel()
        self._monitor_tasks.clear()
        self._monitor_nodes.clear()
        self._monitor_errors.clear()
        self._finished_monitors.clear()
        self._started = False
        self._store.close()

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
        if (
            request.cognitive_session_id is not None
            and request.cognitive_session_id != session.cognitive_session_id
        ):
            raise CoordinatorServiceValidationError(
                "cognitive_session_id does not match the persisted session"
            )

    def _save_result(
        self,
        previous: CoordinatorSessionRecord,
        session: CoordinatorSession,
        agent_session: AgentSession,
    ) -> None:
        saved = self._store.save(
            CoordinatorSessionRecord(
                coordinator_session=session,
                agent_session=agent_session,
                working_directory=previous.working_directory,
                pending_event_activation=previous.pending_event_activation,
            ),
            expected_version=previous.version,
        )
        self._sync_monitors(saved)

    def _sync_monitors(self, record: CoordinatorSessionRecord) -> None:
        if not self._started or self._event_bridge is None:
            return
        plan = record.coordinator_session.plan
        if plan is None:
            return
        for node in plan.nodes:
            if node.execution is None:
                continue
            key = (record.session_id, node.execution.delegation_id)
            self._monitor_nodes[key] = node.node_id
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
                finished = await self._consume_monitor(session_id, node_id, delegation_id)
                self._monitor_errors.pop(key, None)
                if finished:
                    self._finished_monitors.add(key)
                    return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._monitor_errors[key] = str(error)
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._event_retry_seconds,
                    )
                except TimeoutError:
                    continue

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
                if pending is not None:
                    activated = await self._activate_pending_event(saved)
                    self._sync_monitors(activated)
                    return False
        return True

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

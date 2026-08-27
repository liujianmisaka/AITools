from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from agent_framework import AgentSession

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
from misaka_coordinator_service.persistence import CoordinatorSessionRecord


class CoordinatorServiceError(RuntimeError):
    """Base error for the Coordinator application service."""


class CoordinatorServiceValidationError(CoordinatorServiceError):
    """Raised when an application request violates the service contract."""


class CoordinatorServiceNotFoundError(CoordinatorServiceError):
    """Raised when a requested Coordinator session does not exist."""


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
    ) -> None:
        self._orchestrator = orchestrator
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._activation_id_factory = activation_id_factory or (lambda: f"activation-{uuid4().hex}")
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = threading.RLock()

    def get(self, session_id: str) -> CoordinatorSessionRecord:
        normalized = ensure_text(session_id, "session_id")
        record = self._store.load(normalized)
        if record is None:
            raise CoordinatorServiceNotFoundError(f"coordinator session {normalized} was not found")
        return record

    def list_session_ids(self) -> tuple[str, ...]:
        return self._store.list_session_ids()

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
            self._store.save(
                CoordinatorSessionRecord(
                    coordinator_session=result.session,
                    agent_session=result.agent_session,
                ),
                expected_version=expected_version,
            )
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

    def close(self) -> None:
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
        self._store.save(
            CoordinatorSessionRecord(
                coordinator_session=session,
                agent_session=agent_session,
            ),
            expected_version=previous.version,
        )

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

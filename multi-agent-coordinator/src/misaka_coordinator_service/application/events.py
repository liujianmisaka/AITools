from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from agent_framework import AgentSession

from misaka_coordinator_service.domain import (
    CoordinatorEvent,
    CoordinatorEventType,
    CoordinatorSession,
    ExecutionEventCursor,
    ExecutionReference,
)
from misaka_coordinator_service.domain._serialization import ensure_text
from misaka_coordinator_service.execution import (
    DelegationSessionEvent,
    DelegationSessionSnapshot,
    DelegationSnapshot,
    SessionStreamEvent,
    SessionStreamEventKind,
)

if TYPE_CHECKING:
    from misaka_coordinator_service.application.orchestrator import (
        CoordinatorActivationResult,
        CoordinatorOrchestrator,
    )

type EventClock = datetime | Callable[[], datetime]


class CoordinatorEventRecoveryError(RuntimeError):
    """Raised when a V3 event stream cannot be resumed safely."""


class _StreamDisconnected(RuntimeError):
    """Internal marker for a transport closing before the end envelope."""


class CoordinatorEventUpdateKind(StrEnum):
    SNAPSHOT = "snapshot"
    EVENT = "event"
    END = "end"


@dataclass(frozen=True, slots=True)
class CoordinatorEventBridgeConfig:
    max_reconnects: int = 3
    activation_statuses: tuple[str, ...] = (
        "completed",
        "failed",
        "cancelled",
        "reconciliation_required",
        "waiting_input",
    )
    activation_kinds: tuple[str, ...] = (
        "completed",
        "failed",
        "cancelled",
        "output",
        "report",
        "result",
        "waiting_input",
        "agent_question",
        "question",
    )

    def __post_init__(self) -> None:
        if isinstance(self.max_reconnects, bool) or not 0 <= self.max_reconnects <= 20:
            raise CoordinatorEventRecoveryError("max_reconnects must be between 0 and 20")
        object.__setattr__(
            self,
            "activation_statuses",
            tuple(
                ensure_text(value, "activation_statuses[]").lower()
                for value in self.activation_statuses
            ),
        )
        object.__setattr__(
            self,
            "activation_kinds",
            tuple(
                ensure_text(value, "activation_kinds[]").lower() for value in self.activation_kinds
            ),
        )


class SessionEventSource(Protocol):
    async def list_events(
        self,
        delegation_id: str,
        *,
        next_sequence: int = 1,
    ) -> tuple[DelegationSessionEvent, ...]: ...

    def stream_events(
        self,
        delegation_id: str,
        *,
        next_sequence: int = 1,
    ) -> AsyncIterator[SessionStreamEvent]: ...


class SnapshotObserver(Protocol):
    def observe_snapshot(
        self,
        *,
        session: CoordinatorSession,
        node_id: str,
        snapshot: DelegationSnapshot,
        at: datetime,
    ) -> CoordinatorSession: ...


class EventObserver(Protocol):
    def observe_event(
        self,
        *,
        session: CoordinatorSession,
        node_id: str,
        source_event: DelegationSessionEvent,
        at: datetime,
    ) -> CoordinatorSession: ...


@dataclass(frozen=True, slots=True)
class CoordinatorEventUpdate:
    session: CoordinatorSession
    delegation_id: str
    kind: CoordinatorEventUpdateKind
    cursor: ExecutionEventCursor
    coordinator_event: CoordinatorEvent | None = None
    source_event: DelegationSessionEvent | None = None
    snapshot: DelegationSessionSnapshot | None = None
    activation_required: bool = False

    def __post_init__(self) -> None:
        if self.cursor.delegation_id != self.delegation_id:
            raise CoordinatorEventRecoveryError("event update cursor delegation_id must match")
        if self.kind is CoordinatorEventUpdateKind.EVENT and (
            self.coordinator_event is None or self.source_event is None
        ):
            raise CoordinatorEventRecoveryError(
                "event update requires source and coordinator events"
            )
        if self.kind is CoordinatorEventUpdateKind.EVENT and self.snapshot is not None:
            raise CoordinatorEventRecoveryError("event update cannot contain a snapshot")
        if self.kind is CoordinatorEventUpdateKind.SNAPSHOT and self.snapshot is None:
            raise CoordinatorEventRecoveryError("snapshot update requires a snapshot")
        if self.kind is CoordinatorEventUpdateKind.SNAPSHOT and (
            self.coordinator_event is not None or self.source_event is not None
        ):
            raise CoordinatorEventRecoveryError(
                "snapshot update cannot contain a Coordinator event"
            )
        if self.kind is CoordinatorEventUpdateKind.END and (
            self.coordinator_event is not None
            or self.source_event is not None
            or self.snapshot is not None
        ):
            raise CoordinatorEventRecoveryError("end update cannot contain an event payload")
        if self.kind is CoordinatorEventUpdateKind.END and self.activation_required:
            raise CoordinatorEventRecoveryError("end update cannot trigger activation")


@dataclass(frozen=True, slots=True)
class CoordinatorEventRecoveryResult:
    delegation_id: str
    session: CoordinatorSession
    updates: tuple[CoordinatorEventUpdate, ...]

    @property
    def cursor(self) -> ExecutionEventCursor:
        return self.session.event_cursor_for(self.delegation_id)


class CoordinatorEventBridge:
    """Replay and resume one V3 delegation event stream into Coordinator state."""

    def __init__(
        self,
        *,
        source: SessionEventSource,
        snapshot_observer: SnapshotObserver | None = None,
        event_observer: EventObserver | None = None,
        config: CoordinatorEventBridgeConfig | None = None,
    ) -> None:
        self._source = source
        self._snapshot_observer = snapshot_observer
        self._event_observer = event_observer
        self._config = config or CoordinatorEventBridgeConfig()

    async def replay(
        self,
        session: CoordinatorSession,
        delegation_id: str,
        *,
        node_id: str | None = None,
        at: EventClock,
    ) -> CoordinatorEventRecoveryResult:
        normalized_delegation_id = ensure_text(delegation_id, "delegation_id")
        current = session
        cursor = current.event_cursor_for(normalized_delegation_id)
        source_events = await self._source.list_events(
            normalized_delegation_id,
            next_sequence=cursor.next_sequence,
        )
        updates: list[CoordinatorEventUpdate] = []
        for source_event in source_events:
            update = self._apply_source_event(
                current,
                source_event,
                node_id=node_id,
                at=at,
            )
            if update is not None:
                current = update.session
                updates.append(update)
        return CoordinatorEventRecoveryResult(
            delegation_id=normalized_delegation_id,
            session=current,
            updates=tuple(updates),
        )

    async def consume(
        self,
        session: CoordinatorSession,
        delegation_id: str,
        *,
        node_id: str | None = None,
        at: EventClock,
    ) -> AsyncIterator[CoordinatorEventUpdate]:
        normalized_delegation_id = ensure_text(delegation_id, "delegation_id")
        current = session
        replayed = await self.replay(current, normalized_delegation_id, node_id=node_id, at=at)
        for update in replayed.updates:
            current = update.session
            yield update

        reconnects = 0
        while True:
            cursor = current.event_cursor_for(normalized_delegation_id)
            saw_end = False
            saw_frame = False
            try:
                async for frame in self._source.stream_events(
                    normalized_delegation_id,
                    next_sequence=cursor.next_sequence,
                ):
                    saw_frame = True
                    update = self._apply_stream_frame(
                        current,
                        frame,
                        delegation_id=normalized_delegation_id,
                        node_id=node_id,
                        at=at,
                    )
                    if update is not None:
                        current = update.session
                        yield update
                    if frame.kind is SessionStreamEventKind.END:
                        saw_end = True
                        break
                if saw_end:
                    return
                raise _StreamDisconnected("V3 event stream ended without an end envelope")
            except CoordinatorEventRecoveryError:
                raise
            except Exception as error:
                if reconnects >= self._config.max_reconnects:
                    raise CoordinatorEventRecoveryError(
                        "V3 event stream reconnect limit reached"
                    ) from error
                reconnects += 1
                replayed = await self.replay(
                    current,
                    normalized_delegation_id,
                    node_id=node_id,
                    at=at,
                )
                for update in replayed.updates:
                    current = update.session
                    yield update
                if saw_frame or replayed.updates:
                    reconnects = 0

    async def activate(
        self,
        update: CoordinatorEventUpdate,
        *,
        orchestrator: CoordinatorOrchestrator,
        prompt: str,
        agent_session: AgentSession,
        activation_id: str,
        at: datetime,
        cwd: str | None = None,
    ) -> CoordinatorActivationResult | None:
        if not update.activation_required or update.coordinator_event is None:
            return None
        event = update.coordinator_event
        activation_prompt = self.activation_prompt(
            prompt,
            event_type=event.event_type.value,
            event_id=event.event_id,
            external_event_id=event.external_event_id,
            delegation_id=update.delegation_id,
            source_event_kind=update.source_event.kind if update.source_event else None,
            source_event_status=update.source_event.status if update.source_event else None,
            source_event_payload=(
                update.source_event.payload if update.source_event is not None else None
            ),
        )
        return await orchestrator.activate(
            activation_prompt,
            session=update.session,
            agent_session=agent_session,
            activation_id=activation_id,
            at=at,
            cwd=cwd,
        )

    @staticmethod
    def activation_prompt(
        prompt: str,
        *,
        event_type: str,
        event_id: str,
        external_event_id: str | None,
        delegation_id: str,
        source_event_kind: str | None,
        source_event_status: str | None,
        source_event_payload: Mapping[str, object] | None = None,
    ) -> str:
        event_context = {
            "event_type": event_type,
            "event_id": event_id,
            "external_event_id": external_event_id,
            "delegation_id": delegation_id,
            "source_event_kind": source_event_kind,
            "source_event_status": source_event_status,
            "source_event_payload": dict(source_event_payload or {}),
        }
        return f"{prompt}\nProcess this newly observed execution event: " + json.dumps(
            event_context,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def _apply_source_event(
        self,
        session: CoordinatorSession,
        source_event: DelegationSessionEvent,
        *,
        node_id: str | None,
        at: EventClock,
    ) -> CoordinatorEventUpdate | None:
        delegation_id = ensure_text(source_event.delegation_id, "delegation_id")
        current_cursor = session.event_cursor_for(delegation_id)
        if source_event.sequence < current_cursor.next_sequence:
            return None
        if source_event.sequence > current_cursor.next_sequence:
            raise CoordinatorEventRecoveryError("V3 event replay contains a sequence gap")
        resolved_node_id = node_id or self._node_id_for_delegation(session, delegation_id)
        event = self._map_event(session, source_event, node_id=resolved_node_id)
        observed_at = _event_time(at)
        try:
            current = session.record_external_event(
                event,
                delegation_id=delegation_id,
                sequence=source_event.sequence,
                at=observed_at,
            )
        except ValueError as error:
            raise CoordinatorEventRecoveryError(
                "V3 event violates Coordinator cursor rules"
            ) from error
        if self._event_observer is not None and resolved_node_id is not None:
            current = self._event_observer.observe_event(
                session=current,
                node_id=resolved_node_id,
                source_event=source_event,
                at=observed_at,
            )
        return CoordinatorEventUpdate(
            session=current,
            delegation_id=delegation_id,
            kind=CoordinatorEventUpdateKind.EVENT,
            cursor=current.event_cursor_for(delegation_id),
            coordinator_event=event,
            source_event=source_event,
            activation_required=self._requires_activation(source_event),
        )

    def _apply_stream_frame(
        self,
        session: CoordinatorSession,
        frame: SessionStreamEvent,
        *,
        delegation_id: str,
        node_id: str | None,
        at: EventClock,
    ) -> CoordinatorEventUpdate | None:
        if frame.kind is SessionStreamEventKind.SNAPSHOT:
            snapshot = frame.snapshot
            if snapshot is None:
                raise CoordinatorEventRecoveryError("snapshot envelope has no snapshot")
            if snapshot.delegation.delegation_id != delegation_id:
                raise CoordinatorEventRecoveryError(
                    "snapshot delegation_id does not match the stream"
                )
            current = session
            resolved_node_id = node_id or self._node_id_for_delegation(session, delegation_id)
            if self._snapshot_observer is not None and resolved_node_id is not None:
                observed_at = _event_time(at)
                current = self._snapshot_observer.observe_snapshot(
                    session=current,
                    node_id=resolved_node_id,
                    snapshot=snapshot.delegation,
                    at=observed_at,
                )
            return CoordinatorEventUpdate(
                session=current,
                delegation_id=delegation_id,
                kind=CoordinatorEventUpdateKind.SNAPSHOT,
                cursor=current.event_cursor_for(delegation_id),
                snapshot=snapshot,
            )
        if frame.kind is SessionStreamEventKind.EVENT:
            source_event = frame.session_event
            if source_event is None:
                raise CoordinatorEventRecoveryError("event envelope has no session event")
            if source_event.delegation_id != delegation_id:
                raise CoordinatorEventRecoveryError("event delegation_id does not match the stream")
            return self._apply_source_event(session, source_event, node_id=node_id, at=at)
        if frame.kind is SessionStreamEventKind.END:
            next_sequence = frame.next_sequence
            if next_sequence is None:
                raise CoordinatorEventRecoveryError("end envelope has no next sequence")
            cursor = session.event_cursor_for(delegation_id)
            if next_sequence != cursor.next_sequence:
                raise CoordinatorEventRecoveryError(
                    "V3 event stream ended with an unconsumed sequence"
                )
            return CoordinatorEventUpdate(
                session=session,
                delegation_id=delegation_id,
                kind=CoordinatorEventUpdateKind.END,
                cursor=cursor,
            )
        raise CoordinatorEventRecoveryError(f"unsupported stream frame kind {frame.kind!r}")

    @staticmethod
    def _node_id_for_delegation(session: CoordinatorSession, delegation_id: str) -> str | None:
        if session.plan is None:
            return None
        for node in session.plan.nodes:
            if node.execution is not None and node.execution.delegation_id == delegation_id:
                return node.node_id
        return None

    @staticmethod
    def _map_event(
        session: CoordinatorSession,
        source_event: DelegationSessionEvent,
        *,
        node_id: str | None,
    ) -> CoordinatorEvent:
        base_execution = None
        if session.plan is not None and node_id is not None:
            base_execution = next(
                (
                    node.execution
                    for node in session.plan.nodes
                    if node.node_id == node_id and node.execution is not None
                ),
                None,
            )
        execution = ExecutionReference(
            delegation_id=source_event.delegation_id,
            activation_id=source_event.activation_id
            or (None if base_execution is None else base_execution.activation_id),
            invocation_id=source_event.invocation_id
            or (None if base_execution is None else base_execution.invocation_id),
            worker_session_id=None if base_execution is None else base_execution.worker_session_id,
        )
        return CoordinatorEvent(
            event_id=f"v3:{source_event.delegation_id}:{source_event.sequence}",
            session_id=session.session_id,
            event_type=CoordinatorEventBridge._event_type(source_event),
            source="multi-agent-v3",
            occurred_at=source_event.occurred_at,
            node_id=node_id,
            execution=execution if node_id is not None else None,
            external_event_id=f"{source_event.delegation_id}:{source_event.sequence}",
        )

    def _requires_activation(self, source_event: DelegationSessionEvent) -> bool:
        status = (source_event.status or "").lower()
        kind = source_event.kind.lower()
        return status in self._config.activation_statuses or kind in self._config.activation_kinds

    @staticmethod
    def _event_type(source_event: DelegationSessionEvent) -> CoordinatorEventType:
        status = (source_event.status or "").lower()
        kind = source_event.kind.lower()
        if status in {"completed", "reconciliation_required"} or kind in {
            "completed",
            "output",
            "report",
            "result",
        }:
            return CoordinatorEventType.OUTPUT_AVAILABLE
        return CoordinatorEventType.DELEGATION_CHANGED


def _event_time(value: EventClock) -> datetime:
    return value() if callable(value) else value

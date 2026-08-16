from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Literal

from pydantic import Field

from multi_agent_v2.packages.agent_runtime.errors import AgentRuntimeError
from multi_agent_v2.packages.agent_runtime.models import (
    AgentCancelledEvent,
    AgentCompletedEvent,
    AgentErrorInfo,
    AgentEvent,
    AgentExecutionRequest,
    AgentFailedEvent,
    AgentMessageDeltaEvent,
    AgentModel,
    AgentModelCatalog,
    AgentModelSpec,
    AgentReconcileRequest,
    AgentResumeRequest,
    AgentRuntimeCapabilities,
    AgentRuntimeDescription,
    AgentStartedEvent,
    AgentTurnHandle,
    CancelResult,
    PreparedAgentSession,
    ReconcileResult,
    logical_agent_request_key,
)
from multi_agent_v2.packages.agent_runtime.stream_contract import validate_agent_output
from multi_agent_v2.packages.domain.json_types import JsonObject


class FakeScenario(AgentModel):
    deltas: tuple[str, ...] = ()
    output: JsonObject = Field(default_factory=dict)
    delay_seconds: float = Field(default=0.0, ge=0.0, le=30.0)
    error: AgentErrorInfo | None = None
    incomplete_stream: bool = False


@dataclass(slots=True)
class _PreparedState:
    request: AgentExecutionRequest
    session: PreparedAgentSession


@dataclass(slots=True)
class _TurnState:
    request: AgentExecutionRequest
    handle: AgentTurnHandle
    scenario: FakeScenario
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    stream_started: bool = False
    status: Literal["running", "succeeded", "failed", "cancelled", "uncertain"] = "running"
    last_sequence: int = 0


class FakeRuntime:
    name = "fake"
    runtime_id = "fake:local:v1"
    catalog_revision = "fake-catalog-v1"

    def __init__(
        self,
        *,
        scenarios: Mapping[str, FakeScenario] | None = None,
        reconcile_overrides: Mapping[str, ReconcileResult] | None = None,
    ) -> None:
        self._scenarios = dict(scenarios or {})
        self._reconcile_overrides = dict(reconcile_overrides or {})
        self._prepared: dict[str, _PreparedState] = {}
        self._turns: dict[str, _TurnState] = {}
        self._terminal: dict[str, ReconcileResult] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self.prepare_calls: list[AgentExecutionRequest] = []
        self.start_turn_calls: list[AgentExecutionRequest] = []
        self.steer_calls: list[tuple[str, str]] = []
        self.cancel_count = 0
        self.close_count = 0
        self._catalog = AgentModelCatalog(
            runtime_name=self.name,
            runtime_id=self.runtime_id,
            provider_id=self.name,
            revision=self.catalog_revision,
            models=(
                AgentModelSpec(
                    id="fake/model",
                    label="Fake deterministic model",
                    model_type="fake",
                    efforts=("low", "medium", "high", "ultra"),
                    recommended_effort="medium",
                ),
            ),
        )

    async def describe(self) -> AgentRuntimeDescription:
        return AgentRuntimeDescription(
            name=self.name,
            runtime_id=self.runtime_id,
            available=not self._closed,
            capabilities=AgentRuntimeCapabilities(
                new_session=True,
                resume_session=True,
                stream_events=True,
                steer_running_turn=True,
                cancel_running_turn=True,
                structured_output=True,
                reconcile_execution=True,
                read_only_mode=True,
                workspace_write_mode=True,
            ),
            catalog_revision=self.catalog_revision,
            metadata={"kind": "fake", "local": True},
            error=(
                AgentErrorInfo(code="agent.runtime_closed", message="fake runtime is closed")
                if self._closed
                else None
            ),
        )

    async def list_models(self, *, refresh: bool = False) -> AgentModelCatalog:
        del refresh
        self._ensure_open()
        return self._catalog

    async def validate_request(self, request: AgentExecutionRequest) -> None:
        self._ensure_open()
        self._validate_selection(request)

    async def prepare_session(
        self,
        request: AgentExecutionRequest,
    ) -> PreparedAgentSession:
        self._ensure_open()
        await self.validate_request(request)
        execution_id = request.identity.execution_id
        async with self._lock:
            terminal = self._terminal.get(execution_id)
            if terminal is not None:
                raise AgentRuntimeError(
                    "execution is already terminal",
                    code="agent.execution_already_terminal",
                )
            existing = self._prepared.get(execution_id)
            if existing is not None:
                if logical_agent_request_key(existing.request) != logical_agent_request_key(
                    request
                ):
                    raise AgentRuntimeError(
                        "execution ID was reused with a different request",
                        code="agent.idempotency_conflict",
                    )
                return existing.session
            provider_session_id = (
                request.provider_session_id
                if isinstance(request, AgentResumeRequest)
                else f"fake-session:{execution_id}"
            )
            session = PreparedAgentSession(
                handle_id=f"fake-session-handle:{execution_id}",
                execution_id=execution_id,
                provider=self.name,
                provider_session_id=provider_session_id,
            )
            self._prepared[execution_id] = _PreparedState(request=request, session=session)
            self.prepare_calls.append(request)
            return session

    async def start_turn(
        self,
        request: AgentExecutionRequest,
        session: PreparedAgentSession,
    ) -> AgentTurnHandle:
        self._ensure_open()
        await self.validate_request(request)
        execution_id = request.identity.execution_id
        async with self._lock:
            prepared = self._prepared.get(execution_id)
            if prepared is None or prepared.session != session:
                raise AgentRuntimeError(
                    "turn does not reference the prepared session",
                    code="agent.session_not_prepared",
                )
            if logical_agent_request_key(prepared.request) != logical_agent_request_key(request):
                raise AgentRuntimeError(
                    "prepared session request does not match turn request",
                    code="agent.idempotency_conflict",
                )
            existing = self._turns.get(execution_id)
            if existing is not None:
                return existing.handle
            handle = AgentTurnHandle(
                handle_id=f"fake-turn-handle:{execution_id}",
                execution_id=execution_id,
                provider=self.name,
                provider_session_id=session.provider_session_id,
                provider_turn_id=f"fake-turn:{execution_id}",
            )
            self._turns[execution_id] = _TurnState(
                request=request,
                handle=handle,
                scenario=self._scenarios.get(execution_id, FakeScenario(output={})),
            )
            self.start_turn_calls.append(request)
            return handle

    def stream(self, handle: AgentTurnHandle) -> AsyncIterator[AgentEvent]:
        return self._stream(handle)

    async def _stream(self, handle: AgentTurnHandle) -> AsyncIterator[AgentEvent]:
        state = await self._begin_stream(handle)
        yield await self._event(
            state,
            AgentStartedEvent(
                execution_id=handle.execution_id,
                sequence=1,
                provider_session_id=handle.provider_session_id,
                summary="Fake turn started",
                model=state.request.model,
                effort=state.request.effort,
            ),
        )

        for delta in state.scenario.deltas:
            if state.cancel_event.is_set():
                yield await self._cancelled_event(state)
                return
            yield await self._event(
                state,
                AgentMessageDeltaEvent(
                    execution_id=handle.execution_id,
                    sequence=state.last_sequence + 1,
                    provider_session_id=handle.provider_session_id,
                    summary=delta,
                    text=delta,
                ),
            )

        if await self._wait_for_cancel(state):
            yield await self._cancelled_event(state)
            return
        if state.scenario.incomplete_stream:
            state.status = "uncertain"
            return
        if state.scenario.error is not None:
            event = await self._event(
                state,
                AgentFailedEvent(
                    execution_id=handle.execution_id,
                    sequence=state.last_sequence + 1,
                    provider_session_id=handle.provider_session_id,
                    summary=state.scenario.error.message,
                    error=state.scenario.error,
                ),
            )
            state.status = "failed"
            self._terminal[handle.execution_id] = ReconcileResult(
                execution_id=handle.execution_id,
                status="failed",
                provider_session_id=handle.provider_session_id,
                last_sequence=event.sequence,
                error=state.scenario.error,
            )
            yield event
            return

        validate_agent_output(state.request.output_schema, state.scenario.output)
        event = await self._event(
            state,
            AgentCompletedEvent(
                execution_id=handle.execution_id,
                sequence=state.last_sequence + 1,
                provider_session_id=handle.provider_session_id,
                summary="Fake turn completed",
                output=state.scenario.output,
            ),
        )
        state.status = "succeeded"
        self._terminal[handle.execution_id] = ReconcileResult(
            execution_id=handle.execution_id,
            status="succeeded",
            provider_session_id=handle.provider_session_id,
            last_sequence=event.sequence,
            output=state.scenario.output,
        )
        yield event

    async def steer(self, handle: AgentTurnHandle, message: str) -> None:
        normalized = message.strip()
        if not normalized:
            raise AgentRuntimeError("steer message must not be blank", code="agent.steer_invalid")
        state = self._active_turn(handle)
        if state.status != "running" or state.cancel_event.is_set():
            raise AgentRuntimeError("turn is not running", code="agent.turn_not_running")
        self.steer_calls.append((handle.execution_id, normalized))

    async def cancel(self, handle: AgentTurnHandle) -> CancelResult:
        state = self._turns.get(handle.execution_id)
        if state is None or state.handle != handle:
            return CancelResult(execution_id=handle.execution_id, status="not_found")
        if state.status in {"succeeded", "failed", "cancelled"}:
            return CancelResult(execution_id=handle.execution_id, status="already_terminal")
        if state.cancel_event.is_set():
            return CancelResult(execution_id=handle.execution_id, status="requested")
        state.cancel_event.set()
        self.cancel_count += 1
        return CancelResult(execution_id=handle.execution_id, status="requested")

    async def reconcile(self, request: AgentReconcileRequest) -> ReconcileResult:
        execution_id = request.execution_id
        override = self._reconcile_overrides.get(execution_id)
        if override is not None:
            return override
        terminal = self._terminal.get(execution_id)
        if terminal is not None:
            return terminal
        turn = self._turns.get(execution_id)
        if turn is not None:
            if turn.status == "uncertain":
                return ReconcileResult(
                    execution_id=execution_id,
                    status="uncertain",
                    provider_session_id=turn.handle.provider_session_id,
                    last_sequence=turn.last_sequence,
                )
            return ReconcileResult(
                execution_id=execution_id,
                status="running",
                provider_session_id=turn.handle.provider_session_id,
                provider_turn_id=turn.handle.provider_turn_id,
                attachable=not turn.stream_started,
                last_sequence=turn.last_sequence,
            )
        prepared = self._prepared.get(execution_id)
        if prepared is not None:
            return ReconcileResult(
                execution_id=execution_id,
                status="prepared",
                provider_session_id=prepared.session.provider_session_id,
                attachable=False,
            )
        return ReconcileResult(execution_id=execution_id, status="not_found")

    async def attach(self, request: AgentReconcileRequest) -> AgentTurnHandle:
        turn = self._turns.get(request.execution_id)
        if turn is None or turn.status != "running" or turn.stream_started:
            raise AgentRuntimeError(
                "fake execution is not attachable",
                code="agent.execution_not_attachable",
                reconciliation_required=True,
            )
        if request.provider_session_id not in {
            None,
            turn.handle.provider_session_id,
        } or request.provider_turn_id not in {None, turn.handle.provider_turn_id}:
            raise AgentRuntimeError(
                "persisted provider identity does not match fake execution",
                code="agent.execution_identity_mismatch",
                reconciliation_required=True,
            )
        return turn.handle

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.close_count += 1
        for turn in self._turns.values():
            if turn.status == "running":
                turn.cancel_event.set()

    def _validate_selection(self, request: AgentExecutionRequest) -> None:
        if request.provider != self.name:
            raise AgentRuntimeError(
                "request provider does not match runtime",
                code="agent.provider_mismatch",
            )
        selected = next(
            (model for model in self._catalog.models if model.id == request.model), None
        )
        if selected is None:
            raise AgentRuntimeError(
                "model is not in runtime catalog", code="agent.model_unsupported"
            )
        if request.effort not in selected.efforts:
            raise AgentRuntimeError(
                "effort is not supported by selected model",
                code="agent.effort_unsupported",
            )

    async def _begin_stream(self, handle: AgentTurnHandle) -> _TurnState:
        state = self._active_turn(handle)
        if state.stream_started:
            raise AgentRuntimeError(
                "turn stream can only be consumed once",
                code="agent.stream_already_consumed",
            )
        state.stream_started = True
        return state

    def _active_turn(self, handle: AgentTurnHandle) -> _TurnState:
        state = self._turns.get(handle.execution_id)
        if state is None or state.handle != handle:
            raise AgentRuntimeError("unknown agent turn handle", code="agent.turn_not_found")
        return state

    async def _event(self, state: _TurnState, event: AgentEvent) -> AgentEvent:
        state.last_sequence = event.sequence
        return event

    async def _wait_for_cancel(self, state: _TurnState) -> bool:
        if state.cancel_event.is_set():
            return True
        if state.scenario.delay_seconds == 0:
            return False
        try:
            await asyncio.wait_for(
                state.cancel_event.wait(),
                timeout=state.scenario.delay_seconds,
            )
        except TimeoutError:
            return False
        return True

    async def _cancelled_event(self, state: _TurnState) -> AgentCancelledEvent:
        event = AgentCancelledEvent(
            execution_id=state.handle.execution_id,
            sequence=state.last_sequence + 1,
            provider_session_id=state.handle.provider_session_id,
            summary="Fake turn cancelled",
            reason="cancel requested",
        )
        state.last_sequence = event.sequence
        state.status = "cancelled"
        self._terminal[state.handle.execution_id] = ReconcileResult(
            execution_id=state.handle.execution_id,
            status="cancelled",
            provider_session_id=state.handle.provider_session_id,
            last_sequence=event.sequence,
        )
        return event

    def _ensure_open(self) -> None:
        if self._closed:
            raise AgentRuntimeError("fake runtime is closed", code="agent.runtime_closed")

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from misaka_agent_capability import agent_descriptor
from misaka_invocation_contracts import (
    CapabilityDescriptor,
    CapabilityFeature,
    InvocationEvent,
    InvocationRequest,
    InvocationResult,
    InvocationStatus,
    ReconcileResult,
    ReconcileStatus,
)
from misaka_invocation_runtime import ProviderExecutionError, ProviderHandle
from misaka_kernel_contracts import JsonObject, JsonValue


@dataclass(frozen=True, slots=True)
class FakeFailure:
    code: str
    message: str
    reconciliation_required: bool = False


@dataclass(frozen=True, slots=True)
class FakeAgentScenario:
    output: JsonValue = field(default_factory=lambda: {"answer": "ok"})
    events: tuple[JsonObject, ...] = ()
    delay_seconds: float = 0.0
    failure: FakeFailure | None = None

    def __post_init__(self) -> None:
        if self.delay_seconds < 0:
            raise ValueError("delay_seconds must not be negative")


class FakeAgentProvider:
    def __init__(self, scenario: FakeAgentScenario | None = None) -> None:
        self.scenario = scenario or FakeAgentScenario()
        self.starts = 0
        self.last_handle: _FakeAgentHandle | None = None
        self.started = asyncio.Event()

    async def describe(self) -> CapabilityDescriptor:
        return agent_descriptor(
            features=frozenset(
                {
                    CapabilityFeature.STRUCTURED_OUTPUT,
                    CapabilityFeature.STREAMING,
                    CapabilityFeature.CANCELLATION,
                }
            )
        )

    async def start(self, request: InvocationRequest) -> ProviderHandle:
        self.starts += 1
        if self.scenario.failure is not None:
            failure = self.scenario.failure
            raise ProviderExecutionError(
                failure.code,
                failure.message,
                reconciliation_required=failure.reconciliation_required,
            )
        self.last_handle = _FakeAgentHandle(request, self.scenario)
        self.started.set()
        return self.last_handle


class _FakeAgentHandle:
    def __init__(self, request: InvocationRequest, scenario: FakeAgentScenario) -> None:
        self._request = request
        self._scenario = scenario
        self._cancelled = asyncio.Event()
        self._terminal = False
        self._last_reconcile = ReconcileStatus.RUNNING

    async def events(self) -> AsyncIterator[InvocationEvent]:
        for sequence, payload in enumerate(self._scenario.events, start=1):
            if self._cancelled.is_set():
                return
            if self._scenario.delay_seconds:
                await asyncio.sleep(self._scenario.delay_seconds)
            yield InvocationEvent(
                invocation_id=self._request.invocation_id,
                sequence=sequence,
                status=InvocationStatus.RUNNING,
                payload=payload,
            )

    async def wait(self) -> InvocationResult:
        if self._scenario.delay_seconds:
            await asyncio.sleep(self._scenario.delay_seconds)
        if self._cancelled.is_set():
            self._terminal = True
            self._last_reconcile = ReconcileStatus.CANCELLED
            return InvocationResult(
                invocation_id=self._request.invocation_id,
                status=InvocationStatus.CANCELLED,
                error_code="agent.cancelled",
                error_message="fake agent invocation was cancelled",
            )
        if self._request.output_schema is not None and not _matches_schema(
            self._scenario.output,
            self._request.output_schema,
        ):
            self._terminal = True
            self._last_reconcile = ReconcileStatus.FAILED
            return InvocationResult(
                invocation_id=self._request.invocation_id,
                status=InvocationStatus.FAILED,
                error_code="agent.output_contract_violated",
                error_message="fake agent output does not satisfy output_schema",
            )
        self._terminal = True
        self._last_reconcile = ReconcileStatus.SUCCEEDED
        return InvocationResult(
            invocation_id=self._request.invocation_id,
            status=InvocationStatus.SUCCEEDED,
            output=self._scenario.output,
        )

    async def cancel(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("cancellation reason must not be empty")
        self._cancelled.set()

    async def reconcile(self) -> ReconcileResult:
        return ReconcileResult(self._last_reconcile)


def _matches_schema(value: JsonValue, schema: JsonObject) -> bool:
    schema_type = schema.get("type")
    if schema_type is not None and (
        not isinstance(schema_type, str) or not _matches_type(value, schema_type)
    ):
        return False
    if not isinstance(value, dict):
        return True
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        return False
    if any(item not in value for item in required):
        return False
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return False
    if schema.get("additionalProperties", True) is False and set(value) - set(properties):
        return False
    for key, property_value in value.items():
        property_schema = properties.get(key)
        if property_schema is None:
            continue
        if not isinstance(property_schema, dict) or not _matches_schema(
            property_value,
            property_schema,
        ):
            return False
    return True


def _matches_type(value: JsonValue, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return False

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from misaka_capability_catalog import matches_json_schema
from misaka_invocation_contracts import (
    CapabilityDescriptor,
    CapabilityOperation,
    CompletionBoundary,
    InvocationEvent,
    InvocationRequest,
    InvocationResult,
    InvocationStatus,
    ReconcileResult,
    ReconcileStatus,
)
from misaka_invocation_runtime import InvocationRuntime, ProviderHandle
from misaka_kernel_contracts import JsonObject, JsonValue

INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "required": ["prompt"],
    "properties": {"prompt": {"type": "string"}},
    "additionalProperties": False,
}
OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
    "additionalProperties": False,
}


class _Handle:
    def __init__(self, request: InvocationRequest, output: JsonValue) -> None:
        self.request = request
        self.output = output
        self.closed = False

    async def events(self) -> AsyncIterator[InvocationEvent]:
        if False:
            yield InvocationEvent(
                invocation_id=self.request.invocation_id,
                sequence=1,
                status=InvocationStatus.RUNNING,
            )

    async def wait(self) -> InvocationResult:
        return InvocationResult(
            invocation_id=self.request.invocation_id,
            status=InvocationStatus.SUCCEEDED,
            output=self.output,
        )

    async def cancel(self, reason: str) -> None:
        del reason

    async def reconcile(self) -> ReconcileResult:
        return ReconcileResult(ReconcileStatus.SUCCEEDED)

    async def close(self) -> None:
        self.closed = True


class _SchemaProvider:
    def __init__(self, output: JsonValue) -> None:
        self.output = output
        self.starts = 0

    async def describe(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            capability_id="test.schema",
            version="1.0.0",
            operations=(
                CapabilityOperation(
                    name="run",
                    input_schema=INPUT_SCHEMA,
                    output_schema=OUTPUT_SCHEMA,
                ),
            ),
        )

    async def start(self, request: InvocationRequest) -> ProviderHandle:
        self.starts += 1
        return _Handle(request, self.output)


def _request(invocation_id: str, input_value: JsonObject) -> InvocationRequest:
    return InvocationRequest(
        invocation_id=invocation_id,
        capability_id="test.schema",
        operation="run",
        input=input_value,
        idempotency_key=f"key-{invocation_id}",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
    )


def test_capability_catalog_schema_validator_handles_nested_values() -> None:
    assert matches_json_schema(
        {"items": [{"name": "one"}]},
        {
            "type": "object",
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name"],
                    },
                }
            },
        },
    )
    assert not matches_json_schema({"items": [{"name": 1}]}, {"type": "array"})


@pytest.mark.asyncio
async def test_runtime_rejects_input_before_provider_start() -> None:
    provider = _SchemaProvider({"answer": "ok"})
    runtime = InvocationRuntime()
    await runtime.register_provider("schema", provider)

    result = await (
        await runtime.submit(
            _request("invalid-input", {"prompt": 1}),
            provider_id="schema",
        )
    ).wait()

    assert result.status is InvocationStatus.REJECTED
    assert result.error_code == "capability.input_schema_invalid"
    assert provider.starts == 0


@pytest.mark.asyncio
async def test_runtime_rejects_invalid_success_output_as_known_failure() -> None:
    provider = _SchemaProvider({"unexpected": True})
    runtime = InvocationRuntime()
    await runtime.register_provider("schema", provider)

    result = await (
        await runtime.submit(
            _request("invalid-output", {"prompt": "hello"}),
            provider_id="schema",
        )
    ).wait()

    assert result.status is InvocationStatus.FAILED
    assert result.error_code == "provider.output_schema_invalid"
    assert result.error_message is not None
    assert provider.starts == 1

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from multi_agent_v2.packages.agent_runtime.errors import AgentRuntimeError, AgentStreamContractError
from multi_agent_v2.packages.agent_runtime.models import (
    TERMINAL_EVENT_KINDS,
    AgentEvent,
)
from multi_agent_v2.packages.domain.json_types import JsonObject
from multi_agent_v2.packages.workflow_dsl.ir import StrictSchemaIr


async def validate_agent_stream(
    events: AsyncIterator[AgentEvent],
    *,
    execution_id: str,
    provider_session_id: str,
    start_sequence: int = 0,
) -> AsyncIterator[AgentEvent]:
    """Validate identity, sequence, and terminal semantics while forwarding events."""

    if start_sequence < 0:
        raise AgentStreamContractError("start sequence must be non-negative")
    last_sequence = start_sequence
    terminal_seen = False
    async for event in events:
        if terminal_seen:
            raise AgentStreamContractError("agent emitted an event after a terminal event")
        if event.execution_id != execution_id:
            raise AgentStreamContractError("agent event execution identity does not match")
        if event.provider_session_id != provider_session_id:
            raise AgentStreamContractError("agent event provider session does not match")
        expected_sequence = last_sequence + 1
        if event.sequence != expected_sequence:
            raise AgentStreamContractError(
                f"agent event sequence must be {expected_sequence}, got {event.sequence}"
            )
        last_sequence = event.sequence
        terminal_seen = event.kind in TERMINAL_EVENT_KINDS
        yield event

    if not terminal_seen:
        raise AgentStreamContractError("agent stream ended without a terminal event")


def validate_agent_output(schema: StrictSchemaIr, output: JsonObject) -> None:
    try:
        raw_schema = cast(JsonObject, json.loads(schema.canonical))
        Draft202012Validator(raw_schema).validate(  # pyright: ignore[reportUnknownMemberType]
            output
        )
    except (json.JSONDecodeError, ValidationError) as exc:
        raise AgentRuntimeError(
            "agent output does not satisfy the compiled schema",
            code="agent.output_contract_violated",
        ) from exc

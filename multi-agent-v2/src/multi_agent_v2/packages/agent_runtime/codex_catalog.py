from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from multi_agent_v2.packages.agent_runtime.codex_locator import CodexRuntimeDescriptor
from multi_agent_v2.packages.agent_runtime.codex_native import to_plain_data
from multi_agent_v2.packages.agent_runtime.models import (
    AgentModelCatalog,
    AgentModelSpec,
)
from multi_agent_v2.packages.domain.json_types import JsonValue


def catalog_from_app_server(
    runtime: CodexRuntimeDescriptor,
    response: Any,  # pyright: ignore[reportExplicitAny]
) -> AgentModelCatalog:
    raw_response = to_plain_data(response)
    if not isinstance(raw_response, dict):
        raise ValueError("Codex model/list response must be an object")
    raw_models = raw_response.get("data")
    if not isinstance(raw_models, list):
        raise ValueError("Codex model/list response must contain a data list")
    next_cursor = raw_response.get("nextCursor", raw_response.get("next_cursor"))
    if next_cursor:
        raise ValueError("Codex model/list response has an unhandled nextCursor")

    models: list[AgentModelSpec] = []
    for raw_model in raw_models:
        if not isinstance(raw_model, dict) or raw_model.get("hidden") is True:
            continue
        model_id = _text(raw_model, "id", "model")
        if model_id is None:
            continue
        efforts = _efforts(raw_model)
        if not efforts:
            continue
        recommended = _text(
            raw_model,
            "defaultReasoningEffort",
            "default_reasoning_effort",
        )
        models.append(
            AgentModelSpec(
                id=model_id,
                label=_text(raw_model, "displayName", "display_name") or model_id,
                model_type=model_id.partition("/")[0] if "/" in model_id else runtime.provider_id,
                efforts=efforts,
                recommended_effort=recommended,
            )
        )
    payload = {
        "runtime": runtime.runtime_id,
        "provider": runtime.provider_id,
        "models": [model.model_dump(mode="json") for model in models],
    }
    revision = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return AgentModelCatalog(
        runtime_name="codex",
        runtime_id=runtime.runtime_id,
        provider_id=runtime.provider_id,
        revision=revision,
        models=tuple(models),
    )


def _text(raw: Mapping[str, JsonValue], *keys: str) -> str | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _efforts(raw: Mapping[str, JsonValue]) -> tuple[str, ...]:
    raw_levels = raw.get(
        "supportedReasoningEfforts",
        raw.get("supported_reasoning_efforts", ()),
    )
    if not isinstance(raw_levels, list):
        raise ValueError("supportedReasoningEfforts must be a list")
    efforts: list[str] = []
    for raw_level in raw_levels:
        if isinstance(raw_level, str):
            effort = raw_level.strip()
        elif isinstance(raw_level, dict):
            effort = _text(raw_level, "reasoningEffort", "reasoning_effort") or ""
        else:
            effort = ""
        if effort and effort not in efforts:
            efforts.append(effort)
    recommended = _text(raw, "defaultReasoningEffort", "default_reasoning_effort")
    if recommended and recommended not in efforts:
        efforts.append(recommended)
    return tuple(efforts)

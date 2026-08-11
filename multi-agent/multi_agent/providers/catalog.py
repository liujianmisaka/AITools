from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from multi_agent.providers.runtime import CodexRuntimeDescriptor
from multi_agent.providers.utils import to_plain_data


class ProviderModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    label: str
    model_type: str
    efforts: tuple[str, ...]
    default_effort: str | None = None

    @field_validator("id", "label", "model_type")
    @classmethod
    def non_empty_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @field_validator("efforts")
    @classmethod
    def valid_efforts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip() for value in values)
        if not normalized or any(not value for value in normalized):
            raise ValueError("at least one non-empty effort is required")
        if len(set(normalized)) != len(normalized):
            raise ValueError("effort values must be unique")
        return normalized

    @model_validator(mode="after")
    def default_effort_is_supported(self) -> "ProviderModelSpec":
        if self.default_effort is not None and self.default_effort not in self.efforts:
            raise ValueError("default effort must be included in efforts")
        return self


@dataclass(frozen=True, slots=True)
class CodexModelCatalog:
    provider_id: str
    config_path: Path
    catalog_path: Path | None
    models: tuple[ProviderModelSpec, ...]
    runtime_id: str = "fixed"
    environment_kind: str = "fixed"
    config_source: str = "fixed"
    revision: str = "fixed"
    runtime: CodexRuntimeDescriptor | None = None


def _text(raw: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _model_efforts(raw_model: dict[str, Any]) -> tuple[str, ...]:
    raw_levels = raw_model.get(
        "supportedReasoningEfforts",
        raw_model.get("supported_reasoning_efforts", ()),
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

    default_effort = _text(
        raw_model,
        "defaultReasoningEffort",
        "default_reasoning_effort",
    )
    if default_effort and default_effort not in efforts:
        efforts.append(default_effort)
    return tuple(efforts)


def _model_type(model_id: str, provider_id: str) -> str:
    namespace, separator, _name = model_id.partition("/")
    return namespace if separator else provider_id


def _catalog_revision(
    runtime: CodexRuntimeDescriptor,
    models: tuple[ProviderModelSpec, ...],
) -> str:
    payload = {
        "runtime": runtime.runtime_id,
        "provider": runtime.provider_id,
        "models": [model.model_dump(mode="json") for model in models],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def catalog_from_app_server(
    runtime: CodexRuntimeDescriptor,
    response: Any,
) -> CodexModelCatalog:
    raw_response = to_plain_data(response)
    raw_models = raw_response.get("data") if isinstance(raw_response, dict) else None
    if not isinstance(raw_models, list):
        raise ValueError("Codex model/list response must contain a data list")
    next_cursor = (
        raw_response.get("nextCursor", raw_response.get("next_cursor"))
        if isinstance(raw_response, dict)
        else None
    )
    if next_cursor:
        raise ValueError(
            "Codex model/list response is paginated but the installed SDK "
            "does not expose cursor pagination"
        )

    models: list[ProviderModelSpec] = []
    for raw_model in raw_models:
        if not isinstance(raw_model, dict):
            continue
        if raw_model.get("hidden") is True:
            continue
        model_id = _text(raw_model, "id", "model")
        if model_id is None:
            continue
        efforts = _model_efforts(raw_model)
        if not efforts:
            continue
        label = _text(raw_model, "displayName", "display_name") or model_id
        default_effort = _text(
            raw_model,
            "defaultReasoningEffort",
            "default_reasoning_effort",
        )
        models.append(
            ProviderModelSpec(
                id=model_id,
                label=label,
                model_type=_model_type(model_id, runtime.provider_id),
                efforts=efforts,
                default_effort=default_effort,
            )
        )

    model_ids = [model.id for model in models]
    if len(set(model_ids)) != len(model_ids):
        raise ValueError("Codex model/list response contains duplicate model ids")
    if not models:
        raise ValueError("Codex model/list response contains no selectable models")

    normalized_models = tuple(models)
    return CodexModelCatalog(
        provider_id=runtime.provider_id,
        config_path=runtime.config_path,
        catalog_path=runtime.catalog_path,
        models=normalized_models,
        runtime_id=runtime.runtime_id,
        environment_kind=runtime.environment_kind.value,
        config_source=runtime.config_source,
        revision=_catalog_revision(runtime, normalized_models),
        runtime=runtime,
    )

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


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
    catalog_path: Path
    models: tuple[ProviderModelSpec, ...]


def _model_efforts(raw_model: dict[str, Any]) -> tuple[str, ...]:
    raw_levels = raw_model.get("supported_reasoning_levels", ())
    if not isinstance(raw_levels, list):
        raise ValueError("supported_reasoning_levels must be a list")

    efforts: list[str] = []
    for raw_level in raw_levels:
        if isinstance(raw_level, str):
            effort = raw_level.strip()
        elif isinstance(raw_level, dict):
            value = raw_level.get("effort")
            effort = value.strip() if isinstance(value, str) else ""
        else:
            effort = ""
        if effort and effort not in efforts:
            efforts.append(effort)

    default_effort = raw_model.get("default_reasoning_level")
    if isinstance(default_effort, str):
        default_effort = default_effort.strip()
        if default_effort and default_effort not in efforts:
            efforts.append(default_effort)
    return tuple(efforts)


def _model_type(slug: str, provider_id: str) -> str:
    namespace, separator, _name = slug.partition("/")
    return namespace if separator else provider_id


def load_codex_model_catalog(codex_home: Path) -> CodexModelCatalog:
    resolved_home = codex_home.expanduser().resolve()
    config_path = resolved_home / "config.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))

    provider_value = config.get("model_provider", "openai")
    if not isinstance(provider_value, str) or not provider_value.strip():
        raise ValueError("Codex model_provider must be a non-empty string")
    provider_id = provider_value.strip()

    catalog_value = config.get("model_catalog_json")
    if not isinstance(catalog_value, str) or not catalog_value.strip():
        raise ValueError(
            f"Codex config does not define model_catalog_json: {config_path}"
        )
    expanded_catalog = Path(
        os.path.expandvars(os.path.expanduser(catalog_value.strip()))
    )
    catalog_path = (
        expanded_catalog
        if expanded_catalog.is_absolute()
        else resolved_home / expanded_catalog
    ).resolve()

    raw_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    raw_models = raw_catalog.get("models") if isinstance(raw_catalog, dict) else None
    if not isinstance(raw_models, list):
        raise ValueError(f"Codex model catalog must contain a models list: {catalog_path}")

    models: list[ProviderModelSpec] = []
    for raw_model in raw_models:
        if not isinstance(raw_model, dict):
            continue
        if raw_model.get("visibility", "list") != "list":
            continue
        if raw_model.get("supported_in_api", True) is False:
            continue
        slug_value = raw_model.get("slug")
        if not isinstance(slug_value, str) or not slug_value.strip():
            continue
        slug = slug_value.strip()
        label_value = raw_model.get("display_name", slug)
        label = label_value.strip() if isinstance(label_value, str) else slug
        default_value = raw_model.get("default_reasoning_level")
        default_effort = (
            default_value.strip()
            if isinstance(default_value, str) and default_value.strip()
            else None
        )
        models.append(
            ProviderModelSpec(
                id=slug,
                label=label,
                model_type=_model_type(slug, provider_id),
                efforts=_model_efforts(raw_model),
                default_effort=default_effort,
            )
        )

    model_ids = [model.id for model in models]
    if len(set(model_ids)) != len(model_ids):
        raise ValueError(f"Codex model catalog contains duplicate slugs: {catalog_path}")
    if not models:
        raise ValueError(f"Codex model catalog contains no selectable models: {catalog_path}")

    return CodexModelCatalog(
        provider_id=provider_id,
        config_path=config_path,
        catalog_path=catalog_path,
        models=tuple(models),
    )

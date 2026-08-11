from __future__ import annotations

import hashlib
import json
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class CodexEnvironmentKind(str, Enum):
    openai_native = "openai_native"
    ccswitch = "ccswitch"
    opencodex = "opencodex"
    custom_codex = "custom_codex"


@dataclass(frozen=True, slots=True)
class CodexRuntimeDescriptor:
    runtime_id: str
    codex_bin: str | None
    codex_home: Path
    config_path: Path
    config_source: str
    provider_id: str
    environment_kind: CodexEnvironmentKind
    catalog_path: Path | None
    signature: tuple[object, ...]


def _expanded_path(value: str, user_home: Path) -> Path:
    expanded = os.path.expandvars(value.strip())
    if expanded == "~":
        return user_home
    if expanded.startswith(("~/", "~\\")):
        return user_home / expanded[2:]
    return Path(expanded)


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_toml_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = tomllib.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _file_signature(path: Path | None) -> tuple[int, int] | None:
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _catalog_path(
    config: Mapping[str, Any],
    codex_home: Path,
    user_home: Path,
) -> Path | None:
    value = config.get("model_catalog_json")
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = _expanded_path(value, user_home)
    if not candidate.is_absolute():
        candidate = codex_home / candidate
    return candidate.resolve()


def _provider_id(config: Mapping[str, Any]) -> str:
    value = config.get("model_provider", "openai")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Codex model_provider must be a non-empty string")
    provider_id = value.strip()
    if provider_id not in {"openai", "ollama", "lmstudio", "amazon-bedrock"}:
        providers = config.get("model_providers")
        if not isinstance(providers, dict) or not isinstance(
            providers.get(provider_id), dict
        ):
            raise ValueError(
                f"Codex model_provider {provider_id!r} has no matching "
                f"[model_providers.{provider_id}] definition"
            )
    return provider_id


def _environment_kind(
    config: Mapping[str, Any],
    provider_id: str,
    catalog_path: Path | None,
    config_source: str,
) -> CodexEnvironmentKind:
    catalog_name = catalog_path.name.lower() if catalog_path is not None else ""
    if "cc-switch" in catalog_name or config_source == "ccswitch_settings":
        return CodexEnvironmentKind.ccswitch
    if "opencodex" in catalog_name:
        return CodexEnvironmentKind.opencodex
    if (
        provider_id == "openai"
        and catalog_path is None
        and not config.get("openai_base_url")
    ):
        return CodexEnvironmentKind.openai_native
    return CodexEnvironmentKind.custom_codex


def _ccswitch_codex_home(user_home: Path) -> Path | None:
    settings = _read_json_object(user_home / ".cc-switch" / "settings.json")
    value = settings.get("codexConfigDir")
    if not isinstance(value, str) or not value.strip():
        return None
    return _expanded_path(value, user_home).resolve()


class CodexRuntimeLocator:
    def __init__(
        self,
        *,
        codex_bin: str | None = None,
        codex_home: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
        user_home: Path | None = None,
    ) -> None:
        self._codex_bin = codex_bin.strip() if codex_bin else None
        self._codex_home = codex_home
        self._environ = environ if environ is not None else os.environ
        self._user_home = (user_home or Path.home()).expanduser().resolve()

    def _home(self) -> tuple[Path, str]:
        if self._codex_home is not None:
            value = str(self._codex_home)
            return _expanded_path(value, self._user_home).resolve(), "explicit"

        environment_home = self._environ.get("CODEX_HOME")
        if environment_home and environment_home.strip():
            return (
                _expanded_path(environment_home, self._user_home).resolve(),
                "environment",
            )

        ccswitch_home = _ccswitch_codex_home(self._user_home)
        if ccswitch_home is not None:
            return ccswitch_home, "ccswitch_settings"

        return (self._user_home / ".codex").resolve(), "default"

    def resolve(self) -> CodexRuntimeDescriptor:
        codex_home, config_source = self._home()
        if not codex_home.is_dir():
            raise ValueError(f"configured Codex home does not exist: {codex_home}")

        config_path = codex_home / "config.toml"
        config = _read_toml_object(config_path)
        provider_id = _provider_id(config)
        catalog_path = _catalog_path(config, codex_home, self._user_home)
        environment_kind = _environment_kind(
            config,
            provider_id,
            catalog_path,
            config_source,
        )
        identity = f"{self._codex_bin or 'sdk'}\0{codex_home}".encode("utf-8")
        identity_hash = hashlib.sha256(identity).hexdigest()[:12]
        runtime_id = (
            f"codex:{environment_kind.value}:{provider_id}:{identity_hash}"
        )
        signature: tuple[object, ...] = (
            self._codex_bin,
            str(codex_home),
            config_source,
            provider_id,
            environment_kind.value,
            str(catalog_path) if catalog_path is not None else None,
            _file_signature(config_path),
            _file_signature(catalog_path),
        )
        return CodexRuntimeDescriptor(
            runtime_id=runtime_id,
            codex_bin=self._codex_bin,
            codex_home=codex_home,
            config_path=config_path,
            config_source=config_source,
            provider_id=provider_id,
            environment_kind=environment_kind,
            catalog_path=catalog_path,
            signature=signature,
        )

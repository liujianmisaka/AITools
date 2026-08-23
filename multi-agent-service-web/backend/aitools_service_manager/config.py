from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

ProviderKind = Literal["fake", "codex"]
_CONFIGURATION_VERSION = 2
_LEGACY_CONFIGURATION_VERSION = 1
_SENSITIVE_OVERRIDE_TOKENS = {
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
}
_CONTROL_PLANE_STATE_FILES = (
    "control-plane.jsonl",
    "control-plane-codex.jsonl",
    "control-plane-fake.jsonl",
)


@dataclass(frozen=True, slots=True)
class ProviderConfiguration:
    """One provider instance registered in the shared Control Plane runtime."""

    provider_id: str = "fake"
    kind: ProviderKind = "fake"
    codex_home: Path | None = None
    config_overrides: tuple[str, ...] = ()
    network_deny_enforced: bool = False

    def __post_init__(self) -> None:
        if not self.provider_id.strip():
            raise ValueError("provider id must not be empty")
        if self.kind not in {"fake", "codex"}:
            raise ValueError("provider kind must be fake or codex")

        codex_home = self.codex_home
        if codex_home is not None:
            codex_home = _existing_directory(codex_home, "codex home")
            object.__setattr__(self, "codex_home", codex_home)

        overrides = tuple(_validated_config_override(value) for value in self.config_overrides)
        object.__setattr__(self, "config_overrides", overrides)

        if self.kind == "codex":
            if codex_home is None:
                raise ValueError("codex home is required for a codex provider")
            return
        if codex_home is not None or overrides or self.network_deny_enforced:
            raise ValueError(
                "fake providers cannot define codex home, config overrides, "
                "or network deny enforcement"
            )

    def to_payload(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "kind": self.kind,
            "codex_home": str(self.codex_home) if self.codex_home is not None else None,
            "config_overrides": list(self.config_overrides),
            "network_deny_enforced": self.network_deny_enforced,
        }

    def to_profile_payload(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "kind": self.kind,
            "codex_home": self.codex_home,
            "config_overrides": self.config_overrides,
            "network_deny_enforced": self.network_deny_enforced,
        }

    @classmethod
    def from_payload(cls, value: object) -> ProviderConfiguration:
        if not isinstance(value, Mapping):
            raise ValueError("provider configuration must be a JSON object")
        payload = cast(Mapping[object, object], value)
        expected_fields = {
            "provider_id",
            "kind",
            "codex_home",
            "config_overrides",
            "network_deny_enforced",
        }
        if set(payload) != expected_fields:
            raise ValueError("provider configuration fields do not match version 2")

        provider_id = payload["provider_id"]
        kind = payload["kind"]
        codex_home = payload["codex_home"]
        config_overrides = payload["config_overrides"]
        network_deny_enforced = payload["network_deny_enforced"]
        if not isinstance(provider_id, str):
            raise ValueError("provider id must be a string")
        if kind not in {"fake", "codex"}:
            raise ValueError("provider kind must be fake or codex")
        if codex_home is not None and not isinstance(codex_home, str):
            raise ValueError("codex home must be a string or null")
        if not isinstance(config_overrides, list) or not all(
            isinstance(item, str) for item in cast(list[object], config_overrides)
        ):
            raise ValueError("config overrides must be an array of strings")
        if not isinstance(network_deny_enforced, bool):
            raise ValueError("network deny enforcement must be a boolean")
        return cls(
            provider_id=provider_id,
            kind=cast(ProviderKind, kind),
            codex_home=Path(codex_home) if codex_home is not None else None,
            config_overrides=tuple(
                cast(str, item) for item in cast(list[object], config_overrides)
            ),
            network_deny_enforced=network_deny_enforced,
        )


@dataclass(frozen=True, slots=True)
class RuntimeConfiguration:
    """Persisted settings used the next time Control Plane starts."""

    providers: tuple[ProviderConfiguration, ...] = (ProviderConfiguration(),)
    allowed_path_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        if not self.providers:
            raise ValueError("at least one provider is required")
        provider_ids = [provider.provider_id for provider in self.providers]
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("provider ids must be unique")

        roots = tuple(
            _existing_directory(path, "allowed path root") for path in self.allowed_path_roots
        )
        if len(roots) != len(set(roots)):
            raise ValueError("allowed path roots must be unique")
        object.__setattr__(self, "allowed_path_roots", roots)

    def to_payload(self) -> dict[str, object]:
        return {
            "version": _CONFIGURATION_VERSION,
            "providers": [provider.to_payload() for provider in self.providers],
            "allowed_path_roots": [str(path) for path in self.allowed_path_roots],
        }

    @classmethod
    def from_payload(cls, value: object) -> RuntimeConfiguration:
        if not isinstance(value, Mapping):
            raise ValueError("runtime configuration must be a JSON object")
        payload = cast(Mapping[object, object], value)
        version = payload.get("version")
        if version == _LEGACY_CONFIGURATION_VERSION:
            return _migrate_legacy_configuration(payload)
        if version != _CONFIGURATION_VERSION:
            raise ValueError("runtime configuration version is not supported")

        expected_fields = {"version", "providers", "allowed_path_roots"}
        if set(payload) != expected_fields:
            raise ValueError("runtime configuration fields do not match version 2")
        providers = payload["providers"]
        allowed_path_roots = payload["allowed_path_roots"]
        if not isinstance(providers, list):
            raise ValueError("providers must be an array")
        if not isinstance(allowed_path_roots, list) or not all(
            isinstance(path, str) for path in cast(list[object], allowed_path_roots)
        ):
            raise ValueError("allowed path roots must be an array of strings")
        return cls(
            providers=tuple(
                ProviderConfiguration.from_payload(item) for item in cast(list[object], providers)
            ),
            allowed_path_roots=tuple(
                Path(cast(str, path)) for path in cast(list[object], allowed_path_roots)
            ),
        )


class RuntimeConfigurationStore:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def load_or_create(self, default: RuntimeConfiguration) -> RuntimeConfiguration:
        if not self.path.exists():
            self.save(default)
            return default
        payload = self._read_payload()
        configuration = RuntimeConfiguration.from_payload(payload)
        if _payload_version(payload) == _LEGACY_CONFIGURATION_VERSION:
            self.save(configuration)
        return configuration

    def load(self) -> RuntimeConfiguration:
        return RuntimeConfiguration.from_payload(self._read_payload())

    def save(self, configuration: RuntimeConfiguration) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                stream.write(
                    json.dumps(
                        configuration.to_payload(),
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n"
                )
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.replace(self.path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _read_payload(self) -> object:
        try:
            return cast(object, json.loads(self.path.read_text(encoding="utf-8")))
        except FileNotFoundError as exc:
            raise ValueError(f"runtime configuration does not exist: {self.path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"runtime configuration cannot be read: {self.path}") from exc


@dataclass(frozen=True, slots=True)
class ManagementConfig:
    root: Path
    management_host: str = "127.0.0.1"
    management_port: int = 8014
    service_web_port: int = 5174
    control_plane_port: int = 8016
    main_web_port: int = 5173
    configuration_path: Path | None = None

    def __post_init__(self) -> None:
        root = _existing_directory(self.root, "AITools root")
        object.__setattr__(self, "root", root)
        if not self.management_host.strip():
            raise ValueError("management host must not be empty")
        ports = {
            "management": self.management_port,
            "service web": self.service_web_port,
            "control plane": self.control_plane_port,
            "main web": self.main_web_port,
        }
        for name, port in ports.items():
            if not 1 <= port <= 65535:
                raise ValueError(f"{name} port must be between 1 and 65535")
        if len(set(ports.values())) != len(ports):
            raise ValueError(
                "management, service web, control plane, and main web ports must differ"
            )

        configuration_path = self.configuration_path or (
            root / ".data" / "aitools-service-manager" / "configuration.json"
        )
        object.__setattr__(
            self,
            "configuration_path",
            configuration_path.expanduser().resolve(),
        )

    @property
    def initial_runtime_configuration(self) -> RuntimeConfiguration:
        return RuntimeConfiguration()

    @property
    def management_url(self) -> str:
        return f"http://{self.management_host}:{self.management_port}"

    @property
    def service_web_url(self) -> str:
        return f"http://127.0.0.1:{self.service_web_port}"

    @property
    def control_plane_url(self) -> str:
        return f"http://127.0.0.1:{self.control_plane_port}"

    @property
    def main_web_url(self) -> str:
        return f"http://127.0.0.1:{self.main_web_port}"

    def control_plane_state_path(self) -> Path:
        return resolve_control_plane_state_path(self.root)


def resolve_control_plane_state_path(root: Path) -> Path:
    state_directory = (root / ".data" / "multi-agent-v3").resolve()
    candidates = tuple(state_directory / name for name in _CONTROL_PLANE_STATE_FILES)
    existing = tuple(path for path in candidates if path.exists())
    if len(existing) > 1:
        names = ", ".join(path.name for path in existing)
        raise ValueError(
            f"multiple Control Plane state files exist ({names}); consolidate them before startup"
        )
    return existing[0] if existing else candidates[0]


def _migrate_legacy_configuration(
    payload: Mapping[object, object],
) -> RuntimeConfiguration:
    expected_fields = {
        "version",
        "profile",
        "codex_home",
        "provider_id",
        "network_deny_enforced",
        "allowed_path_roots",
    }
    if set(payload) != expected_fields:
        raise ValueError("runtime configuration fields do not match version 1")

    profile = payload["profile"]
    codex_home = payload["codex_home"]
    provider_id = payload["provider_id"]
    network_deny_enforced = payload["network_deny_enforced"]
    allowed_path_roots = payload["allowed_path_roots"]
    if profile not in {"fake", "codex"}:
        raise ValueError("profile must be fake or codex")
    if codex_home is not None and not isinstance(codex_home, str):
        raise ValueError("codex home must be a string or null")
    if not isinstance(provider_id, str):
        raise ValueError("provider id must be a string")
    if not isinstance(network_deny_enforced, bool):
        raise ValueError("network deny enforcement must be a boolean")
    if not isinstance(allowed_path_roots, list) or not all(
        isinstance(path, str) for path in cast(list[object], allowed_path_roots)
    ):
        raise ValueError("allowed path roots must be an array of strings")

    provider = (
        ProviderConfiguration()
        if profile == "fake"
        else ProviderConfiguration(
            provider_id=provider_id,
            kind="codex",
            codex_home=Path(codex_home) if codex_home is not None else None,
            network_deny_enforced=network_deny_enforced,
        )
    )
    return RuntimeConfiguration(
        providers=(provider,),
        allowed_path_roots=tuple(
            Path(cast(str, path)) for path in cast(list[object], allowed_path_roots)
        ),
    )


def _payload_version(value: object) -> object:
    if not isinstance(value, Mapping):
        return None
    return cast(Mapping[object, object], value).get("version")


def _validated_config_override(value: str) -> str:
    if not value.strip():
        raise ValueError("config overrides must not contain empty values")
    key = value.partition("=")[0].strip().lower()
    tokens = set(re.split(r"[^a-z0-9]+", key))
    secret_key = "apikey" in tokens or (
        "key" in tokens and bool(tokens.intersection({"access", "api", "private"}))
    )
    if secret_key or tokens.intersection(_SENSITIVE_OVERRIDE_TOKENS):
        raise ValueError(
            "config override keys cannot store secrets; reference an environment variable instead"
        )
    return value


def _existing_directory(path: str | Path, name: str) -> Path:
    selected = Path(path).expanduser()
    if not selected.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    try:
        resolved = selected.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{name} is unavailable: {path}") from exc
    if not resolved.is_dir():
        raise ValueError(f"{name} is not a directory: {path}")
    return resolved

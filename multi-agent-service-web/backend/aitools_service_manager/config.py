from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

ProfileName = Literal["fake", "codex"]
_CONFIGURATION_VERSION = 1


@dataclass(frozen=True, slots=True)
class RuntimeConfiguration:
    """Persisted settings used the next time Control Plane starts."""

    profile: ProfileName = "fake"
    codex_home: Path | None = None
    provider_id: str = "codex"
    network_deny_enforced: bool = False
    allowed_path_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        if self.profile not in {"fake", "codex"}:
            raise ValueError("profile must be fake or codex")
        if not self.provider_id.strip():
            raise ValueError("provider id must not be empty")

        codex_home = self.codex_home
        if codex_home is not None:
            codex_home = _existing_directory(codex_home, "codex home")
            object.__setattr__(self, "codex_home", codex_home)
        if self.profile == "codex" and codex_home is None:
            raise ValueError("codex home is required for the codex profile")

        roots = tuple(
            _existing_directory(path, "allowed path root") for path in self.allowed_path_roots
        )
        if len(roots) != len(set(roots)):
            raise ValueError("allowed path roots must be unique")
        object.__setattr__(self, "allowed_path_roots", roots)

    def to_payload(self) -> dict[str, object]:
        return {
            "version": _CONFIGURATION_VERSION,
            "profile": self.profile,
            "codex_home": str(self.codex_home) if self.codex_home is not None else None,
            "provider_id": self.provider_id,
            "network_deny_enforced": self.network_deny_enforced,
            "allowed_path_roots": [str(path) for path in self.allowed_path_roots],
        }

    @classmethod
    def from_payload(cls, value: object) -> RuntimeConfiguration:
        if not isinstance(value, Mapping):
            raise ValueError("runtime configuration must be a JSON object")
        payload = cast(Mapping[object, object], value)
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
        if payload["version"] != _CONFIGURATION_VERSION:
            raise ValueError("runtime configuration version is not supported")

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
        return cls(
            profile=cast(ProfileName, profile),
            codex_home=Path(codex_home) if codex_home is not None else None,
            provider_id=provider_id,
            network_deny_enforced=network_deny_enforced,
            allowed_path_roots=tuple(
                Path(cast(str, path)) for path in cast(list[object], allowed_path_roots)
            ),
        )


class RuntimeConfigurationStore:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def load_or_create(self, default: RuntimeConfiguration) -> RuntimeConfiguration:
        if self.path.exists():
            return self.load()
        self.save(default)
        return default

    def load(self) -> RuntimeConfiguration:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"runtime configuration does not exist: {self.path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"runtime configuration cannot be read: {self.path}") from exc
        return RuntimeConfiguration.from_payload(payload)

    def save(self, configuration: RuntimeConfiguration) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_name(f"{self.path.name}.tmp")
        try:
            temporary_path.write_text(
                json.dumps(
                    configuration.to_payload(),
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            temporary_path.replace(self.path)
        finally:
            temporary_path.unlink(missing_ok=True)


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

    def control_plane_state_path(self, profile: ProfileName) -> Path:
        return (self.root / ".data" / "multi-agent-v3" / f"control-plane-{profile}.jsonl").resolve()


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

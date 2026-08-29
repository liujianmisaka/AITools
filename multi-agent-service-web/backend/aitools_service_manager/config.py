from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlparse

ProviderKind = Literal["fake", "codex", "claude"]
ClaudeRuntimeMode = Literal["native", "opencodex"]
CoordinatorReasoningEffort = Literal["none", "low", "medium", "high", "xhigh"]
_CONFIGURATION_VERSION = 6
_AUTONOMY_LESS_CONFIGURATION_VERSION = 5
_COORDINATORLESS_CONFIGURATION_VERSION = 4
_PREVIOUS_CONFIGURATION_VERSION = 3
_OLDER_CONFIGURATION_VERSION = 2
_LEGACY_CONFIGURATION_VERSION = 1
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+$")
_DISPLAY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._/-]{0,99}$")
_ENVIRONMENT_VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MODEL_PROVIDER_FIELD = re.compile(
    r"^model_providers\.([A-Za-z0-9_-]+)\."
    r"(base_url|env_key|name|requires_openai_auth|wire_api)$"
)
_CONTROL_PLANE_STATE_FILES = (
    "control-plane.jsonl",
    "control-plane-codex.jsonl",
    "control-plane-fake.jsonl",
)
_DEFAULT_CLAUDE_OPENCODEX_BASE_URL = "http://127.0.0.1:10100"
_DEFAULT_CLAUDE_OPENCODEX_AUTH_TOKEN_ENV = "ANTHROPIC_AUTH_TOKEN"
_DEFAULT_CLAUDE_OPENCODEX_AUTH_TOKEN = "opencodex-proxy"
_DEFAULT_COORDINATOR_MODEL = "pixel/gpt-5.6-luna"
_DEFAULT_COORDINATOR_BASE_URL = "http://127.0.0.1:10100/v1"
_DEFAULT_COORDINATOR_API_KEY_ENV = "OPENAI_API_KEY"
_CLAUDE_OPENCODEX_ENVIRONMENT_KEYS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
    "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
)


@dataclass(frozen=True, slots=True)
class ProviderConfiguration:
    """One provider instance registered in the shared Control Plane runtime."""

    provider_id: str = "fake"
    kind: ProviderKind = "fake"
    codex_home: Path | None = None
    config_overrides: tuple[str, ...] = ()
    claude_config_dir: Path | None = None
    claude_cli_path: Path | None = None
    model_ids: tuple[str, ...] = ()
    network_deny_enforced: bool = False

    def __post_init__(self) -> None:
        if not self.provider_id.strip():
            raise ValueError("provider id must not be empty")
        if self.kind not in {"fake", "codex", "claude"}:
            raise ValueError("provider kind must be fake, codex, or claude")

        codex_home = self.codex_home
        if codex_home is not None:
            codex_home = _existing_directory(codex_home, "codex home")
            object.__setattr__(self, "codex_home", codex_home)

        overrides = tuple(_validated_config_override(value) for value in self.config_overrides)
        object.__setattr__(self, "config_overrides", overrides)

        claude_config_dir = self.claude_config_dir
        if claude_config_dir is not None:
            claude_config_dir = _existing_directory(claude_config_dir, "claude config directory")
            object.__setattr__(self, "claude_config_dir", claude_config_dir)
        claude_cli_path = self.claude_cli_path
        if claude_cli_path is not None:
            claude_cli_path = _existing_file(claude_cli_path, "claude cli path")
            object.__setattr__(self, "claude_cli_path", claude_cli_path)

        model_ids = tuple(model.strip() for model in self.model_ids)
        if any(not model for model in model_ids):
            raise ValueError("Claude model ids must be non-empty")
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("Claude model ids must be unique")
        object.__setattr__(self, "model_ids", model_ids)

        if self.kind == "codex":
            if codex_home is None:
                raise ValueError("codex home is required for a codex provider")
            if claude_config_dir is not None or claude_cli_path is not None or model_ids:
                raise ValueError("codex providers cannot define Claude settings")
            return
        if self.kind == "claude":
            if codex_home is not None or overrides:
                raise ValueError("Claude providers cannot define Codex settings")
            if not model_ids:
                raise ValueError("at least one Claude model id is required")
            return
        if (
            codex_home is not None
            or overrides
            or claude_config_dir is not None
            or claude_cli_path is not None
            or model_ids
            or self.network_deny_enforced
        ):
            raise ValueError("fake providers cannot define provider-specific settings")

    def to_payload(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "kind": self.kind,
            "codex_home": str(self.codex_home) if self.codex_home is not None else None,
            "config_overrides": list(self.config_overrides),
            "claude_config_dir": (
                str(self.claude_config_dir) if self.claude_config_dir is not None else None
            ),
            "claude_cli_path": (
                str(self.claude_cli_path) if self.claude_cli_path is not None else None
            ),
            "model_ids": list(self.model_ids),
            "network_deny_enforced": self.network_deny_enforced,
        }

    def to_profile_payload(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "kind": self.kind,
            "codex_home": self.codex_home,
            "config_overrides": self.config_overrides,
            "claude_config_dir": self.claude_config_dir,
            "claude_cli_path": self.claude_cli_path,
            "model_ids": self.model_ids,
            "network_deny_enforced": self.network_deny_enforced,
        }

    @classmethod
    def from_payload(cls, value: object) -> ProviderConfiguration:
        if not isinstance(value, Mapping):
            raise ValueError("provider configuration must be a JSON object")
        payload = cast(Mapping[object, object], value)
        legacy_fields = {
            "provider_id",
            "kind",
            "codex_home",
            "config_overrides",
            "network_deny_enforced",
        }
        expected_fields = legacy_fields | {"claude_config_dir", "claude_cli_path", "model_ids"}
        if set(payload) not in (legacy_fields, expected_fields):
            raise ValueError("provider configuration fields do not match version 3")

        provider_id = payload["provider_id"]
        kind = payload["kind"]
        codex_home = payload["codex_home"]
        config_overrides = payload["config_overrides"]
        claude_config_dir = payload.get("claude_config_dir")
        claude_cli_path = payload.get("claude_cli_path")
        model_ids = payload.get("model_ids", [])
        network_deny_enforced = payload["network_deny_enforced"]
        if not isinstance(provider_id, str):
            raise ValueError("provider id must be a string")
        if kind not in {"fake", "codex", "claude"}:
            raise ValueError("provider kind must be fake, codex, or claude")
        if codex_home is not None and not isinstance(codex_home, str):
            raise ValueError("codex home must be a string or null")
        if claude_config_dir is not None and not isinstance(claude_config_dir, str):
            raise ValueError("Claude config directory must be a string or null")
        if claude_cli_path is not None and not isinstance(claude_cli_path, str):
            raise ValueError("Claude CLI path must be a string or null")
        if not isinstance(config_overrides, list) or not all(
            isinstance(item, str) for item in cast(list[object], config_overrides)
        ):
            raise ValueError("config overrides must be an array of strings")
        if not isinstance(model_ids, list) or not all(
            isinstance(item, str) for item in cast(list[object], model_ids)
        ):
            raise ValueError("Claude model ids must be an array of strings")
        if not isinstance(network_deny_enforced, bool):
            raise ValueError("network deny enforcement must be a boolean")
        return cls(
            provider_id=provider_id,
            kind=cast(ProviderKind, kind),
            codex_home=Path(codex_home) if codex_home is not None else None,
            config_overrides=tuple(
                cast(str, item) for item in cast(list[object], config_overrides)
            ),
            claude_config_dir=(Path(claude_config_dir) if claude_config_dir is not None else None),
            claude_cli_path=Path(claude_cli_path) if claude_cli_path is not None else None,
            model_ids=tuple(cast(str, item) for item in cast(list[object], model_ids)),
            network_deny_enforced=network_deny_enforced,
        )


@dataclass(frozen=True, slots=True)
class RuntimeConfiguration:
    """Persisted settings used the next time Control Plane starts."""

    providers: tuple[ProviderConfiguration, ...] = (ProviderConfiguration(),)
    allowed_path_roots: tuple[Path, ...] = ()
    claude_runtime_mode: ClaudeRuntimeMode = "native"
    claude_opencodex_base_url: str = _DEFAULT_CLAUDE_OPENCODEX_BASE_URL
    claude_opencodex_auth_token_env: str = _DEFAULT_CLAUDE_OPENCODEX_AUTH_TOKEN_ENV
    coordinator_model: str = _DEFAULT_COORDINATOR_MODEL
    coordinator_reasoning_effort: CoordinatorReasoningEffort = "medium"
    coordinator_api_key_env: str = _DEFAULT_COORDINATOR_API_KEY_ENV
    coordinator_base_url: str | None = _DEFAULT_COORDINATOR_BASE_URL
    coordinator_max_decision_steps: int = 16
    coordinator_wait_timeout_ms: int = 0
    coordinator_max_concurrent_delegations: int = 8
    coordinator_max_total_delegations: int = 30
    coordinator_max_delegation_depth: int = 3
    coordinator_max_plan_revisions: int = 10
    coordinator_max_retries_per_node: int = 2
    coordinator_max_runtime_minutes: int = 120
    coordinator_max_model_activations: int = 50

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

        if self.claude_runtime_mode not in {"native", "opencodex"}:
            raise ValueError("Claude runtime mode must be native or opencodex")
        base_url = self.claude_opencodex_base_url.strip().rstrip("/")
        if not base_url:
            raise ValueError("Claude OpenCodex base URL must not be empty")
        _validate_provider_base_url(base_url)
        object.__setattr__(self, "claude_opencodex_base_url", base_url)
        auth_token_env = self.claude_opencodex_auth_token_env.strip()
        if _ENVIRONMENT_VARIABLE.fullmatch(auth_token_env) is None:
            raise ValueError("Claude OpenCodex auth token environment variable is invalid")
        object.__setattr__(self, "claude_opencodex_auth_token_env", auth_token_env)

        coordinator_model = self.coordinator_model.strip()
        if not coordinator_model:
            raise ValueError("Coordinator model must not be empty")
        object.__setattr__(self, "coordinator_model", coordinator_model)
        if self.coordinator_reasoning_effort not in {
            "none",
            "low",
            "medium",
            "high",
            "xhigh",
        }:
            raise ValueError("Coordinator reasoning effort is invalid")
        coordinator_api_key_env = self.coordinator_api_key_env.strip()
        if _ENVIRONMENT_VARIABLE.fullmatch(coordinator_api_key_env) is None:
            raise ValueError("Coordinator API key environment variable is invalid")
        object.__setattr__(self, "coordinator_api_key_env", coordinator_api_key_env)
        coordinator_base_url = self.coordinator_base_url
        if coordinator_base_url is not None:
            coordinator_base_url = coordinator_base_url.strip().rstrip("/")
            _validate_provider_base_url(coordinator_base_url)
            object.__setattr__(self, "coordinator_base_url", coordinator_base_url)
        if not 1 <= self.coordinator_max_decision_steps <= 128:
            raise ValueError("Coordinator max decision steps must be between 1 and 128")
        if not 0 <= self.coordinator_wait_timeout_ms <= 300_000:
            raise ValueError("Coordinator wait timeout must be between 0 and 300000 milliseconds")
        _validate_coordinator_autonomy_limits(self)

    def to_payload(self) -> dict[str, object]:
        return {
            "version": _CONFIGURATION_VERSION,
            "providers": [provider.to_payload() for provider in self.providers],
            "allowed_path_roots": [str(path) for path in self.allowed_path_roots],
            "claude_runtime_mode": self.claude_runtime_mode,
            "claude_opencodex_base_url": self.claude_opencodex_base_url,
            "claude_opencodex_auth_token_env": self.claude_opencodex_auth_token_env,
            "coordinator_model": self.coordinator_model,
            "coordinator_reasoning_effort": self.coordinator_reasoning_effort,
            "coordinator_api_key_env": self.coordinator_api_key_env,
            "coordinator_base_url": self.coordinator_base_url,
            "coordinator_max_decision_steps": self.coordinator_max_decision_steps,
            "coordinator_wait_timeout_ms": self.coordinator_wait_timeout_ms,
            "coordinator_max_concurrent_delegations": (self.coordinator_max_concurrent_delegations),
            "coordinator_max_total_delegations": self.coordinator_max_total_delegations,
            "coordinator_max_delegation_depth": self.coordinator_max_delegation_depth,
            "coordinator_max_plan_revisions": self.coordinator_max_plan_revisions,
            "coordinator_max_retries_per_node": self.coordinator_max_retries_per_node,
            "coordinator_max_runtime_minutes": self.coordinator_max_runtime_minutes,
            "coordinator_max_model_activations": self.coordinator_max_model_activations,
        }

    @classmethod
    def from_payload(cls, value: object) -> RuntimeConfiguration:
        if not isinstance(value, Mapping):
            raise ValueError("runtime configuration must be a JSON object")
        payload = cast(Mapping[object, object], value)
        version = payload.get("version")
        if version == _LEGACY_CONFIGURATION_VERSION:
            return _migrate_legacy_configuration(payload)
        if version == _OLDER_CONFIGURATION_VERSION:
            return _migrate_previous_configuration(payload)
        if version == _PREVIOUS_CONFIGURATION_VERSION:
            return _migrate_v3_configuration(payload)
        if version == _COORDINATORLESS_CONFIGURATION_VERSION:
            return _migrate_v4_configuration(payload)
        if version == _AUTONOMY_LESS_CONFIGURATION_VERSION:
            return _migrate_v5_configuration(payload)
        if version != _CONFIGURATION_VERSION:
            raise ValueError("runtime configuration version is not supported")

        expected_fields = {
            "version",
            "providers",
            "allowed_path_roots",
            "claude_runtime_mode",
            "claude_opencodex_base_url",
            "claude_opencodex_auth_token_env",
            "coordinator_model",
            "coordinator_reasoning_effort",
            "coordinator_api_key_env",
            "coordinator_base_url",
            "coordinator_max_decision_steps",
            "coordinator_wait_timeout_ms",
            "coordinator_max_concurrent_delegations",
            "coordinator_max_total_delegations",
            "coordinator_max_delegation_depth",
            "coordinator_max_plan_revisions",
            "coordinator_max_retries_per_node",
            "coordinator_max_runtime_minutes",
            "coordinator_max_model_activations",
        }
        if set(payload) != expected_fields:
            raise ValueError("runtime configuration fields do not match version 6")
        providers = payload["providers"]
        allowed_path_roots = payload["allowed_path_roots"]
        claude_runtime_mode = payload["claude_runtime_mode"]
        claude_opencodex_base_url = payload["claude_opencodex_base_url"]
        claude_opencodex_auth_token_env = payload["claude_opencodex_auth_token_env"]
        coordinator_model = payload["coordinator_model"]
        coordinator_reasoning_effort = payload["coordinator_reasoning_effort"]
        coordinator_api_key_env = payload["coordinator_api_key_env"]
        coordinator_base_url = payload["coordinator_base_url"]
        coordinator_max_decision_steps = payload["coordinator_max_decision_steps"]
        coordinator_wait_timeout_ms = payload["coordinator_wait_timeout_ms"]
        coordinator_max_concurrent_delegations = payload["coordinator_max_concurrent_delegations"]
        coordinator_max_total_delegations = payload["coordinator_max_total_delegations"]
        coordinator_max_delegation_depth = payload["coordinator_max_delegation_depth"]
        coordinator_max_plan_revisions = payload["coordinator_max_plan_revisions"]
        coordinator_max_retries_per_node = payload["coordinator_max_retries_per_node"]
        coordinator_max_runtime_minutes = payload["coordinator_max_runtime_minutes"]
        coordinator_max_model_activations = payload["coordinator_max_model_activations"]
        if not isinstance(providers, list):
            raise ValueError("providers must be an array")
        if not isinstance(allowed_path_roots, list) or not all(
            isinstance(path, str) for path in cast(list[object], allowed_path_roots)
        ):
            raise ValueError("allowed path roots must be an array of strings")
        if claude_runtime_mode not in {"native", "opencodex"}:
            raise ValueError("Claude runtime mode must be native or opencodex")
        if not isinstance(claude_opencodex_base_url, str):
            raise ValueError("Claude OpenCodex base URL must be a string")
        if not isinstance(claude_opencodex_auth_token_env, str):
            raise ValueError("Claude OpenCodex auth token environment variable must be a string")
        if not isinstance(coordinator_model, str):
            raise ValueError("Coordinator model must be a string")
        if coordinator_reasoning_effort not in {"none", "low", "medium", "high", "xhigh"}:
            raise ValueError("Coordinator reasoning effort is invalid")
        if not isinstance(coordinator_api_key_env, str):
            raise ValueError("Coordinator API key environment variable must be a string")
        if coordinator_base_url is not None and not isinstance(coordinator_base_url, str):
            raise ValueError("Coordinator base URL must be a string or null")
        if isinstance(coordinator_max_decision_steps, bool) or not isinstance(
            coordinator_max_decision_steps, int
        ):
            raise ValueError("Coordinator max decision steps must be an integer")
        if isinstance(coordinator_wait_timeout_ms, bool) or not isinstance(
            coordinator_wait_timeout_ms, int
        ):
            raise ValueError("Coordinator wait timeout must be an integer")
        autonomy_values = {
            "coordinator_max_concurrent_delegations": (coordinator_max_concurrent_delegations),
            "coordinator_max_total_delegations": coordinator_max_total_delegations,
            "coordinator_max_delegation_depth": coordinator_max_delegation_depth,
            "coordinator_max_plan_revisions": coordinator_max_plan_revisions,
            "coordinator_max_retries_per_node": coordinator_max_retries_per_node,
            "coordinator_max_runtime_minutes": coordinator_max_runtime_minutes,
            "coordinator_max_model_activations": coordinator_max_model_activations,
        }
        if any(
            isinstance(item, bool) or not isinstance(item, int) for item in autonomy_values.values()
        ):
            raise ValueError("Coordinator autonomy limits must be integers")
        return cls(
            providers=tuple(
                ProviderConfiguration.from_payload(item) for item in cast(list[object], providers)
            ),
            allowed_path_roots=tuple(
                Path(cast(str, path)) for path in cast(list[object], allowed_path_roots)
            ),
            claude_runtime_mode=cast(ClaudeRuntimeMode, claude_runtime_mode),
            claude_opencodex_base_url=claude_opencodex_base_url,
            claude_opencodex_auth_token_env=claude_opencodex_auth_token_env,
            coordinator_model=coordinator_model,
            coordinator_reasoning_effort=cast(
                CoordinatorReasoningEffort, coordinator_reasoning_effort
            ),
            coordinator_api_key_env=coordinator_api_key_env,
            coordinator_base_url=coordinator_base_url,
            coordinator_max_decision_steps=coordinator_max_decision_steps,
            coordinator_wait_timeout_ms=coordinator_wait_timeout_ms,
            coordinator_max_concurrent_delegations=cast(
                int, coordinator_max_concurrent_delegations
            ),
            coordinator_max_total_delegations=cast(int, coordinator_max_total_delegations),
            coordinator_max_delegation_depth=cast(int, coordinator_max_delegation_depth),
            coordinator_max_plan_revisions=cast(int, coordinator_max_plan_revisions),
            coordinator_max_retries_per_node=cast(int, coordinator_max_retries_per_node),
            coordinator_max_runtime_minutes=cast(int, coordinator_max_runtime_minutes),
            coordinator_max_model_activations=cast(int, coordinator_max_model_activations),
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
        if _payload_version(payload) in {
            _LEGACY_CONFIGURATION_VERSION,
            _OLDER_CONFIGURATION_VERSION,
            _PREVIOUS_CONFIGURATION_VERSION,
            _COORDINATORLESS_CONFIGURATION_VERSION,
            _AUTONOMY_LESS_CONFIGURATION_VERSION,
        }:
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
    coordinator_port: int = 8020
    terminal_host_port: int = 8022
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
            "coordinator": self.coordinator_port,
            "terminal host": self.terminal_host_port,
        }
        for name, port in ports.items():
            if not 1 <= port <= 65535:
                raise ValueError(f"{name} port must be between 1 and 65535")
        if len(set(ports.values())) != len(ports):
            raise ValueError(
                "management, service web, control plane, main web, coordinator, and terminal host "
                "ports must differ"
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

    @property
    def coordinator_url(self) -> str:
        return f"http://127.0.0.1:{self.coordinator_port}"

    @property
    def terminal_host_url(self) -> str:
        return f"http://127.0.0.1:{self.terminal_host_port}"

    @property
    def coordinator_state_path(self) -> Path:
        return (self.root / ".data" / "multi-agent-coordinator" / "sessions.jsonl").resolve()

    @property
    def terminal_host_state_path(self) -> Path:
        return (self.root / ".data" / "multi-agent-terminal-host" / "sessions.jsonl").resolve()

    @property
    def terminal_host_auth_token_path(self) -> Path:
        return (self.root / ".data" / "multi-agent-terminal-host" / "auth-token").resolve()

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


def _migrate_previous_configuration(
    payload: Mapping[object, object],
) -> RuntimeConfiguration:
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
    return RuntimeConfiguration(
        providers=tuple(
            ProviderConfiguration.from_payload(item) for item in cast(list[object], providers)
        ),
        allowed_path_roots=tuple(
            Path(cast(str, path)) for path in cast(list[object], allowed_path_roots)
        ),
    )


def _migrate_v3_configuration(
    payload: Mapping[object, object],
) -> RuntimeConfiguration:
    expected_fields = {"version", "providers", "allowed_path_roots"}
    if set(payload) != expected_fields:
        raise ValueError("runtime configuration fields do not match version 3")
    providers = payload["providers"]
    allowed_path_roots = payload["allowed_path_roots"]
    if not isinstance(providers, list):
        raise ValueError("providers must be an array")
    if not isinstance(allowed_path_roots, list) or not all(
        isinstance(path, str) for path in cast(list[object], allowed_path_roots)
    ):
        raise ValueError("allowed path roots must be an array of strings")
    return RuntimeConfiguration(
        providers=tuple(
            ProviderConfiguration.from_payload(item) for item in cast(list[object], providers)
        ),
        allowed_path_roots=tuple(
            Path(cast(str, path)) for path in cast(list[object], allowed_path_roots)
        ),
    )


def _migrate_v4_configuration(
    payload: Mapping[object, object],
) -> RuntimeConfiguration:
    expected_fields = {
        "version",
        "providers",
        "allowed_path_roots",
        "claude_runtime_mode",
        "claude_opencodex_base_url",
        "claude_opencodex_auth_token_env",
    }
    if set(payload) != expected_fields:
        raise ValueError("runtime configuration fields do not match version 4")
    migrated = dict(payload)
    migrated["version"] = _AUTONOMY_LESS_CONFIGURATION_VERSION
    migrated.update(
        {
            "coordinator_model": _DEFAULT_COORDINATOR_MODEL,
            "coordinator_reasoning_effort": "medium",
            "coordinator_api_key_env": _DEFAULT_COORDINATOR_API_KEY_ENV,
            "coordinator_base_url": _DEFAULT_COORDINATOR_BASE_URL,
            "coordinator_max_decision_steps": 16,
            "coordinator_wait_timeout_ms": 0,
        }
    )
    return RuntimeConfiguration.from_payload(migrated)


def _migrate_v5_configuration(
    payload: Mapping[object, object],
) -> RuntimeConfiguration:
    expected_fields = {
        "version",
        "providers",
        "allowed_path_roots",
        "claude_runtime_mode",
        "claude_opencodex_base_url",
        "claude_opencodex_auth_token_env",
        "coordinator_model",
        "coordinator_reasoning_effort",
        "coordinator_api_key_env",
        "coordinator_base_url",
        "coordinator_max_decision_steps",
        "coordinator_wait_timeout_ms",
    }
    if set(payload) != expected_fields:
        raise ValueError("runtime configuration fields do not match version 5")
    migrated = dict(payload)
    migrated["version"] = _CONFIGURATION_VERSION
    migrated.update(
        {
            "coordinator_max_concurrent_delegations": 8,
            "coordinator_max_total_delegations": 30,
            "coordinator_max_delegation_depth": 3,
            "coordinator_max_plan_revisions": 10,
            "coordinator_max_retries_per_node": 2,
            "coordinator_max_runtime_minutes": 120,
            "coordinator_max_model_activations": 50,
        }
    )
    return RuntimeConfiguration.from_payload(migrated)


def _validate_coordinator_autonomy_limits(configuration: RuntimeConfiguration) -> None:
    positive_limits = {
        "max concurrent delegations": configuration.coordinator_max_concurrent_delegations,
        "max total delegations": configuration.coordinator_max_total_delegations,
        "max plan revisions": configuration.coordinator_max_plan_revisions,
        "max runtime minutes": configuration.coordinator_max_runtime_minutes,
        "max model activations": configuration.coordinator_max_model_activations,
    }
    for name, value in positive_limits.items():
        if isinstance(value, bool) or value < 1:
            raise ValueError(f"Coordinator {name} must be positive")
    non_negative_limits = {
        "max delegation depth": configuration.coordinator_max_delegation_depth,
        "max retries per node": configuration.coordinator_max_retries_per_node,
    }
    for name, value in non_negative_limits.items():
        if isinstance(value, bool) or value < 0:
            raise ValueError(f"Coordinator {name} must not be negative")


def _payload_version(value: object) -> object:
    if not isinstance(value, Mapping):
        return None
    return cast(Mapping[object, object], value).get("version")


def apply_claude_runtime_environment(
    configuration: RuntimeConfiguration,
    environment: MutableMapping[str, str] | None = None,
) -> None:
    """Apply the explicit Claude backend choice to a Control Plane process."""

    target = os.environ if environment is None else environment
    if configuration.claude_runtime_mode == "native":
        for key in _CLAUDE_OPENCODEX_ENVIRONMENT_KEYS:
            target.pop(key, None)
        return

    auth_token = target.get(configuration.claude_opencodex_auth_token_env, "").strip()
    if not auth_token and _uses_default_local_opencodex_gateway(configuration):
        auth_token = _DEFAULT_CLAUDE_OPENCODEX_AUTH_TOKEN
        target[configuration.claude_opencodex_auth_token_env] = auth_token
    if not auth_token:
        raise ValueError(
            "Claude OpenCodex mode requires a non-empty auth token in environment variable "
            f"{configuration.claude_opencodex_auth_token_env}"
        )
    target.pop("ANTHROPIC_MODEL", None)
    target["ANTHROPIC_BASE_URL"] = configuration.claude_opencodex_base_url
    target["ANTHROPIC_AUTH_TOKEN"] = auth_token
    target["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] = "1"
    target["CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST"] = "1"
    target["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = "829800"


def _uses_default_local_opencodex_gateway(configuration: RuntimeConfiguration) -> bool:
    return (
        configuration.claude_opencodex_base_url == _DEFAULT_CLAUDE_OPENCODEX_BASE_URL
        and configuration.claude_opencodex_auth_token_env
        == _DEFAULT_CLAUDE_OPENCODEX_AUTH_TOKEN_ENV
    )


def _validated_config_override(value: str) -> str:
    normalized = value.strip()
    if not normalized or "\n" in normalized or "\r" in normalized:
        raise ValueError("config overrides must be non-empty single-line values")
    key, separator, raw_value = normalized.partition("=")
    key = key.strip()
    if not separator or not key or not raw_value.strip():
        raise ValueError("config overrides must use key=value syntax")
    parsed_value = _parse_override_value(raw_value)

    if key == "model_provider":
        _require_identifier_value(parsed_value, "model_provider")
        return normalized

    match = _MODEL_PROVIDER_FIELD.fullmatch(key)
    if match is None:
        raise ValueError(
            "config override key is not supported; use model_provider or a safe "
            "model_providers.<id> endpoint/environment reference"
        )
    field_name = match.group(2)
    if field_name == "name":
        if not isinstance(parsed_value, str) or _DISPLAY_NAME.fullmatch(parsed_value) is None:
            raise ValueError("model provider name contains unsupported characters")
    elif field_name == "wire_api":
        _require_identifier_value(parsed_value, field_name)
    elif field_name == "env_key":
        if (
            not isinstance(parsed_value, str)
            or _ENVIRONMENT_VARIABLE.fullmatch(parsed_value) is None
        ):
            raise ValueError("model provider env_key must name an environment variable")
    elif field_name == "base_url":
        _validate_provider_base_url(parsed_value)
    elif not isinstance(parsed_value, bool):
        raise ValueError("model provider requires_openai_auth must be a boolean")
    return normalized


def _parse_override_value(raw_value: str) -> object:
    try:
        return cast(object, tomllib.loads("value=" + raw_value)["value"])
    except (tomllib.TOMLDecodeError, KeyError) as exc:
        raise ValueError("config override value must be valid TOML") from exc


def _require_identifier_value(value: object, field_name: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a non-empty identifier")


def _validate_provider_base_url(value: object) -> None:
    if not isinstance(value, str):
        raise ValueError("model provider base_url must be a string")
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "model provider base_url must be an HTTP(S) URL without credentials, query, or fragment"
        )


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


def _existing_file(path: str | Path, name: str) -> Path:
    selected = Path(path).expanduser()
    if not selected.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    try:
        resolved = selected.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{name} is unavailable: {path}") from exc
    if not resolved.is_file():
        raise ValueError(f"{name} is not a file: {path}")
    return resolved

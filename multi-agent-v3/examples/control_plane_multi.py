from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from fastapi import FastAPI
from misaka_claude_provider import ClaudeAgentProvider, ClaudeProviderConfig
from misaka_codex_provider import CodexAgentProvider, CodexProviderConfig
from misaka_control_plane import (
    ControlPlaneService,
    WorkingDirectoryPolicy,
    create_app,
    create_local_service_manager,
)
from misaka_control_plane_workflow import create_dag_runner
from misaka_fake_agent import FakeAgentProvider
from misaka_invocation_runtime import InvocationProvider, InvocationRuntime
from misaka_kernel.lifecycle import AsyncDisposer
from misaka_session_capability import MemorySessionStore

_PROVIDER_FIELDS = {
    "provider_id",
    "kind",
    "codex_home",
    "config_overrides",
    "claude_config_dir",
    "claude_cli_path",
    "model_ids",
    "network_deny_enforced",
}
_LEGACY_PROVIDER_FIELDS = _PROVIDER_FIELDS - {
    "claude_config_dir",
    "claude_cli_path",
    "model_ids",
}


def _create_providers(
    configurations: tuple[Mapping[str, object], ...],
    *,
    codex_app_server_url: str | None = None,
) -> tuple[tuple[str, InvocationProvider], ...]:
    if not configurations:
        raise ValueError("at least one provider configuration is required")
    providers = tuple(
        _create_provider(configuration, codex_app_server_url=codex_app_server_url)
        for configuration in configurations
    )
    provider_ids = [provider_id for provider_id, _ in providers]
    if len(provider_ids) != len(set(provider_ids)):
        raise ValueError("provider ids must be unique")
    return providers


def _create_provider(
    configuration: Mapping[str, object],
    *,
    codex_app_server_url: str | None = None,
) -> tuple[str, InvocationProvider]:
    if set(configuration) == _LEGACY_PROVIDER_FIELDS:
        configuration = {
            **configuration,
            "claude_config_dir": None,
            "claude_cli_path": None,
            "model_ids": (),
        }
    if set(configuration) != _PROVIDER_FIELDS:
        raise ValueError("provider configuration fields do not match the multi-provider profile")
    provider_id = _required_string(configuration, "provider_id")
    kind = _required_string(configuration, "kind")
    codex_home = configuration["codex_home"]
    config_overrides = _string_tuple(configuration, "config_overrides")
    claude_config_dir = configuration["claude_config_dir"]
    claude_cli_path = configuration["claude_cli_path"]
    model_ids = _string_tuple(configuration, "model_ids")
    network_deny_enforced = configuration["network_deny_enforced"]
    if not isinstance(network_deny_enforced, bool):
        raise ValueError("network_deny_enforced must be a boolean")

    if kind == "fake":
        if (
            codex_home is not None
            or config_overrides
            or claude_config_dir is not None
            or claude_cli_path is not None
            or model_ids
            or network_deny_enforced
        ):
            raise ValueError("fake provider configuration contains provider-only settings")
        return provider_id, FakeAgentProvider()
    if kind == "codex":
        if claude_config_dir is not None or claude_cli_path is not None:
            raise ValueError("codex provider configuration contains Claude-only settings")
        selected_codex_home = _existing_directory(codex_home, "codex_home")
        return provider_id, CodexAgentProvider(
            CodexProviderConfig(
                provider_id=provider_id,
                codex_home=selected_codex_home,
                app_server_url=codex_app_server_url,
                config_overrides=config_overrides,
                model_ids=model_ids,
                network_deny_enforced=network_deny_enforced,
            ),
            session_store=MemorySessionStore(),
        )
    if kind != "claude":
        raise ValueError("provider kind must be fake, codex, or claude")
    if codex_home is not None or config_overrides:
        raise ValueError("Claude provider configuration contains Codex-only settings")
    if not model_ids:
        raise ValueError("Claude provider requires at least one model id")
    selected_claude_config_dir = _optional_directory(claude_config_dir, "claude_config_dir")
    selected_claude_cli_path = _optional_file(claude_cli_path, "claude_cli_path")
    return provider_id, ClaudeAgentProvider(
        ClaudeProviderConfig(
            provider_id=provider_id,
            claude_config_dir=selected_claude_config_dir,
            cli_path=selected_claude_cli_path,
            model_ids=model_ids,
            network_deny_enforced=network_deny_enforced,
        ),
        session_store=MemorySessionStore(),
    )


def build_app(
    *,
    provider_configs: tuple[Mapping[str, object], ...],
    allowed_path_roots: tuple[Path, ...] = (),
    state_path: Path,
    a2a_node_port: int = 8025,
    a2a_agent_host_port: int = 8026,
    codex_app_server_url: str | None = None,
) -> FastAPI:
    runtime = InvocationRuntime()
    providers = _create_providers(
        provider_configs,
        codex_app_server_url=codex_app_server_url,
    )

    async def register_providers(target: InvocationRuntime) -> None:
        await _register_providers(target, providers)

    service = ControlPlaneService(
        runtime,
        state_path=state_path,
        provider_setup=register_providers,
        dag_runner=create_dag_runner(runtime),
        cwd_policy=WorkingDirectoryPolicy(allowed_path_roots),
        service_manager=create_local_service_manager(
            project_root=Path(__file__).resolve().parents[1],
            python_executable=sys.executable,
            a2a_node_port=a2a_node_port,
            a2a_agent_host_port=a2a_agent_host_port,
        ),
    )
    return create_app(service)


async def _register_providers(
    target: InvocationRuntime,
    providers: tuple[tuple[str, InvocationProvider], ...],
) -> None:
    if target.descriptors():
        return
    disposers: list[AsyncDisposer] = []
    try:
        for provider_id, provider in providers:
            disposers.append(await target.register_provider(provider_id, provider))
    except BaseException:
        for dispose in reversed(disposers):
            await dispose()
        raise


def _required_string(configuration: Mapping[str, object], name: str) -> str:
    value = configuration[name]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _string_tuple(configuration: Mapping[str, object], name: str) -> tuple[str, ...]:
    value = configuration[name]
    if not isinstance(value, tuple) or not all(
        isinstance(item, str) and item.strip() for item in cast(tuple[object, ...], value)
    ):
        raise ValueError(f"{name} must be a tuple of non-empty strings")
    return cast(tuple[str, ...], value)


def _optional_directory(value: object, name: str) -> Path | None:
    if value is None:
        return None
    return _existing_directory(value, name)


def _optional_file(value: object, name: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, Path):
        raise ValueError(f"{name} must be a Path")
    try:
        resolved = value.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{name} is unavailable") from exc
    if not resolved.is_file():
        raise ValueError(f"{name} must be a file")
    return resolved


def _existing_directory(value: object, name: str) -> Path:
    if not isinstance(value, Path):
        raise ValueError(f"{name} must be a Path")
    try:
        resolved = value.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{name} is unavailable") from exc
    if not resolved.is_dir():
        raise ValueError(f"{name} must be a directory")
    return resolved

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

from aitools_service_manager.config import ProviderConfiguration, RuntimeConfigurationStore

_NETWORK_DENY_OVERRIDES = (
    'web_search="disabled"',
    "tools.web_search=false",
    "sandbox_workspace_write.network_access=false",
)


def app_server_command(configuration_path: Path, *, listen_url: str) -> tuple[str, ...]:
    configuration = RuntimeConfigurationStore(configuration_path).load()
    providers = tuple(provider for provider in configuration.providers if provider.kind == "codex")
    codex = shutil.which("codex.exe") or shutil.which("codex")
    if codex is None:
        raise RuntimeError("Codex CLI was not found on PATH")

    command = [codex]
    overrides = _shared_codex_overrides(providers)
    for override in overrides:
        command.extend(("--config", override))
    command.extend(("app-server", "--listen", listen_url))
    return tuple(command)


def app_server_environment(configuration_path: Path) -> dict[str, str]:
    configuration = RuntimeConfigurationStore(configuration_path).load()
    providers = tuple(provider for provider in configuration.providers if provider.kind == "codex")
    homes = {provider.codex_home for provider in providers}
    if len(homes) > 1:
        raise ValueError("shared Codex App Server requires all Codex providers to use one home")
    environment = os.environ.copy()
    if homes:
        codex_home = next(iter(homes))
        if codex_home is None:
            raise AssertionError("validated Codex provider must define codex_home")
        environment["CODEX_HOME"] = str(codex_home)
    return environment


def _shared_codex_overrides(
    providers: tuple[ProviderConfiguration, ...],
) -> tuple[str, ...]:
    by_key: dict[str, str] = {}
    network_policies: set[bool] = set()
    for provider in providers:
        network_policies.add(provider.network_deny_enforced)
        for override in provider.config_overrides:
            key, separator, _ = override.partition("=")
            normalized_key = key.strip()
            if not separator or normalized_key == "model_provider":
                continue
            existing = by_key.get(normalized_key)
            if existing is not None and existing != override:
                raise ValueError(f"Codex providers define conflicting override: {normalized_key}")
            by_key[normalized_key] = override
    if len(network_policies) > 1:
        raise ValueError(
            "shared Codex App Server requires one network policy for all Codex providers"
        )
    if network_policies == {True}:
        for override in _NETWORK_DENY_OVERRIDES:
            key = override.partition("=")[0]
            by_key[key] = override
    return tuple(by_key.values())


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the shared Codex App Server")
    parser.add_argument("--configuration-path", type=Path, required=True)
    parser.add_argument("--listen-url", required=True)
    args = parser.parse_args()
    completed = subprocess.run(
        app_server_command(args.configuration_path, listen_url=args.listen_url),
        env=app_server_environment(args.configuration_path),
        check=False,
    )
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()

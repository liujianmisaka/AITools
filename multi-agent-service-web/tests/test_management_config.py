from __future__ import annotations

import json
from pathlib import Path

import pytest
from aitools_service_manager.catalog import control_plane_command, coordinator_command
from aitools_service_manager.config import (
    ManagementConfig,
    ProviderConfiguration,
    RuntimeConfiguration,
    RuntimeConfigurationStore,
)
from aitools_service_manager.coordinator_host import coordinator_arguments


def test_management_config_uses_aitools_owned_runtime_configuration(tmp_path: Path) -> None:
    config = ManagementConfig(root=tmp_path)

    assert (
        config.configuration_path
        == (tmp_path / ".data" / "aitools-service-manager" / "configuration.json").resolve()
    )
    assert config.management_url == "http://127.0.0.1:8014"
    assert config.coordinator_url == "http://127.0.0.1:8020"
    assert config.initial_runtime_configuration == RuntimeConfiguration()
    assert (
        config.control_plane_state_path()
        == (tmp_path / ".data" / "multi-agent-v3" / "control-plane.jsonl").resolve()
    )


def test_runtime_configuration_requires_valid_codex_home_and_allowed_roots(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    allowed = tmp_path / "allowed"
    codex_home.mkdir()
    allowed.mkdir()

    provider = ProviderConfiguration(
        provider_id="codex",
        kind="codex",
        codex_home=codex_home,
    )
    configuration = RuntimeConfiguration(providers=(provider,), allowed_path_roots=(allowed,))

    assert configuration.providers[0].codex_home == codex_home.resolve()
    assert configuration.allowed_path_roots == (allowed.resolve(),)
    with pytest.raises(ValueError, match="codex home"):
        ProviderConfiguration(provider_id="codex", kind="codex")
    with pytest.raises(ValueError, match="at least one provider"):
        RuntimeConfiguration(providers=())
    with pytest.raises(ValueError, match="absolute"):
        RuntimeConfiguration(allowed_path_roots=(Path("relative"),))
    with pytest.raises(ValueError, match="unavailable"):
        RuntimeConfiguration(allowed_path_roots=(tmp_path / "missing",))


def test_runtime_configuration_store_persists_and_reloads_exact_settings(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    allowed = tmp_path / "allowed"
    codex_home.mkdir()
    allowed.mkdir()
    store = RuntimeConfigurationStore(tmp_path / "configuration.json")
    expected = RuntimeConfiguration(
        providers=(
            ProviderConfiguration(provider_id="fake-local"),
            ProviderConfiguration(
                provider_id="codex-local",
                kind="codex",
                codex_home=codex_home,
                config_overrides=('model_provider="local"',),
                network_deny_enforced=True,
            ),
        ),
        allowed_path_roots=(allowed,),
    )

    store.save(expected)

    assert store.load() == expected


def test_runtime_configuration_store_uses_distinct_files_for_overlapping_saves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RuntimeConfigurationStore(tmp_path / "configuration.json")
    outer = RuntimeConfiguration(providers=(ProviderConfiguration(provider_id="outer"),))
    inner = RuntimeConfiguration(providers=(ProviderConfiguration(provider_id="inner"),))
    original_replace = Path.replace
    temporary_paths: list[Path] = []
    overlapping = False

    def replace_with_overlap(source: Path, target: Path) -> Path:
        nonlocal overlapping
        temporary_paths.append(source)
        if not overlapping:
            overlapping = True
            store.save(inner)
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", replace_with_overlap)

    store.save(outer)

    assert len(set(temporary_paths)) == 2
    assert store.load() == outer
    assert not list(tmp_path.glob(".configuration.json.*.tmp"))


def test_runtime_configuration_store_migrates_version_1_codex_settings(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    allowed = tmp_path / "allowed"
    codex_home.mkdir()
    allowed.mkdir()
    path = tmp_path / "configuration.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "profile": "codex",
                "codex_home": str(codex_home),
                "provider_id": "codex-local",
                "network_deny_enforced": True,
                "allowed_path_roots": [str(allowed)],
            }
        ),
        encoding="utf-8",
    )
    store = RuntimeConfigurationStore(path)

    migrated = store.load_or_create(RuntimeConfiguration())

    assert migrated.providers == (
        ProviderConfiguration(
            provider_id="codex-local",
            kind="codex",
            codex_home=codex_home,
            network_deny_enforced=True,
        ),
    )
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["version"] == 5
    assert set(persisted) == {
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


def test_runtime_configuration_store_migrates_version_2_provider_settings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "configuration.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "providers": [
                    {
                        "provider_id": "fake-local",
                        "kind": "fake",
                        "codex_home": None,
                        "config_overrides": [],
                        "network_deny_enforced": False,
                    }
                ],
                "allowed_path_roots": [],
            }
        ),
        encoding="utf-8",
    )

    configuration = RuntimeConfigurationStore(path).load_or_create(RuntimeConfiguration())

    assert configuration.providers == (ProviderConfiguration(provider_id="fake-local"),)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["version"] == 5
    assert persisted["claude_runtime_mode"] == "native"


def test_runtime_configuration_store_migrates_version_3_claude_runtime_defaults(
    tmp_path: Path,
) -> None:
    path = tmp_path / "configuration.json"
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "providers": [
                    {
                        "provider_id": "fake-local",
                        "kind": "fake",
                        "codex_home": None,
                        "config_overrides": [],
                        "claude_config_dir": None,
                        "claude_cli_path": None,
                        "model_ids": [],
                        "network_deny_enforced": False,
                    }
                ],
                "allowed_path_roots": [],
            }
        ),
        encoding="utf-8",
    )

    configuration = RuntimeConfigurationStore(path).load_or_create(RuntimeConfiguration())

    assert configuration.claude_runtime_mode == "native"
    assert configuration.claude_opencodex_base_url == "http://127.0.0.1:10100"
    assert configuration.claude_opencodex_auth_token_env == "ANTHROPIC_AUTH_TOKEN"
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 5


def test_runtime_configuration_store_migrates_version_4_coordinator_defaults(
    tmp_path: Path,
) -> None:
    path = tmp_path / "configuration.json"
    path.write_text(
        json.dumps(
            {
                "version": 4,
                "providers": [ProviderConfiguration().to_payload()],
                "allowed_path_roots": [],
                "claude_runtime_mode": "native",
                "claude_opencodex_base_url": "http://127.0.0.1:10100",
                "claude_opencodex_auth_token_env": "ANTHROPIC_AUTH_TOKEN",
            }
        ),
        encoding="utf-8",
    )

    configuration = RuntimeConfigurationStore(path).load_or_create(RuntimeConfiguration())

    assert configuration.coordinator_model == "pixel/gpt-5.6-luna"
    assert configuration.coordinator_reasoning_effort == "medium"
    assert configuration.coordinator_base_url == "http://127.0.0.1:10100/v1"
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 5


def test_runtime_configuration_migrates_legacy_fake_profile_to_fake_provider() -> None:
    migrated = RuntimeConfiguration.from_payload(
        {
            "version": 1,
            "profile": "fake",
            "codex_home": None,
            "provider_id": "unused-legacy-id",
            "network_deny_enforced": False,
            "allowed_path_roots": [],
        }
    )

    assert migrated.providers == (ProviderConfiguration(),)


def test_provider_configuration_accepts_safe_codex_overrides(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    configuration = ProviderConfiguration(
        provider_id="deepseek",
        kind="codex",
        codex_home=codex_home,
        config_overrides=(
            'model_provider="deepseek"',
            'model_providers.deepseek.name="DeepSeek API"',
            'model_providers.deepseek.base_url="https://api.deepseek.com/v1"',
            'model_providers.deepseek.env_key="DEEPSEEK_API_KEY"',
            'model_providers.deepseek.wire_api="responses"',
            "model_providers.deepseek.requires_openai_auth=false",
        ),
    )

    assert len(configuration.config_overrides) == 6


def test_provider_configuration_rejects_duplicate_ids_and_unsafe_overrides(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    with pytest.raises(ValueError, match="provider ids must be unique"):
        RuntimeConfiguration(
            providers=(
                ProviderConfiguration(provider_id="duplicate"),
                ProviderConfiguration(provider_id="duplicate"),
            )
        )
    unsafe_overrides = (
        'api_token="plaintext"',
        'api_key="plaintext"',
        'model_providers.deepseek.http_headers={Authorization="Bearer plaintext"}',
        'model_providers.deepseek.base_url="https://api.example/v1?key=plaintext"',
        'model_providers.deepseek.env_key="plaintext-secret"',
    )
    for override in unsafe_overrides:
        with pytest.raises(ValueError):
            ProviderConfiguration(
                provider_id="codex",
                kind="codex",
                codex_home=codex_home,
                config_overrides=(override,),
            )


def test_provider_configuration_accepts_claude_sdk_settings(tmp_path: Path) -> None:
    claude_config = tmp_path / "claude-config"
    claude_cli = tmp_path / "claude.exe"
    claude_config.mkdir()
    claude_cli.write_text("placeholder", encoding="utf-8")

    provider = ProviderConfiguration(
        provider_id="claude-local",
        kind="claude",
        claude_config_dir=claude_config,
        claude_cli_path=claude_cli,
        model_ids=("claude-sonnet-4-5", "claude-opus-4-5"),
        network_deny_enforced=True,
    )
    store = RuntimeConfigurationStore(tmp_path / "configuration.json")
    store.save(RuntimeConfiguration(providers=(provider,)))

    restored = store.load().providers[0]
    assert restored == provider
    assert restored.to_profile_payload()["model_ids"] == (
        "claude-sonnet-4-5",
        "claude-opus-4-5",
    )


def test_provider_configuration_rejects_empty_or_duplicate_claude_models(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="at least one Claude model"):
        ProviderConfiguration(provider_id="claude", kind="claude")
    with pytest.raises(ValueError, match="unique"):
        ProviderConfiguration(
            provider_id="claude",
            kind="claude",
            model_ids=("claude-sonnet-4-5", "claude-sonnet-4-5"),
        )


def test_claude_runtime_configuration_validates_and_round_trips() -> None:
    configuration = RuntimeConfiguration(
        claude_runtime_mode="opencodex",
        claude_opencodex_base_url="http://127.0.0.1:10100/",
        claude_opencodex_auth_token_env="OPENCODEX_TOKEN",
    )

    restored = RuntimeConfiguration.from_payload(configuration.to_payload())

    assert restored == RuntimeConfiguration(
        claude_runtime_mode="opencodex",
        claude_opencodex_base_url="http://127.0.0.1:10100",
        claude_opencodex_auth_token_env="OPENCODEX_TOKEN",
    )
    with pytest.raises(ValueError, match=r"HTTP\(S\) URL"):
        RuntimeConfiguration(claude_opencodex_base_url="file:///tmp/claude")
    with pytest.raises(ValueError, match="environment variable is invalid"):
        RuntimeConfiguration(claude_opencodex_auth_token_env="not-safe")


def test_coordinator_runtime_configuration_validates_and_round_trips() -> None:
    configuration = RuntimeConfiguration(
        coordinator_model="pixel/gpt-5.6-luna ",
        coordinator_reasoning_effort="xhigh",
        coordinator_api_key_env="OPENCODEX_TOKEN",
        coordinator_base_url="http://127.0.0.1:10100/v1/",
        coordinator_max_decision_steps=32,
        coordinator_wait_timeout_ms=250,
    )

    assert RuntimeConfiguration.from_payload(configuration.to_payload()) == configuration
    assert configuration.coordinator_model == "pixel/gpt-5.6-luna"
    assert configuration.coordinator_base_url == "http://127.0.0.1:10100/v1"
    with pytest.raises(ValueError, match="Coordinator API key"):
        RuntimeConfiguration(coordinator_api_key_env="not-safe")
    with pytest.raises(ValueError, match="max decision steps"):
        RuntimeConfiguration(coordinator_max_decision_steps=0)


def test_control_plane_state_path_preserves_one_legacy_history(tmp_path: Path) -> None:
    state_directory = tmp_path / ".data" / "multi-agent-v3"
    state_directory.mkdir(parents=True)
    legacy_state = state_directory / "control-plane-codex.jsonl"
    legacy_state.write_text("existing history", encoding="utf-8")

    assert ManagementConfig(root=tmp_path).control_plane_state_path() == legacy_state.resolve()


def test_control_plane_state_path_rejects_ambiguous_histories(tmp_path: Path) -> None:
    state_directory = tmp_path / ".data" / "multi-agent-v3"
    state_directory.mkdir(parents=True)
    (state_directory / "control-plane-codex.jsonl").touch()
    (state_directory / "control-plane-fake.jsonl").touch()

    with pytest.raises(ValueError, match="multiple Control Plane state files"):
        ManagementConfig(root=tmp_path).control_plane_state_path()


def test_control_plane_command_reads_persisted_configuration_at_start(tmp_path: Path) -> None:
    command = control_plane_command(ManagementConfig(root=tmp_path))

    assert command[1:3] == ("-m", "aitools_service_manager.control_plane_host")
    assert "--configuration-path" in command
    assert "--workspace-root" not in command
    assert "--workspace-id" not in command
    assert "--allowed-path-root" not in command


def test_coordinator_command_reads_persisted_configuration_at_start(tmp_path: Path) -> None:
    config = ManagementConfig(root=tmp_path)
    command = coordinator_command(config)

    assert command[1:3] == ("-m", "aitools_service_manager.coordinator_host")
    assert command[0].endswith(
        str(Path("multi-agent-coordinator") / ".venv" / "Scripts" / "python.exe")
    )
    assert "--configuration-path" in command
    assert "--control-plane-url" in command


def test_coordinator_host_translates_runtime_configuration_to_host_arguments(
    tmp_path: Path,
) -> None:
    configuration_path = tmp_path / "configuration.json"
    RuntimeConfigurationStore(configuration_path).save(
        RuntimeConfiguration(
            coordinator_model="model/test",
            coordinator_reasoning_effort="high",
            coordinator_api_key_env="TEST_API_KEY",
            coordinator_base_url="https://models.example.test/v1",
            coordinator_max_decision_steps=24,
            coordinator_wait_timeout_ms=100,
        )
    )

    arguments = coordinator_arguments(
        configuration_path=configuration_path,
        state_path=tmp_path / "sessions.jsonl",
        control_plane_url="http://127.0.0.1:8116",
        host="127.0.0.1",
        port=8120,
    )

    assert arguments[0] == "misaka_coordinator_service.transport"
    assert arguments[arguments.index("--model") + 1] == "model/test"
    assert arguments[arguments.index("--reasoning-effort") + 1] == "high"
    assert arguments[arguments.index("--base-url") + 1] == "https://models.example.test/v1"
    assert arguments[arguments.index("--port") + 1] == "8120"


def test_ports_must_be_distinct(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ports must differ"):
        ManagementConfig(root=tmp_path, management_port=8016, control_plane_port=8016)

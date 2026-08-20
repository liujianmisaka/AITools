from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_ROOT = PROJECT_ROOT / "deploy" / "local"


def test_v3_compose_is_self_contained_and_loopback_only() -> None:
    compose = (DEPLOY_ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "name: multi-agent-v3" in compose
    assert "multi-agent-v2" not in compose
    assert "MULTI_AGENT_V2" not in compose
    assert "127.0.0.1:5432:5432" in compose
    assert "127.0.0.1:7233:7233" in compose
    assert "temporalio/server:1.31.2" in compose
    assert compose.count("temporalio/admin-tools:1.31.2") == 2


def test_v3_database_roles_are_isolated() -> None:
    script = (DEPLOY_ROOT / "postgres-init" / "001-create-roles-and-databases.sh").read_text(
        encoding="utf-8"
    )

    assert "CREATE ROLE temporal_runtime" in script
    assert "CREATE ROLE multi_agent_v3_app" in script
    assert "CREATE DATABASE temporal OWNER temporal_runtime" in script
    assert "CREATE DATABASE multi_agent_v3 OWNER multi_agent_v3_app" in script
    assert "GRANT CONNECT ON DATABASE multi_agent_v3 TO multi_agent_v3_app" in script
    assert "GRANT CONNECT ON DATABASE temporal TO multi_agent_v3_app" not in script
    assert "multi_agent_v2" not in script


def test_v3_environment_example_uses_v3_application_role() -> None:
    example = (DEPLOY_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "postgresql://multi_agent_v3_app:" in example
    assert "@127.0.0.1:5432/multi_agent_v3" in example
    assert "temporal_runtime" not in example
    assert "MULTI_AGENT_V2" not in example

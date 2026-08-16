from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml


def test_compose_only_exposes_persistence_on_loopback() -> None:
    compose_path = Path(__file__).parents[1] / "deploy" / "local" / "compose.yaml"
    document = cast(dict[str, object], yaml.safe_load(compose_path.read_text(encoding="utf-8")))
    services = cast(dict[str, dict[str, object]], document["services"])

    assert set(services) == {
        "postgresql",
        "temporal",
        "temporal-namespace",
        "temporal-schema",
    }
    assert services["postgresql"]["image"] == "postgres:16.15"
    assert services["temporal"]["image"] == "temporalio/server:1.31.2"
    assert services["temporal-schema"]["image"] == "temporalio/admin-tools:1.31.2"
    assert services["temporal-namespace"]["image"] == "temporalio/admin-tools:1.31.2"
    assert services["postgresql"]["ports"] == ["127.0.0.1:5432:5432"]
    assert services["temporal"]["ports"] == ["127.0.0.1:7233:7233"]


def test_database_roles_are_isolated_by_postgres_initialization() -> None:
    init_path = (
        Path(__file__).parents[1]
        / "deploy"
        / "local"
        / "postgres-init"
        / "001-create-roles-and-databases.sh"
    )

    script = init_path.read_text(encoding="utf-8")
    assert "CREATE ROLE temporal_runtime" in script
    assert "CREATE ROLE multi_agent_app" in script
    assert "CREATE DATABASE temporal OWNER temporal_runtime" in script
    assert "CREATE DATABASE multi_agent_v2 OWNER multi_agent_app" in script
    assert "REVOKE CONNECT ON DATABASE temporal FROM PUBLIC" in script
    assert "GRANT CONNECT ON DATABASE multi_agent_v2 TO multi_agent_app" in script
    assert "GRANT CONNECT ON DATABASE temporal TO multi_agent_app" not in script


def test_control_api_example_uses_only_control_database_role() -> None:
    env_path = Path(__file__).parents[1] / "deploy" / "local" / ".env.example"
    example = env_path.read_text(encoding="utf-8")

    assert "postgresql+asyncpg://multi_agent_app:" in example
    assert "postgresql+asyncpg://temporal_runtime:" not in example

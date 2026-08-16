from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from multi_agent_v2.packages.persistence import CURRENT_SCHEMA_REVISION


def test_application_schema_revision_matches_alembic_head() -> None:
    project_root = Path(__file__).parents[1]
    config = Config(project_root / "alembic.ini")
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_current_head() == CURRENT_SCHEMA_REVISION

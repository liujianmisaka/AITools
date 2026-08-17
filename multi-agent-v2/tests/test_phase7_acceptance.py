from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tools.phase7_acceptance import (
    REQUIRED_ENTRYPOINTS,
    AcceptanceFailure,
    declared_entrypoints,
    run_command,
)


def test_phase7_console_script_contract_is_complete() -> None:
    project_root = Path(__file__).parents[1]

    assert declared_entrypoints(project_root) == REQUIRED_ENTRYPOINTS


def test_phase7_subprocess_deadline_is_enforced(tmp_path: Path) -> None:
    with pytest.raises(AcceptanceFailure, match="exceeded"):
        run_command(
            (sys.executable, "-c", "import time; time.sleep(10)"),
            cwd=tmp_path,
            timeout_seconds=0.1,
        )

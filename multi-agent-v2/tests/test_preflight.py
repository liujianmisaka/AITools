from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from multi_agent_v2.apps.preflight import check_prerequisite


def test_preflight_reports_timeout_without_traceback() -> None:
    executable = Path("tool.exe")
    with (
        patch(
            "multi_agent_v2.apps.preflight._resolve_executable",
            return_value=executable,
        ),
        patch(
            "multi_agent_v2.apps.preflight.subprocess.run",
            side_effect=subprocess.TimeoutExpired("tool", 5),
        ),
    ):
        result = check_prerequisite("tool", ("--version",))

    assert not result.available
    assert result.executable == executable
    assert result.error == "TimeoutExpired"

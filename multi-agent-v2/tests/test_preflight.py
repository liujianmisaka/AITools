from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from multi_agent_v2.apps.preflight import check_prerequisite, resolve_executable


def test_preflight_reports_timeout_without_traceback() -> None:
    executable = Path("tool.exe")
    with (
        patch(
            "multi_agent_v2.apps.preflight.resolve_executable",
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


def test_preflight_finds_per_user_docker_desktop_install(tmp_path: Path) -> None:
    docker = (
        tmp_path
        / "AppData"
        / "Local"
        / "Programs"
        / "DockerDesktop"
        / "resources"
        / "bin"
        / "docker.exe"
    )
    docker.parent.mkdir(parents=True)
    docker.touch()

    with (
        patch("multi_agent_v2.apps.preflight.shutil.which", return_value=None),
        patch("multi_agent_v2.apps.preflight.Path.home", return_value=tmp_path),
    ):
        assert resolve_executable("docker") == docker

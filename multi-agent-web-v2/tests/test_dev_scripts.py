from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_START = _ROOT / "start-multi-agent-v2-dev.ps1"
_STOP = _ROOT / "stop-multi-agent-v2-dev.ps1"
_START_BASH = _ROOT / "start-multi-agent-v2-dev.sh"
_STOP_BASH = _ROOT / "stop-multi-agent-v2-dev.sh"


def _git_bash() -> str | None:
    git = shutil.which("git")
    candidates: list[Path] = []
    if git is not None:
        git_path = Path(git).resolve()
        candidates.extend(
            (
                git_path.parent / "bash.exe",
                git_path.parent.parent / "bin" / "bash.exe",
                git_path.parent.parent / "usr" / "bin" / "bash.exe",
            )
        )
    candidates.extend(
        (
            Path("C:/Program Files/Git/bin/bash.exe"),
            Path("C:/Program Files/Git/usr/bin/bash.exe"),
        )
    )
    return next((str(path) for path in candidates if path.is_file()), None)


def _powershells() -> list[str]:
    shells: list[str] = []
    for command in ("powershell.exe", "pwsh.exe", "pwsh"):
        executable = shutil.which(command)
        if executable is not None and executable not in shells:
            shells.append(executable)
    return shells


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell lifecycle is Windows-only")
def test_powershell_scripts_parse_without_errors() -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")

    command = (
        "$errors = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{_START}', "
        "[ref]$null, [ref]$errors); "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{_STOP}', "
        "[ref]$null, [ref]$errors); "
        "if ($errors.Count -gt 0) { $errors | Out-String | Write-Error; exit 1 }"
    )
    subprocess.run(
        [powershell, "-NoLogo", "-NoProfile", "-Command", command],
        check=True,
        timeout=20,
    )


def test_default_worktree_root_is_outside_the_registered_repository() -> None:
    script = _START.read_text(encoding="utf-8")

    assert "$repositoryParent = Split-Path -Parent $root" in script
    assert 'Join-Path $repositoryParent ".multi-agent-worktrees\\$WorkspaceId"' in script
    assert 'Join-Path $runRoot "worktrees"' not in script


def test_start_script_owns_local_infrastructure_and_applies_migrations() -> None:
    script = _START.read_text(encoding="utf-8")

    assert '"compose", "--project-name", $ComposeProjectName' in script
    assert '"up", "-d", "--wait", "--wait-timeout", "120",' in script
    assert '"postgresql", "temporal"' in script
    assert '"run", "--rm", "temporal-namespace"' in script
    assert '"alembic",' in script
    assert '"upgrade", "head"' in script
    assert 'Join-Path $runRoot "infrastructure-secrets.json"' in script
    assert "Get-Command npm.cmd -CommandType Application" in script
    assert "Assert-ManagedProcessesRunning -Entries $processes" in script
    assert 'Wait-HttpReady -Name "Control API" -Url "http://127.0.0.1:$CorePort/ready"' in script


def test_stop_script_uses_manifest_owned_compose_project() -> None:
    script = _STOP.read_text(encoding="utf-8")

    assert '"/PID", "$ProcessId", "/T", "/F"' in script
    assert "$manifest.infrastructure.composeProjectName" in script
    assert "$manifest.infrastructure.composeFile" in script
    assert "$manifest.infrastructure.secretsFile" in script
    assert "down --remove-orphans" in script


def test_git_bash_wrappers_parse_when_bash_is_available() -> None:
    bash = _git_bash() if sys.platform == "win32" else shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")
    subprocess.run(
        [bash, "-n", str(_START_BASH), str(_STOP_BASH)],
        check=True,
        timeout=20,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell lifecycle is Windows-only")
@pytest.mark.parametrize("powershell", _powershells())
def test_stop_script_terminates_only_the_manifest_process(
    tmp_path: Path,
    powershell: str,
) -> None:
    sleeper = subprocess.Popen(
        [powershell, "-NoLogo", "-NoProfile", "-Command", "Start-Sleep -Seconds 60"]
    )
    try:
        query = f"(Get-Process -Id {sleeper.pid}).StartTime.ToUniversalTime().ToString('O')"
        started_at = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-Command", query],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        manifest = tmp_path / "processes.json"
        manifest.write_text(
            json.dumps(
                {
                    "version": 1,
                    "processes": [
                        {
                            "role": "fake-worker",
                            "pid": sleeper.pid,
                            "startTimeUtc": started_at,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        subprocess.run(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(_STOP),
                "-ManifestPath",
                str(manifest),
            ],
            check=True,
            timeout=20,
        )

        deadline = time.monotonic() + 5
        while sleeper.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert sleeper.poll() is not None
        assert not manifest.exists()
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)

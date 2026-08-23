from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONFIGURE_SCRIPT = REPOSITORY_ROOT / "configure-multi-agent-mcp.ps1"


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is required to test the Windows configuration script")
    return executable


def _fake_codex(tmp_path: Path) -> Path:
    implementation = tmp_path / "fake_codex.py"
    implementation.write_text(
        """\
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

arguments = sys.argv[1:]
with Path(os.environ["FAKE_CODEX_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(arguments) + "\\n")

if arguments[:2] == ["mcp", "add"]:
    print("Added global MCP server.")
elif arguments[:2] == ["mcp", "get"]:
    print(json.dumps({
        "name": arguments[2],
        "enabled": True,
        "transport": {
            "type": "stdio",
            "command": os.environ["FAKE_CODEX_PYTHON"],
            "args": json.loads(os.environ["FAKE_CODEX_GATEWAY_ARGS"]),
            "env": {
                "PYTHONPATH": os.environ["FAKE_CODEX_GATEWAY_SOURCE"],
                "PYTHONUTF8": "1",
            },
            "env_vars": [],
            "cwd": None,
        },
    }))
else:
    print(f"Unexpected arguments: {arguments!r}", file=sys.stderr)
    raise SystemExit(2)
""",
        encoding="utf-8",
    )
    launcher = tmp_path / "codex.cmd"
    launcher.write_text(
        f'@echo off\n"{sys.executable}" "%~dp0fake_codex.py" %*\n',
        encoding="ascii",
    )
    return launcher


def test_script_registers_and_verifies_repository_gateway(tmp_path: Path) -> None:
    fake_codex = _fake_codex(tmp_path)
    log_path = tmp_path / "codex-calls.jsonl"
    python_path = (
        REPOSITORY_ROOT / "multi-agent-v3" / ".venv" / "Scripts" / "python.exe"
    ).resolve()
    gateway_source = (REPOSITORY_ROOT / "multi-agent-mcp" / "src").resolve()
    gateway_arguments = [
        "-m",
        "misaka_mcp_gateway",
        "--control-plane-url",
        "http://127.0.0.1:8016",
        "--sandbox",
        "workspace_write",
        "--network-policy",
        "deny",
        "--timeout-seconds",
        "30",
    ]
    environment = os.environ.copy()
    environment.update(
        {
            "FAKE_CODEX_LOG": str(log_path),
            "FAKE_CODEX_PYTHON": str(python_path),
            "FAKE_CODEX_GATEWAY_SOURCE": str(gateway_source),
            "FAKE_CODEX_GATEWAY_ARGS": json.dumps(gateway_arguments),
        }
    )

    result = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(CONFIGURE_SCRIPT),
            "-CodexCommand",
            str(fake_codex),
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0, result.stderr
    assert "Configured Codex MCP server 'multi_agent_v3'." in result.stdout
    calls = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert calls == [
        [
            "mcp",
            "add",
            "multi_agent_v3",
            "--env",
            f"PYTHONPATH={gateway_source}",
            "--env",
            "PYTHONUTF8=1",
            "--",
            str(python_path),
            *gateway_arguments,
        ],
        ["mcp", "get", "multi_agent_v3", "--json"],
    ]


def test_script_rejects_non_http_control_plane_url(tmp_path: Path) -> None:
    fake_codex = _fake_codex(tmp_path)
    environment = os.environ.copy()
    environment["FAKE_CODEX_LOG"] = str(tmp_path / "unused.jsonl")

    result = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(CONFIGURE_SCRIPT),
            "-CodexCommand",
            str(fake_codex),
            "-ControlPlaneUrl",
            "file:///tmp/control-plane",
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode != 0
    assert "ControlPlaneUrl must be an absolute HTTP(S) base URL" in result.stderr

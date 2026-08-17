from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

REQUIRED_ENTRYPOINTS = (
    "multi-agent-v2-agent-worker",
    "multi-agent-v2-api",
    "multi-agent-v2-catalog-refresher",
    "multi-agent-v2-dispatcher",
    "multi-agent-v2-event-catalog",
    "multi-agent-v2-orchestration-worker",
    "multi-agent-v2-preflight",
)


class AcceptanceFailure(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    elapsed_seconds: float


def declared_entrypoints(project_root: Path) -> tuple[str, ...]:
    document: object = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise AcceptanceFailure("pyproject.toml must contain a table")
    root = cast(dict[object, object], document)
    project = root.get("project")
    if not isinstance(project, dict):
        raise AcceptanceFailure("pyproject.toml has no project table")
    project_table = cast(dict[object, object], project)
    scripts = project_table.get("scripts")
    if not isinstance(scripts, dict):
        raise AcceptanceFailure("pyproject.toml has no project.scripts table")
    script_table = cast(dict[object, object], scripts)
    return tuple(sorted(name for name in script_table if isinstance(name, str)))


def run_command(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    environment: dict[str, str] | None = None,
    timeout_seconds: float = 20,
) -> CommandResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=environment,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise AcceptanceFailure(f"command exceeded {timeout_seconds:g}s: {argv[0]}") from exc
    return CommandResult(
        argv=argv,
        returncode=completed.returncode,
        stdout=completed.stdout[-8000:],
        stderr=completed.stderr[-8000:],
        elapsed_seconds=time.monotonic() - started,
    )


def verify_installed_entrypoints(project_root: Path) -> dict[str, object]:
    declared = declared_entrypoints(project_root)
    if declared != REQUIRED_ENTRYPOINTS:
        raise AcceptanceFailure(f"console script set differs from the Phase 7 contract: {declared}")
    with tempfile.TemporaryDirectory(prefix="multi-agent-v2-phase7-") as temporary:
        root = Path(temporary)
        uv = shutil.which("uv")
        if uv is None:
            raise AcceptanceFailure("uv is required for installed-entry acceptance")
        build_environment = dict(os.environ)
        build_environment["UV_CACHE_DIR"] = str(root / "uv-cache")
        wheels = root / "wheels"
        wheels.mkdir()
        build = run_command(
            (
                uv,
                "build",
                "--wheel",
                "--out-dir",
                str(wheels),
            ),
            cwd=project_root,
            environment=build_environment,
            timeout_seconds=120,
        )
        _require_success("wheel build", build)
        wheel_files = tuple(wheels.glob("*.whl"))
        if len(wheel_files) != 1:
            raise AcceptanceFailure(f"expected one built wheel, found {len(wheel_files)}")

        environment_root = root / "installed"
        create_environment = run_command(
            (
                uv,
                "venv",
                "--system-site-packages",
                "--python",
                sys.executable,
                str(environment_root),
            ),
            cwd=project_root,
            environment=build_environment,
            timeout_seconds=120,
        )
        _require_success("acceptance environment creation", create_environment)
        python = environment_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        scripts = environment_root / ("Scripts" if os.name == "nt" else "bin")
        install = run_command(
            (
                uv,
                "pip",
                "install",
                "--python",
                str(python),
                str(wheel_files[0]),
            ),
            cwd=project_root,
            environment=build_environment,
            timeout_seconds=120,
        )
        _require_success("wheel install", install)

        event_catalog = run_command(
            (
                str(_entrypoint(scripts, "multi-agent-v2-event-catalog")),
                "--check",
                str(project_root / "docs" / "event-catalog.json"),
            ),
            cwd=root,
        )
        _require_success("installed event catalog", event_catalog)

        preflight = run_command(
            (str(_entrypoint(scripts, "multi-agent-v2-preflight")),),
            cwd=root,
        )
        _require_success("installed preflight", preflight)

        failed_entries: list[str] = []
        invalid_database_environment = dict(os.environ)
        invalid_database_environment["MULTI_AGENT_V2_DATABASE_URL"] = "invalid-database-url"
        for name in (
            "multi-agent-v2-agent-worker",
            "multi-agent-v2-catalog-refresher",
            "multi-agent-v2-dispatcher",
            "multi-agent-v2-orchestration-worker",
        ):
            result = run_command(
                (str(_entrypoint(scripts, name)),),
                cwd=root,
                environment=invalid_database_environment,
            )
            _require_failure(name, result)
            failed_entries.append(name)

        invalid_api_environment = dict(os.environ)
        invalid_api_environment["MULTI_AGENT_V2_CONTROL_HOST"] = "0.0.0.0"
        api = run_command(
            (str(_entrypoint(scripts, "multi-agent-v2-api")),),
            cwd=root,
            environment=invalid_api_environment,
        )
        _require_failure("multi-agent-v2-api", api)
        failed_entries.append("multi-agent-v2-api")

    return {
        "declaredEntrypoints": list(declared),
        "wheel": wheel_files[0].name,
        "successfulEntrypoints": [
            "multi-agent-v2-event-catalog",
            "multi-agent-v2-preflight",
        ],
        "diagnosticFailureEntrypoints": sorted(failed_entries),
    }


def _entrypoint(scripts: Path, name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    path = scripts / f"{name}{suffix}"
    if not path.is_file():
        raise AcceptanceFailure(f"installed console script is missing: {name}")
    return path


def _require_success(label: str, result: CommandResult) -> None:
    if result.returncode != 0:
        raise AcceptanceFailure(
            f"{label} failed with {result.returncode}: {result.stderr or result.stdout}"
        )


def _require_failure(label: str, result: CommandResult) -> None:
    if result.returncode == 0:
        raise AcceptanceFailure(f"{label} unexpectedly succeeded with invalid configuration")
    output = f"{result.stdout}\n{result.stderr}".strip()
    if not output:
        raise AcceptanceFailure(f"{label} failed without diagnostic output")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run bounded Phase 7 installed-entry acceptance")
    parser.add_argument(
        "--project",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    arguments = parser.parse_args()
    result = verify_installed_entrypoints(arguments.project.resolve())
    for key, value in result.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()

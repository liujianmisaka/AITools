from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Prerequisite:
    name: str
    executable: Path | None
    version: str | None
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.executable is not None and self.version is not None and self.error is None


def _resolve_executable(name: str) -> Path | None:
    from_path = shutil.which(name)
    if from_path is not None:
        return Path(from_path)

    if name == "psql":
        scoop_apps = Path.home() / "scoop" / "apps"
        preferred = scoop_apps / "postgresql16" / "current" / "bin" / "psql.exe"
        if preferred.is_file():
            return preferred
        candidates = sorted(
            scoop_apps.glob("postgresql*/current/bin/psql.exe"),
            reverse=True,
        )
        if candidates:
            return candidates[0]
    return None


def check_prerequisite(name: str, version_args: tuple[str, ...]) -> Prerequisite:
    executable = _resolve_executable(name)
    if executable is None:
        return Prerequisite(name=name, executable=None, version=None)

    try:
        completed = subprocess.run(
            [str(executable), *version_args],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Prerequisite(
            name=name,
            executable=executable,
            version=None,
            error=type(exc).__name__,
        )
    output = (completed.stdout or completed.stderr).strip()
    version = output.splitlines()[0] if completed.returncode == 0 and output else None
    return Prerequisite(name=name, executable=executable, version=version)


def inspect_prerequisites() -> tuple[Prerequisite, ...]:
    return (
        Prerequisite(
            name="python",
            executable=Path(sys.executable),
            version=sys.version.split()[0],
        ),
        check_prerequisite("uv", ("--version",)),
        check_prerequisite("temporal", ("--version",)),
        check_prerequisite("psql", ("--version",)),
        check_prerequisite("docker", ("compose", "version")),
    )


def run() -> None:
    prerequisites = inspect_prerequisites()
    for prerequisite in prerequisites:
        state = "ok" if prerequisite.available else "missing"
        location = str(prerequisite.executable) if prerequisite.executable else "-"
        version = prerequisite.version or "-"
        error = prerequisite.error or "-"
        print(f"{prerequisite.name}: {state} | {version} | {location} | {error}")

    if not all(prerequisite.available for prerequisite in prerequisites):
        raise SystemExit(1)

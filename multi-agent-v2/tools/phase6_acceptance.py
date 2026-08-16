from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{24,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
_TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}
_FRONTEND_GENERATED_DIRECTORIES = {
    ("multi-agent-web-v2", "frontend", "dist"),
    ("multi-agent-web-v2", "frontend", "node_modules"),
}
_FRONTEND_GENERATED_FILES = {
    ("multi-agent-web-v2", "frontend", "tsconfig.app.tsbuildinfo"),
    ("multi-agent-web-v2", "frontend", "tsconfig.node.tsbuildinfo"),
}


def _git_files(repository: Path, *arguments: str) -> tuple[Path, ...]:
    result = subprocess.run(
        ["git", "ls-files", "-z", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return tuple(
        path
        for value in result.stdout.split(b"\0")
        if value and (path := repository / value.decode()).is_file()
    )


def _is_generated_untracked(path: Path, repository: Path) -> bool:
    parts = path.relative_to(repository).parts
    return parts in _FRONTEND_GENERATED_FILES or any(
        parts[: len(directory)] == directory for directory in _FRONTEND_GENERATED_DIRECTORIES
    )


def repository_files(repository: Path) -> tuple[Path, ...]:
    tracked = _git_files(repository, "--cached")
    untracked = tuple(
        path
        for path in _git_files(repository, "--others", "--exclude-standard")
        if not _is_generated_untracked(path, repository)
    )
    return tuple(dict.fromkeys((*tracked, *untracked)))


def verify(repository: Path) -> dict[str, object]:
    files = repository_files(repository)
    legacy = [
        path
        for path in files
        if path.relative_to(repository).parts[0] in {"multi-agent", "multi-agent-web"}
        or path.name
        in {
            "start-multi-agent-dev.ps1",
            "start-multi-agent-dev.sh",
            "stop-multi-agent-dev.ps1",
            "stop-multi-agent-dev.sh",
        }
    ]
    findings: list[str] = []
    for path in files:
        if path.suffix.lower() not in _TEXT_SUFFIXES or path.name.endswith("lock.json"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(str(path.relative_to(repository)))
                break

    start_script = (repository / "start-multi-agent-v2-dev.ps1").read_text(encoding="utf-8")
    network_ok = (
        '$env:MULTI_AGENT_V2_CONTROL_HOST = "127.0.0.1"' in start_script
        and '$env:MULTI_AGENT_WEB_V2_INTERNAL_HOST = "127.0.0.1"' in start_script
        and '--host", "127.0.0.1"' in start_script
    )
    if legacy or findings or not network_ok:
        raise RuntimeError(
            json.dumps(
                {
                    "legacyPaths": [str(path.relative_to(repository)) for path in legacy],
                    "secretFindings": findings,
                    "networkBoundary": network_ok,
                },
                ensure_ascii=False,
            )
        )
    return {
        "repositoryFilesScanned": len(files),
        "secretFindings": 0,
        "legacyRuntimePaths": 0,
        "networkBoundary": "verified",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.repository.resolve()), ensure_ascii=False))


if __name__ == "__main__":
    main()

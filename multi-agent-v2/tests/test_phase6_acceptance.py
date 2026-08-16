from __future__ import annotations

import subprocess
from pathlib import Path

from tools.phase6_acceptance import repository_files, verify


def test_repository_has_no_v1_runtime_or_embedded_secret() -> None:
    repository = Path(__file__).resolve().parents[2]

    result = verify(repository)

    assert result["secretFindings"] == 0
    assert result["legacyRuntimePaths"] == 0
    assert result["networkBoundary"] == "verified"


def test_repository_scan_includes_untracked_files_but_not_ignored_files(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / "tracked.txt").write_text("tracked", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".gitignore", "tracked.txt"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "untracked.txt").write_text("untracked", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("ignored", encoding="utf-8")
    generated = tmp_path / "multi-agent-web-v2" / "frontend" / "node_modules" / "dependency.js"
    generated.parent.mkdir(parents=True)
    generated.write_text("generated", encoding="utf-8")

    files = {path.relative_to(tmp_path).as_posix() for path in repository_files(tmp_path)}

    assert files == {".gitignore", "tracked.txt", "untracked.txt"}

    subprocess.run(
        ["git", "add", "multi-agent-web-v2/frontend/node_modules/dependency.js"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    files = {path.relative_to(tmp_path).as_posix() for path in repository_files(tmp_path)}

    assert files == {
        ".gitignore",
        "multi-agent-web-v2/frontend/node_modules/dependency.js",
        "tracked.txt",
        "untracked.txt",
    }

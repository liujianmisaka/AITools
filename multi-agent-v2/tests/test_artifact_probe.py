from __future__ import annotations

from pathlib import Path

from multi_agent_v2.packages.artifacts import ArtifactRootProbe


async def test_artifact_probe_creates_root_and_removes_probe_file(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"

    await ArtifactRootProbe(root).check()

    assert root.is_dir()
    assert list(root.iterdir()) == []

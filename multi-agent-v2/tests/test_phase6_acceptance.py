from __future__ import annotations

from pathlib import Path

from tools.phase6_acceptance import verify


def test_repository_has_no_v1_runtime_or_embedded_secret() -> None:
    repository = Path(__file__).resolve().parents[2]

    result = verify(repository)

    assert result["secretFindings"] == 0
    assert result["legacyRuntimePaths"] == 0
    assert result["networkBoundary"] == "verified"

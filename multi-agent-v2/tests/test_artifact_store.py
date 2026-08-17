from __future__ import annotations

from pathlib import Path

import pytest

from multi_agent_v2.packages.artifacts import (
    ArtifactStorageError,
    LocalArtifactStore,
    redact_json,
)
from multi_agent_v2.packages.persistence import ArtifactRegistration


@pytest.mark.asyncio
async def test_artifact_store_writes_atomically_and_verifies_content(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")

    artifact = await store.put_bytes(
        b"durable output",
        media_type="text/plain",
        kind="agent-output",
    )

    assert not Path(artifact.relative_path).is_absolute()
    assert await store.read_verified(artifact) == b"durable output"
    assert not tuple(store.root.rglob("*.tmp"))


@pytest.mark.asyncio
async def test_artifact_store_rejects_tampered_content(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    artifact = await store.put_bytes(
        b"original",
        media_type="application/octet-stream",
        kind="test",
    )
    (store.root / artifact.relative_path).write_bytes(b"tampered")

    with pytest.raises(ArtifactStorageError, match=r"size|hash"):
        await store.read_verified(artifact)


@pytest.mark.asyncio
async def test_artifact_store_enforces_size_limit(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts", maximum_bytes=4)

    with pytest.raises(ArtifactStorageError, match="maximum"):
        await store.put_bytes(
            b"12345",
            media_type="text/plain",
            kind="test",
        )


@pytest.mark.asyncio
async def test_artifact_store_reuses_a_deterministic_id_only_for_identical_content(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    artifact_id = "afcab9fb-51ce-464a-a05f-ef23dbb788c2"

    first = await store.put_bytes(
        b"same",
        media_type="text/plain",
        kind="test",
        artifact_id=artifact_id,
    )
    second = await store.put_bytes(
        b"same",
        media_type="text/plain",
        kind="test",
        artifact_id=artifact_id,
    )

    assert first == second
    with pytest.raises(ArtifactStorageError, match="different content"):
        await store.put_bytes(
            b"different",
            media_type="text/plain",
            kind="test",
            artifact_id=artifact_id,
        )


def test_redaction_removes_nested_credential_values() -> None:
    assert redact_json(
        {
            "safe": "value",
            "apiKey": "secret",
            "nested": {
                "refresh_token": "secret",
                "items": [{"passwordFile": "secret"}],
            },
        }
    ) == {
        "safe": "value",
        "apiKey": "<redacted>",
        "nested": {
            "refresh_token": "<redacted>",
            "items": [{"passwordFile": "<redacted>"}],
        },
    }


def test_artifact_metadata_requires_a_lowercase_hex_sha256() -> None:
    with pytest.raises(ValueError, match="hexadecimal"):
        ArtifactRegistration(
            artifact_id="afcab9fb-51ce-464a-a05f-ef23dbb788c2",
            execution_id=None,
            relative_path="aa/artifact",
            sha256="z" * 64,
            size_bytes=1,
            media_type="text/plain",
            kind="test",
        )

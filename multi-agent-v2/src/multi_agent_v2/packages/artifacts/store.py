from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from multi_agent_v2.packages.domain.json_types import JsonObject, JsonValue


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    artifact_id: str
    relative_path: str
    sha256: str
    size_bytes: int
    media_type: str
    kind: str


class ArtifactStorageError(RuntimeError):
    pass


class LocalArtifactStore:
    def __init__(self, root: Path, *, maximum_bytes: int = 128 * 1024 * 1024) -> None:
        if maximum_bytes <= 0:
            raise ValueError("artifact maximum size must be positive")
        self._root = root.resolve()
        self._maximum_bytes = maximum_bytes

    @property
    def root(self) -> Path:
        return self._root

    async def put_bytes(
        self,
        content: bytes,
        *,
        media_type: str,
        kind: str,
        artifact_id: str | None = None,
    ) -> StoredArtifact:
        return await asyncio.to_thread(
            self._put_bytes_sync,
            content,
            media_type,
            kind,
            artifact_id,
        )

    async def put_json(
        self,
        value: JsonValue,
        *,
        kind: str,
        artifact_id: str | None = None,
    ) -> StoredArtifact:
        content = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return await self.put_bytes(
            content,
            media_type="application/json",
            kind=kind,
            artifact_id=artifact_id,
        )

    async def read_verified(self, artifact: StoredArtifact) -> bytes:
        return await asyncio.to_thread(self._read_verified_sync, artifact)

    async def delete(self, artifact: StoredArtifact) -> None:
        await asyncio.to_thread(self._artifact_path(artifact.relative_path).unlink, True)

    def _put_bytes_sync(
        self,
        content: bytes,
        media_type: str,
        kind: str,
        requested_artifact_id: str | None,
    ) -> StoredArtifact:
        if len(content) > self._maximum_bytes:
            raise ArtifactStorageError("artifact exceeds the configured maximum size")
        if not media_type.strip() or not kind.strip():
            raise ValueError("artifact media type and kind must not be blank")
        self._root.mkdir(parents=True, exist_ok=True)
        artifact_id = _artifact_id(requested_artifact_id)
        digest = hashlib.sha256(content).hexdigest()
        relative = f"{artifact_id[:2]}/{artifact_id}.artifact"
        target = self._artifact_path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != content:
                raise ArtifactStorageError("artifact ID was reused with different content")
            return StoredArtifact(
                artifact_id=artifact_id,
                relative_path=relative,
                sha256=digest,
                size_bytes=len(content),
                media_type=media_type,
                kind=kind,
            )
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{artifact_id}-",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary = Path(raw_temporary)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            _fsync_directory(target.parent)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return StoredArtifact(
            artifact_id=artifact_id,
            relative_path=relative,
            sha256=digest,
            size_bytes=len(content),
            media_type=media_type,
            kind=kind,
        )

    def _read_verified_sync(self, artifact: StoredArtifact) -> bytes:
        content = self._artifact_path(artifact.relative_path).read_bytes()
        if len(content) != artifact.size_bytes:
            raise ArtifactStorageError("artifact size does not match metadata")
        if hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise ArtifactStorageError("artifact hash does not match metadata")
        return content

    def _artifact_path(self, relative_path: str) -> Path:
        if (
            not relative_path
            or "\\" in relative_path
            or Path(relative_path).is_absolute()
            or ".." in Path(relative_path).parts
        ):
            raise ArtifactStorageError("artifact path is not a safe relative path")
        candidate = (self._root / Path(relative_path)).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as exc:
            raise ArtifactStorageError("artifact path escapes the configured root") from exc
        return candidate


_REDACTED = "<redacted>"
_SENSITIVE_PARTS = ("key", "secret", "token", "password")


def redact_json(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        redacted: JsonObject = {}
        for key, child in value.items():
            compact = "".join(character for character in key.lower() if character.isalnum())
            redacted[key] = (
                _REDACTED
                if any(part in compact for part in _SENSITIVE_PARTS)
                else redact_json(child)
            )
        return redacted
    if isinstance(value, list):
        return [redact_json(child) for child in value]
    return value


def _artifact_id(requested: str | None) -> str:
    if requested is None:
        return str(uuid4())
    try:
        parsed = UUID(requested)
    except ValueError as exc:
        raise ValueError("artifact ID must be a canonical UUID") from exc
    canonical = str(parsed)
    if requested != canonical:
        raise ValueError("artifact ID must be a canonical UUID")
    return canonical


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from misaka_interaction_contracts import PrincipalRef
from misaka_invocation_contracts import ArtifactRef
from misaka_kernel import HostContext
from misaka_kernel.lifecycle import AsyncDisposer
from misaka_kernel_contracts import (
    ModuleId,
    ModuleManifest,
    ServiceKey,
    ServiceProvision,
)
from misaka_resource_contracts import ResourceLease

ARTIFACT_STORE_SERVICE = ServiceKey("capability.artifact.store")
ARTIFACT_MODULE_ID = ModuleId("capability.artifact.memory")


class ArtifactClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    SECRET = "secret"


@dataclass(frozen=True, slots=True)
class ArtifactWrite:
    artifact_key: str
    content: bytes
    media_type: str
    owner: PrincipalRef
    lease: ResourceLease
    classification: ArtifactClassification = ArtifactClassification.INTERNAL
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.artifact_key.strip() or not self.media_type.strip():
            raise ValueError("artifact key and media type must not be empty")
        if self.lease.resource.resource_type != "artifact":
            raise ValueError("artifact writes require an artifact resource lease")
        if not self.lease.active_at():
            raise ValueError("artifact writes require an active resource lease")
        if self.lease.resource.resource_id != self.artifact_key:
            raise ValueError("artifact lease must target artifact_key")
        if self.lease.owner != self.owner:
            raise ValueError("artifact owner must match lease owner")
        _reject_sensitive_metadata(self.metadata)


@dataclass(frozen=True, slots=True)
class ArtifactCommit:
    artifact: ArtifactRef
    artifact_key: str
    owner: PrincipalRef
    lease_epoch: int
    committed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.artifact_key.strip() or self.lease_epoch < 1:
            raise ValueError("artifact commit requires key and positive lease epoch")
        if self.committed_at.tzinfo is None or self.committed_at.utcoffset() is None:
            raise ValueError("artifact commit timestamp must be timezone-aware")


class ArtifactStore(Protocol):
    async def put(self, write: ArtifactWrite) -> ArtifactCommit: ...

    async def get(self, artifact: ArtifactRef) -> bytes: ...


class MemoryArtifactStore:
    def __init__(self) -> None:
        self._content: dict[str, bytes] = {}
        self._commits: dict[tuple[str, int], ArtifactCommit] = {}
        self._fingerprints: dict[tuple[str, int], str] = {}
        self._last_epoch: dict[str, int] = {}

    async def put(self, write: ArtifactWrite) -> ArtifactCommit:
        if write.classification is ArtifactClassification.SECRET:
            raise ValueError("secret material cannot be persisted as an artifact")
        key = (write.artifact_key, write.lease.epoch)
        digest = hashlib.sha256(write.content).hexdigest()
        existing = self._commits.get(key)
        if existing is not None:
            if self._fingerprints[key] != digest:
                raise RuntimeError("artifact lease epoch was reused for different content")
            return existing
        last_epoch = self._last_epoch.get(write.artifact_key, 0)
        if write.lease.epoch <= last_epoch:
            raise RuntimeError("artifact write lease was fenced by a newer epoch")
        artifact_id = f"sha256:{digest}"
        self._content[artifact_id] = bytes(write.content)
        artifact = ArtifactRef(
            artifact_id=artifact_id,
            media_type=write.media_type,
            size_bytes=len(write.content),
            sha256=digest,
            location=f"memory://{digest}",
            metadata={
                **dict(write.metadata),
                "classification": write.classification.value,
                "owner": write.owner.principal_id,
                "lease_epoch": write.lease.epoch,
            },
        )
        commit = ArtifactCommit(
            artifact=artifact,
            artifact_key=write.artifact_key,
            owner=write.owner,
            lease_epoch=write.lease.epoch,
        )
        self._commits[key] = commit
        self._fingerprints[key] = digest
        self._last_epoch[write.artifact_key] = write.lease.epoch
        return commit

    async def get(self, artifact: ArtifactRef) -> bytes:
        try:
            return self._content[artifact.artifact_id]
        except KeyError as exc:
            raise KeyError(f"artifact {artifact.artifact_id} was not found") from exc


class MemoryArtifactStoreModule:
    @property
    def manifest(self) -> ModuleManifest:
        return ModuleManifest(
            module_id=ARTIFACT_MODULE_ID,
            version="1.0.0",
            provides=(ServiceProvision(ARTIFACT_STORE_SERVICE, "1.0.0"),),
        )

    async def attach(self, context: HostContext) -> AsyncDisposer | None:
        context.provide(
            ARTIFACT_STORE_SERVICE,
            MemoryArtifactStore(),
            version="1.0.0",
        )
        return None

    async def start(self, context: HostContext) -> None:
        del context


def _reject_sensitive_metadata(metadata: Mapping[str, str]) -> None:
    for key in metadata:
        normalized = key.casefold()
        if any(marker in normalized for marker in ("secret", "token", "password", "api_key")):
            raise ValueError(f"artifact metadata {key} may expose secret material")

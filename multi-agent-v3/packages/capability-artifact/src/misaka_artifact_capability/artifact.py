from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Protocol

from misaka_invocation_contracts import ArtifactRef
from misaka_kernel import HostContext
from misaka_kernel.lifecycle import AsyncDisposer
from misaka_kernel_contracts import (
    ModuleId,
    ModuleManifest,
    ServiceKey,
    ServiceProvision,
)

ARTIFACT_STORE_SERVICE = ServiceKey("capability.artifact.store")
ARTIFACT_MODULE_ID = ModuleId("capability.artifact.memory")


class ArtifactStore(Protocol):
    async def put(
        self,
        content: bytes,
        *,
        media_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> ArtifactRef: ...

    async def get(self, artifact: ArtifactRef) -> bytes: ...


class MemoryArtifactStore:
    def __init__(self) -> None:
        self._content: dict[str, bytes] = {}

    async def put(
        self,
        content: bytes,
        *,
        media_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> ArtifactRef:
        if not media_type.strip():
            raise ValueError("media_type must not be empty")
        digest = hashlib.sha256(content).hexdigest()
        artifact_id = f"sha256:{digest}"
        self._content[artifact_id] = bytes(content)
        return ArtifactRef(
            artifact_id=artifact_id,
            media_type=media_type,
            size_bytes=len(content),
            sha256=digest,
            location=f"memory://{digest}",
            metadata={key: value for key, value in (metadata or {}).items()},
        )

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

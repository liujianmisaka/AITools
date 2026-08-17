from __future__ import annotations

from typing import Protocol

from multi_agent_v2.packages.credentials.models import (
    CredentialInfo,
    CredentialRef,
    ResolvedCredential,
)


class CredentialProvider(Protocol):
    async def resolve(self, reference: CredentialRef) -> ResolvedCredential | None: ...

    async def info(self, reference: CredentialRef) -> CredentialInfo: ...

    async def set(self, reference: CredentialRef, value: str | None) -> CredentialInfo: ...

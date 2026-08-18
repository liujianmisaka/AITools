from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from misaka_invocation_contracts import SessionRef
from misaka_kernel import HostContext
from misaka_kernel.lifecycle import AsyncDisposer
from misaka_kernel_contracts import (
    JsonObject,
    ModuleId,
    ModuleManifest,
    ServiceKey,
    ServiceProvision,
)

SESSION_STORE_SERVICE = ServiceKey("capability.session.store")
MEMORY_SESSION_MODULE_ID = ModuleId("capability.session.memory")


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session: SessionRef
    status: str = "active"
    metadata: JsonObject = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    claimed_by: str | None = None


class SessionBusyError(RuntimeError):
    pass


class SessionStore(Protocol):
    async def create(self, provider: str, *, native_id: str | None = None) -> SessionRecord: ...

    async def get(self, session: SessionRef) -> SessionRecord | None: ...

    async def claim(self, session: SessionRef, owner: str) -> SessionRecord: ...

    async def release(self, session: SessionRef, owner: str) -> SessionRecord: ...


class MemorySessionStore:
    def __init__(self) -> None:
        self._records: dict[SessionRef, SessionRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, provider: str, *, native_id: str | None = None) -> SessionRecord:
        session = SessionRef(provider, native_id or uuid.uuid4().hex)
        record = SessionRecord(session=session)
        async with self._lock:
            if session in self._records:
                raise SessionBusyError(f"session {session.native_id} already exists")
            self._records[session] = record
        return record

    async def get(self, session: SessionRef) -> SessionRecord | None:
        async with self._lock:
            return self._records.get(session)

    async def claim(self, session: SessionRef, owner: str) -> SessionRecord:
        if not owner.strip():
            raise ValueError("owner must not be empty")
        async with self._lock:
            record = self._require(session)
            if record.claimed_by not in (None, owner):
                raise SessionBusyError(f"session {session.native_id} is claimed by another owner")
            claimed = SessionRecord(
                session=record.session,
                status=record.status,
                metadata=record.metadata,
                created_at=record.created_at,
                claimed_by=owner,
            )
            self._records[session] = claimed
            return claimed

    async def release(self, session: SessionRef, owner: str) -> SessionRecord:
        async with self._lock:
            record = self._require(session)
            if record.claimed_by != owner:
                raise SessionBusyError(f"session {session.native_id} is not claimed by {owner}")
            released = SessionRecord(
                session=record.session,
                status=record.status,
                metadata=record.metadata,
                created_at=record.created_at,
            )
            self._records[session] = released
            return released

    def _require(self, session: SessionRef) -> SessionRecord:
        record = self._records.get(session)
        if record is None:
            raise KeyError(f"session {session.native_id} was not found")
        return record


class MemorySessionStoreModule:
    @property
    def manifest(self) -> ModuleManifest:
        return ModuleManifest(
            module_id=MEMORY_SESSION_MODULE_ID,
            version="1.0.0",
            provides=(ServiceProvision(SESSION_STORE_SERVICE, "1.0.0"),),
        )

    async def attach(self, context: HostContext) -> AsyncDisposer | None:
        context.provide(
            SESSION_STORE_SERVICE,
            MemorySessionStore(),
            version="1.0.0",
        )
        return None

    async def start(self, context: HostContext) -> None:
        del context

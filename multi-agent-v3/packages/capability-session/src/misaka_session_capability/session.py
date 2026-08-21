from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
DEFAULT_SESSION_LEASE_TTL_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class SessionLease:
    session: SessionRef
    owner: str
    operation_id: str
    epoch: int
    token: str
    acquired_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.owner.strip() or not self.operation_id.strip() or not self.token.strip():
            raise ValueError("session lease owner, operation id and token must not be empty")
        if self.epoch < 1:
            raise ValueError("session lease epoch must be positive")
        if self.acquired_at.tzinfo is None or self.acquired_at.utcoffset() is None:
            raise ValueError("session lease acquired_at must be timezone-aware")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("session lease expires_at must be timezone-aware")
        if self.expires_at <= self.acquired_at:
            raise ValueError("session lease expiry must follow acquisition")

    def active_at(self, now: datetime | None = None) -> bool:
        value = now or datetime.now(UTC)
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("session lease current time must be timezone-aware")
        return value < self.expires_at


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session: SessionRef
    status: str = "active"
    metadata: JsonObject = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    revision: int = 1
    lease: SessionLease | None = None

    def __post_init__(self) -> None:
        if not self.status.strip():
            raise ValueError("session status must not be empty")
        if self.revision < 1:
            raise ValueError("session revision must be positive")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("session created_at must be timezone-aware")
        if self.lease is not None and self.lease.session != self.session:
            raise ValueError("session lease must belong to the record session")


class SessionLeaseError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SessionLeaseBusy(SessionLeaseError):
    def __init__(self, message: str) -> None:
        super().__init__("session.lease_busy", message)


class SessionLeaseExpired(SessionLeaseError):
    def __init__(self, message: str = "session lease has expired") -> None:
        super().__init__("session.lease_expired", message)


class SessionLeaseFenced(SessionLeaseError):
    def __init__(self, message: str = "session lease is fenced") -> None:
        super().__init__("session.lease_fenced", message)


class SessionStore(Protocol):
    async def create(self, provider: str, *, native_id: str | None = None) -> SessionRecord: ...

    async def ensure(self, session: SessionRef) -> SessionRecord: ...

    async def get(self, session: SessionRef) -> SessionRecord | None: ...

    async def acquire(
        self,
        session: SessionRef,
        owner: str,
        operation_id: str,
        *,
        ttl_seconds: float = DEFAULT_SESSION_LEASE_TTL_SECONDS,
    ) -> SessionLease: ...

    async def renew(self, lease: SessionLease, *, ttl_seconds: float) -> SessionLease: ...

    async def validate(self, lease: SessionLease) -> None: ...

    async def transfer(
        self,
        lease: SessionLease,
        owner: str,
        operation_id: str,
        *,
        ttl_seconds: float = DEFAULT_SESSION_LEASE_TTL_SECONDS,
    ) -> SessionLease: ...

    async def release(self, lease: SessionLease) -> SessionRecord: ...


class MemorySessionStore:
    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._records: dict[SessionRef, SessionRecord] = {}
        self._last_epochs: dict[SessionRef, int] = {}
        self._lock = asyncio.Lock()
        self._clock = clock or (lambda: datetime.now(UTC))

    async def create(self, provider: str, *, native_id: str | None = None) -> SessionRecord:
        session = SessionRef(provider, native_id or uuid.uuid4().hex)
        record = SessionRecord(session=session)
        async with self._lock:
            if session in self._records:
                raise SessionLeaseBusy(f"session {session.native_id} already exists")
            self._records[session] = record
            self._last_epochs.setdefault(session, 0)
        return record

    async def ensure(self, session: SessionRef) -> SessionRecord:
        existing = await self.get(session)
        if existing is not None:
            return existing
        try:
            return await self.create(session.provider, native_id=session.native_id)
        except SessionLeaseBusy:
            existing = await self.get(session)
            if existing is None:
                raise
            return existing

    async def get(self, session: SessionRef) -> SessionRecord | None:
        async with self._lock:
            return self._records.get(session)

    async def acquire(
        self,
        session: SessionRef,
        owner: str,
        operation_id: str,
        *,
        ttl_seconds: float = DEFAULT_SESSION_LEASE_TTL_SECONDS,
    ) -> SessionLease:
        _validate_lease_request(owner, operation_id, ttl_seconds)
        async with self._lock:
            record = self._require(session)
            now = self._clock()
            current = record.lease
            if current is not None and current.active_at(now):
                if current.owner == owner and current.operation_id == operation_id:
                    return current
                raise SessionLeaseBusy(f"session {session.native_id} is leased by another owner")
            epoch = self._last_epochs.get(session, 0) + 1
            lease = SessionLease(
                session=session,
                owner=owner,
                operation_id=operation_id,
                epoch=epoch,
                token=uuid.uuid4().hex,
                acquired_at=now,
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
            self._last_epochs[session] = epoch
            self._records[session] = _replace_record(record, lease=lease)
            return lease

    async def renew(self, lease: SessionLease, *, ttl_seconds: float) -> SessionLease:
        if ttl_seconds <= 0:
            raise ValueError("session lease ttl must be positive")
        async with self._lock:
            record = self._require(lease.session)
            now = self._clock()
            self._validate_unlocked(record, lease, now)
            renewed = SessionLease(
                session=lease.session,
                owner=lease.owner,
                operation_id=lease.operation_id,
                epoch=lease.epoch,
                token=lease.token,
                acquired_at=lease.acquired_at,
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
            self._records[lease.session] = _replace_record(record, lease=renewed)
            return renewed

    async def validate(self, lease: SessionLease) -> None:
        async with self._lock:
            record = self._require(lease.session)
            self._validate_unlocked(record, lease, self._clock())

    async def transfer(
        self,
        lease: SessionLease,
        owner: str,
        operation_id: str,
        *,
        ttl_seconds: float = DEFAULT_SESSION_LEASE_TTL_SECONDS,
    ) -> SessionLease:
        _validate_lease_request(owner, operation_id, ttl_seconds)
        async with self._lock:
            record = self._require(lease.session)
            now = self._clock()
            self._validate_unlocked(record, lease, now)
            if owner == lease.owner and operation_id == lease.operation_id:
                raise ValueError("session lease transfer requires a different owner or operation")
            epoch = self._last_epochs.get(lease.session, lease.epoch) + 1
            transferred = SessionLease(
                session=lease.session,
                owner=owner,
                operation_id=operation_id,
                epoch=epoch,
                token=uuid.uuid4().hex,
                acquired_at=now,
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
            self._last_epochs[lease.session] = epoch
            self._records[lease.session] = _replace_record(record, lease=transferred)
            return transferred

    async def release(self, lease: SessionLease) -> SessionRecord:
        async with self._lock:
            record = self._require(lease.session)
            self._validate_unlocked(record, lease, self._clock())
            released = _replace_record(record, lease=None)
            self._records[lease.session] = released
            return released

    def _validate_unlocked(self, record: SessionRecord, lease: SessionLease, now: datetime) -> None:
        current = record.lease
        if current is None or (
            current.epoch != lease.epoch
            or current.token != lease.token
            or current.owner != lease.owner
            or current.operation_id != lease.operation_id
        ):
            raise SessionLeaseFenced(
                f"session {lease.session.native_id} lease epoch {lease.epoch} is no longer current"
            )
        if not current.active_at(now):
            raise SessionLeaseExpired()

    def _require(self, session: SessionRef) -> SessionRecord:
        record = self._records.get(session)
        if record is None:
            raise KeyError(f"session {session.native_id} was not found")
        return record


class MemorySessionStoreModule:
    def __init__(self, store: MemorySessionStore | None = None) -> None:
        self.store = store or MemorySessionStore()

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
            self.store,
            version="1.0.0",
        )
        return None

    async def start(self, context: HostContext) -> None:
        del context


def _replace_record(record: SessionRecord, *, lease: SessionLease | None) -> SessionRecord:
    return SessionRecord(
        session=record.session,
        status=record.status,
        metadata=record.metadata,
        created_at=record.created_at,
        revision=record.revision + 1,
        lease=lease,
    )


def _validate_lease_request(owner: str, operation_id: str, ttl_seconds: float) -> None:
    if not owner.strip():
        raise ValueError("session lease owner must not be empty")
    if not operation_id.strip():
        raise ValueError("session lease operation id must not be empty")
    if ttl_seconds <= 0:
        raise ValueError("session lease ttl must be positive")

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import cast

import asyncpg
from misaka_kernel_contracts import JsonObject
from misaka_persistence_contracts import (
    CURRENT_DURABLE_FORMAT_VERSION,
    DurableConflict,
    DurableCorruption,
    DurableEvent,
    DurableJob,
    DurableJobStatus,
    DurableNotFound,
)

from misaka_persistence_postgres.schema import SCHEMA_SQL


class PostgresDurableStore:
    """One PostgreSQL fact source for durable events and jobs."""

    def __init__(self, dsn: str, *, command_timeout_seconds: float = 30.0) -> None:
        if not dsn.strip():
            raise ValueError("dsn must not be empty")
        if command_timeout_seconds <= 0:
            raise ValueError("command_timeout_seconds must be positive")
        self._dsn = dsn
        self._command_timeout_seconds = command_timeout_seconds
        self._pool: asyncpg.Pool[asyncpg.Record] | None = None

    @property
    def started(self) -> bool:
        return self._pool is not None

    async def start(self) -> None:
        if self._pool is not None:
            return
        pool = await asyncpg.create_pool(
            self._dsn,
            min_size=1,
            max_size=10,
            command_timeout=self._command_timeout_seconds,
        )
        try:
            async with pool.acquire() as connection:
                await connection.execute(SCHEMA_SQL)
        except Exception:
            await pool.close()
            raise
        self._pool = pool

    async def close(self) -> None:
        pool = self._pool
        self._pool = None
        if pool is not None:
            await pool.close()

    async def append(
        self,
        stream_id: str,
        event_id: str,
        event_type: str,
        payload: JsonObject,
        *,
        schema_version: int = CURRENT_DURABLE_FORMAT_VERSION,
        occurred_at: datetime | None = None,
    ) -> DurableEvent:
        if schema_version != CURRENT_DURABLE_FORMAT_VERSION:
            raise DurableConflict(
                "durable.unsupported_schema",
                f"unsupported durable event schema version {schema_version}",
            )
        pool = self._require_pool()
        async with pool.acquire() as connection, connection.transaction():
            existing = await connection.fetchrow(
                """
                SELECT stream_id, sequence, event_id, event_type, payload::text,
                       schema_version, occurred_at
                FROM durable_events
                WHERE stream_id = $1 AND event_id = $2
                """,
                stream_id,
                event_id,
            )
            if existing is not None:
                event = _event_from_row(existing)
                if event.event_type != event_type or event.payload != payload:
                    raise DurableConflict(
                        "durable.event_conflict",
                        f"event {event_id} already exists with different content",
                    )
                return event
            await connection.execute("SELECT pg_advisory_xact_lock(hashtext($1))", stream_id)
            sequence = await connection.fetchval(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM durable_events WHERE stream_id = $1",
                stream_id,
            )
            if not isinstance(sequence, int):
                raise RuntimeError("PostgreSQL returned an invalid event sequence")
            row = await connection.fetchrow(
                """
                INSERT INTO durable_events (
                    stream_id, sequence, event_id, event_type, payload, schema_version, occurred_at
                ) VALUES ($1, $2, $3, $4, $5::jsonb, $6, COALESCE($7, clock_timestamp()))
                RETURNING stream_id, sequence, event_id, event_type, payload::text,
                          schema_version, occurred_at
                """,
                stream_id,
                sequence,
                event_id,
                event_type,
                _json(payload),
                schema_version,
                occurred_at,
            )
            if row is None:
                raise RuntimeError("PostgreSQL did not return the inserted event")
            return _event_from_row(row)

    async def read(self, stream_id: str, *, start_sequence: int = 1) -> tuple[DurableEvent, ...]:
        if start_sequence < 1:
            raise ValueError("start_sequence must be at least one")
        rows = await self._require_pool().fetch(
            """
            SELECT stream_id, sequence, event_id, event_type, payload::text,
                   schema_version, occurred_at
            FROM durable_events
            WHERE stream_id = $1 AND sequence >= $2
            ORDER BY sequence
            """,
            stream_id,
            start_sequence,
        )
        return tuple(_event_from_row(row) for row in rows)

    async def register(
        self,
        job_id: str,
        idempotency_key: str,
        request: JsonObject,
    ) -> tuple[DurableJob, bool]:
        fingerprint = _fingerprint(request)
        pool = self._require_pool()
        async with pool.acquire() as connection, connection.transaction():
            existing = await connection.fetchrow(
                """
                SELECT job_id, idempotency_key, request::text, status, version,
                       result::text, error_code, error_message, created_at, updated_at,
                       request_fingerprint
                FROM durable_jobs
                WHERE idempotency_key = $1 OR job_id = $2
                FOR UPDATE
                """,
                idempotency_key,
                job_id,
            )
            if existing is not None:
                job = _job_from_row(existing)
                if (
                    job.job_id != job_id
                    or job.idempotency_key != idempotency_key
                    or str(existing["request_fingerprint"]) != fingerprint
                ):
                    raise DurableConflict(
                        "durable.job_conflict",
                        "job id or idempotency key has different content",
                    )
                return job, False
            row = await connection.fetchrow(
                """
                INSERT INTO durable_jobs (
                    job_id, idempotency_key, request, request_fingerprint, status, version
                ) VALUES ($1, $2, $3::jsonb, $4, 'queued', 1)
                RETURNING job_id, idempotency_key, request::text, status, version,
                          result::text, error_code, error_message, created_at, updated_at
                """,
                job_id,
                idempotency_key,
                _json(request),
                fingerprint,
            )
            if row is None:
                raise RuntimeError("PostgreSQL did not return the inserted job")
            return _job_from_row(row), True

    async def get(self, job_id: str) -> DurableJob:
        row = await self._require_pool().fetchrow(
            """
            SELECT job_id, idempotency_key, request::text, status, version,
                   result::text, error_code, error_message, created_at, updated_at
            FROM durable_jobs WHERE job_id = $1
            """,
            job_id,
        )
        if row is None:
            raise DurableNotFound("durable.job_not_found", f"job {job_id} was not found")
        return _job_from_row(row)

    async def list(self) -> tuple[DurableJob, ...]:
        rows = await self._require_pool().fetch(
            """
            SELECT job_id, idempotency_key, request::text, status, version,
                   result::text, error_code, error_message, created_at, updated_at
            FROM durable_jobs ORDER BY created_at, job_id
            """
        )
        return tuple(_job_from_row(row) for row in rows)

    async def transition(
        self,
        job_id: str,
        status: DurableJobStatus,
        *,
        expected_version: int | None = None,
        result: JsonObject | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> DurableJob:
        current = await self.get(job_id)
        version = expected_version or current.version
        if current.version != version:
            raise DurableConflict(
                "durable.version_conflict",
                f"job {job_id} expected version {version}, got {current.version}",
            )
        if current.status in _TERMINAL_STATUSES:
            if current.status is status and current.result == result:
                return current
            raise DurableConflict("durable.terminal_conflict", f"job {job_id} is terminal")
        row = await self._require_pool().fetchrow(
            """
            UPDATE durable_jobs
            SET status = $1, version = version + 1, result = $2::jsonb,
                error_code = $3, error_message = $4, updated_at = clock_timestamp()
            WHERE job_id = $5 AND version = $6
              AND status NOT IN ('succeeded', 'failed', 'cancelled', 'reconciliation_required')
            RETURNING job_id, idempotency_key, request::text, status, version,
                      result::text, error_code, error_message, created_at, updated_at
            """,
            status.value,
            _json(result) if result is not None else None,
            error_code,
            error_message,
            job_id,
            version,
        )
        if row is None:
            raise DurableConflict("durable.version_conflict", f"job {job_id} changed concurrently")
        return _job_from_row(row)

    def _require_pool(self) -> asyncpg.Pool[asyncpg.Record]:
        if self._pool is None:
            raise RuntimeError("PostgresDurableStore must be started before use")
        return self._pool


def _event_from_row(row: asyncpg.Record) -> DurableEvent:
    schema_version = int(row["schema_version"])
    if schema_version != CURRENT_DURABLE_FORMAT_VERSION:
        raise DurableCorruption(
            "durable.unsupported_schema",
            f"unsupported durable event schema version {schema_version}",
        )
    return DurableEvent(
        stream_id=str(row["stream_id"]),
        sequence=int(row["sequence"]),
        event_id=str(row["event_id"]),
        event_type=str(row["event_type"]),
        payload=_json_object(row["payload"]),
        schema_version=schema_version,
        occurred_at=cast(datetime, row["occurred_at"]),
    )


def _job_from_row(row: asyncpg.Record) -> DurableJob:
    result = row["result"]
    return DurableJob(
        job_id=str(row["job_id"]),
        idempotency_key=str(row["idempotency_key"]),
        request=_json_object(row["request"]),
        status=DurableJobStatus(str(row["status"])),
        version=int(row["version"]),
        result=_json_object(result) if result is not None else None,
        error_code=_optional_str(row["error_code"]),
        error_message=_optional_str(row["error_message"]),
        created_at=cast(datetime, row["created_at"]),
        updated_at=cast(datetime, row["updated_at"]),
    )


def _json(value: JsonObject) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(value: object) -> JsonObject:
    decoded: object = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise RuntimeError("PostgreSQL JSON payload is not an object")
    return cast(JsonObject, decoded)


def _fingerprint(value: JsonObject) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


_TERMINAL_STATUSES = frozenset(
    {
        DurableJobStatus.SUCCEEDED,
        DurableJobStatus.FAILED,
        DurableJobStatus.CANCELLED,
        DurableJobStatus.RECONCILIATION_REQUIRED,
    }
)

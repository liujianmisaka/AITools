from __future__ import annotations

import os
import uuid

import pytest
from misaka_persistence_contracts import DurableConflict, DurableJobStatus
from misaka_persistence_postgres import SCHEMA_SQL, PostgresDurableStore


def test_postgres_schema_declares_single_event_and_job_fact_source() -> None:
    assert "PRIMARY KEY (stream_id, sequence)" in SCHEMA_SQL
    assert "UNIQUE (stream_id, event_id)" in SCHEMA_SQL
    assert "schema_version BIGINT NOT NULL" in SCHEMA_SQL
    assert "idempotency_key TEXT NOT NULL UNIQUE" in SCHEMA_SQL
    assert "version BIGINT NOT NULL" in SCHEMA_SQL
    assert "reconciliation_required" in SCHEMA_SQL


@pytest.mark.asyncio
async def test_postgres_store_requires_explicit_lifecycle() -> None:
    store = PostgresDurableStore("postgresql://example.invalid/test")
    with pytest.raises(RuntimeError, match="started"):
        await store.read("stream-1")


@pytest.mark.asyncio
async def test_postgres_store_integration() -> None:
    dsn = os.environ.get("MULTI_AGENT_V3_POSTGRES_DSN")
    if dsn is None:
        pytest.skip("MULTI_AGENT_V3_POSTGRES_DSN is not configured")
    suffix = uuid.uuid4().hex
    store = PostgresDurableStore(dsn)
    await store.start()
    try:
        first = await store.append(
            f"stream-{suffix}",
            f"event-{suffix}",
            "job.created",
            {"job_id": f"job-{suffix}"},
        )
        duplicate = await store.append(
            f"stream-{suffix}",
            f"event-{suffix}",
            "job.created",
            {"job_id": f"job-{suffix}"},
        )
        assert duplicate == first

        job, created = await store.register(f"job-{suffix}", f"idem-{suffix}", {"prompt": "hello"})
        assert created is True
        running = await store.transition(
            job.job_id, DurableJobStatus.RUNNING, expected_version=job.version
        )
        succeeded = await store.transition(
            job.job_id,
            DurableJobStatus.SUCCEEDED,
            expected_version=running.version,
            result={"answer": "ok"},
        )
        assert succeeded.result == {"answer": "ok"}
        with pytest.raises(DurableConflict):
            await store.transition(job.job_id, DurableJobStatus.FAILED)
    finally:
        await store.close()

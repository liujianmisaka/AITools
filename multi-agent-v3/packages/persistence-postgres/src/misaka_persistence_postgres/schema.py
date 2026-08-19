SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS durable_events (
    stream_id TEXT NOT NULL,
    sequence BIGINT NOT NULL CHECK (sequence > 0),
    event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    schema_version BIGINT NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (stream_id, sequence),
    UNIQUE (stream_id, event_id)
);

CREATE INDEX IF NOT EXISTS ix_durable_events_event_id
    ON durable_events (event_id);

CREATE TABLE IF NOT EXISTS durable_jobs (
    job_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request JSONB NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'queued', 'running', 'succeeded', 'failed', 'cancelled',
            'reconciliation_required'
        )
    ),
    version BIGINT NOT NULL CHECK (version > 0),
    result JSONB,
    error_code TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS ix_durable_jobs_status_updated
    ON durable_jobs (status, updated_at);
"""

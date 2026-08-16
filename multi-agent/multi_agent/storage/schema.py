from __future__ import annotations


SCHEMA_VERSION = 4

SCHEMA_SQL = """
CREATE TABLE workflow_templates (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL CHECK(version >= 1),
    kind TEXT NOT NULL,
    definition_schema_version INTEGER NOT NULL CHECK(definition_schema_version >= 1),
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    work_item_count INTEGER NOT NULL CHECK(work_item_count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT
);

CREATE TABLE trigger_bindings (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_version INTEGER NOT NULL CHECK(event_version >= 1),
    source_key TEXT,
    template_id TEXT NOT NULL
        REFERENCES workflow_templates(id) ON DELETE RESTRICT,
    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
    source_config_json TEXT NOT NULL,
    event_filter_json TEXT NOT NULL,
    input_mapping_json TEXT NOT NULL,
    concurrency_policy TEXT NOT NULL
        CHECK(concurrency_policy IN ('allow_parallel', 'skip_if_running')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT
);

CREATE TABLE trigger_events (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_version INTEGER NOT NULL CHECK(event_version >= 1),
    source_key TEXT,
    dedup_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('received', 'processed', 'failed')),
    error TEXT,
    received_at TEXT NOT NULL,
    processed_at TEXT,
    UNIQUE(source_type, dedup_key)
);

CREATE TABLE workflow_instances (
    id TEXT PRIMARY KEY,
    template_id TEXT REFERENCES workflow_templates(id) ON DELETE RESTRICT,
    template_version INTEGER,
    source TEXT NOT NULL CHECK(source IN ('template', 'ad_hoc')),
    kind TEXT NOT NULL,
    definition_schema_version INTEGER NOT NULL CHECK(definition_schema_version >= 1),
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    input_json TEXT NOT NULL,
    runtime_state_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    work_item_count INTEGER NOT NULL CHECK(work_item_count >= 0),
    status TEXT NOT NULL,
    cause_type TEXT NOT NULL CHECK(cause_type IN ('manual', 'trigger')),
    trigger_binding_id TEXT REFERENCES trigger_bindings(id) ON DELETE RESTRICT,
    trigger_event_id TEXT REFERENCES trigger_events(id) ON DELETE RESTRICT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(
        (source = 'template' AND template_id IS NOT NULL AND template_version IS NOT NULL)
        OR (source = 'ad_hoc' AND template_id IS NULL AND template_version IS NULL)
    ),
    CHECK(
        (cause_type = 'manual' AND trigger_binding_id IS NULL AND trigger_event_id IS NULL)
        OR (cause_type = 'trigger' AND source = 'template'
            AND trigger_binding_id IS NOT NULL AND trigger_event_id IS NOT NULL)
    ),
    UNIQUE(trigger_binding_id, trigger_event_id)
);

CREATE TABLE work_items (
    id TEXT PRIMARY KEY,
    workflow_instance_id TEXT NOT NULL
        REFERENCES workflow_instances(id) ON DELETE CASCADE,
    logical_key TEXT NOT NULL,
    activation_number INTEGER NOT NULL DEFAULT 1 CHECK(activation_number >= 1),
    executor_kind TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    provider_session_id TEXT,
    final_output TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(workflow_instance_id, logical_key, activation_number)
);

CREATE TABLE execution_attempts (
    id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL
        REFERENCES work_items(id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    provider_session_id TEXT,
    error_code TEXT,
    error_message TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE TABLE workflow_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_instance_id TEXT NOT NULL
        REFERENCES workflow_instances(id) ON DELETE CASCADE,
    work_item_id TEXT,
    execution_attempt_id TEXT,
    provider TEXT,
    kind TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    raw_event_type TEXT
);

CREATE TABLE workflow_approvals (
    id TEXT PRIMARY KEY,
    workflow_instance_id TEXT NOT NULL
        REFERENCES workflow_instances(id) ON DELETE CASCADE,
    work_item_id TEXT NOT NULL
        REFERENCES work_items(id) ON DELETE CASCADE,
    execution_attempt_id TEXT NOT NULL
        REFERENCES execution_attempts(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_request_id TEXT NOT NULL,
    status TEXT NOT NULL,
    request_json TEXT NOT NULL,
    decided_by TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE TABLE trigger_deliveries (
    id TEXT PRIMARY KEY,
    trigger_event_id TEXT NOT NULL
        REFERENCES trigger_events(id) ON DELETE CASCADE,
    trigger_binding_id TEXT NOT NULL
        REFERENCES trigger_bindings(id) ON DELETE RESTRICT,
    binding_snapshot_json TEXT NOT NULL,
    workflow_instance_id TEXT
        REFERENCES workflow_instances(id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'delivered', 'skipped', 'failed')),
    reason TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(trigger_event_id, trigger_binding_id)
);

CREATE TABLE trigger_source_state (
    source_type TEXT NOT NULL,
    source_key TEXT NOT NULL,
    cursor_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(source_type, source_key)
);

CREATE TABLE internal_event_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_version INTEGER NOT NULL CHECK(event_version >= 1),
    source_key TEXT,
    dedup_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'published', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    published_at TEXT,
    UNIQUE(source_type, dedup_key)
);

CREATE TABLE scheduled_tasks (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL CHECK(version >= 1),
    name TEXT NOT NULL,
    schedule_type TEXT NOT NULL,
    schedule_json TEXT NOT NULL,
    action_type TEXT NOT NULL,
    action_json TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
    next_run_at TEXT,
    last_run_at TEXT,
    last_status TEXT,
    last_error TEXT,
    scheduler_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT
);

CREATE TABLE scheduled_task_runs (
    id TEXT PRIMARY KEY,
    scheduled_task_id TEXT NOT NULL
        REFERENCES scheduled_tasks(id) ON DELETE RESTRICT,
    scheduled_for TEXT,
    status TEXT NOT NULL
        CHECK(status IN ('running', 'succeeded', 'failed', 'interrupted')),
    result_json TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX idx_workflow_templates_active_updated
ON workflow_templates(updated_at DESC, id DESC)
WHERE archived_at IS NULL;

CREATE INDEX idx_workflow_instances_created
ON workflow_instances(created_at DESC, id DESC);

CREATE INDEX idx_workflow_instances_template
ON workflow_instances(template_id);

CREATE INDEX idx_workflow_instances_trigger
ON workflow_instances(trigger_binding_id, trigger_event_id);

CREATE INDEX idx_work_items_instance_status
ON work_items(workflow_instance_id, status);

CREATE INDEX idx_execution_attempts_work_item
ON execution_attempts(work_item_id);

CREATE INDEX idx_workflow_events_instance_id
ON workflow_events(workflow_instance_id, id);

CREATE INDEX idx_workflow_approvals_instance_status
ON workflow_approvals(workflow_instance_id, status);

CREATE INDEX idx_trigger_bindings_match
ON trigger_bindings(source_type, event_type, event_version, source_key)
WHERE enabled = 1 AND archived_at IS NULL;

CREATE UNIQUE INDEX idx_trigger_bindings_webhook_source_key
ON trigger_bindings(source_type, source_key)
WHERE source_type = 'webhook' AND archived_at IS NULL;

CREATE INDEX idx_trigger_events_received
ON trigger_events(received_at DESC, id DESC);

CREATE INDEX idx_trigger_deliveries_status
ON trigger_deliveries(status, created_at, id);

CREATE INDEX idx_scheduled_tasks_enabled
ON scheduled_tasks(enabled, updated_at, id)
WHERE archived_at IS NULL;

CREATE INDEX idx_scheduled_task_runs_task_started
ON scheduled_task_runs(scheduled_task_id, started_at DESC, id DESC);
"""

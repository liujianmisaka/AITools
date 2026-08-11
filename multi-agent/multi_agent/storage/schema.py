from __future__ import annotations


SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE workflow_templates (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL CHECK(version >= 1),
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    task_count INTEGER NOT NULL CHECK(task_count >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT
);

CREATE TABLE workflow_instances (
    id TEXT PRIMARY KEY,
    template_id TEXT REFERENCES workflow_templates(id) ON DELETE RESTRICT,
    template_version INTEGER,
    source TEXT NOT NULL CHECK(source IN ('template', 'ad_hoc')),
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    task_count INTEGER NOT NULL CHECK(task_count >= 0),
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(
        (source = 'template' AND template_id IS NOT NULL AND template_version IS NOT NULL)
        OR (source = 'ad_hoc' AND template_id IS NULL AND template_version IS NULL)
    )
);

CREATE TABLE task_instances (
    id TEXT PRIMARY KEY,
    workflow_instance_id TEXT NOT NULL
        REFERENCES workflow_instances(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    provider_session_id TEXT,
    final_output TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(workflow_instance_id, task_id)
);

CREATE TABLE execution_attempts (
    id TEXT PRIMARY KEY,
    task_instance_id TEXT NOT NULL
        REFERENCES task_instances(id) ON DELETE CASCADE,
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
    task_instance_id TEXT,
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
    task_instance_id TEXT NOT NULL
        REFERENCES task_instances(id) ON DELETE CASCADE,
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

CREATE INDEX idx_workflow_templates_active_updated
ON workflow_templates(updated_at DESC, id DESC)
WHERE archived_at IS NULL;

CREATE INDEX idx_workflow_instances_created
ON workflow_instances(created_at DESC, id DESC);

CREATE INDEX idx_workflow_instances_template
ON workflow_instances(template_id);

CREATE INDEX idx_task_instances_instance_status
ON task_instances(workflow_instance_id, status);

CREATE INDEX idx_execution_attempts_task_instance
ON execution_attempts(task_instance_id);

CREATE INDEX idx_workflow_events_instance_id
ON workflow_events(workflow_instance_id, id);

CREATE INDEX idx_workflow_approvals_instance_status
ON workflow_approvals(workflow_instance_id, status);
"""

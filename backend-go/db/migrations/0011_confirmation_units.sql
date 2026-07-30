CREATE TABLE IF NOT EXISTS go_confirmation_units (
    unit_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES go_control_tasks(task_id),
    unit_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (task_id, unit_key)
);

CREATE TABLE IF NOT EXISTS go_confirmation_unit_versions (
    unit_version_id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES go_confirmation_units(unit_id),
    draft_id TEXT NOT NULL REFERENCES go_working_draft_versions(draft_id),
    content_hash TEXT NOT NULL,
    title TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (draft_id, unit_id)
);

CREATE INDEX IF NOT EXISTS ix_go_confirmation_versions_draft
    ON go_confirmation_unit_versions(draft_id, ordinal);

CREATE TABLE IF NOT EXISTS go_confirmation_decisions (
    decision_id TEXT PRIMARY KEY,
    unit_version_id TEXT NOT NULL REFERENCES go_confirmation_unit_versions(unit_version_id),
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('CONFIRMED', 'REOPENED')),
    feedback TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    expected_task_version INTEGER NOT NULL,
    resulting_task_version INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (tenant_id, owner_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS go_run_revision_scopes (
    run_id TEXT PRIMARY KEY REFERENCES go_agent_runs(run_id),
    base_draft_id TEXT NOT NULL REFERENCES go_working_draft_versions(draft_id),
    base_draft_hash TEXT NOT NULL,
    reopened_unit_keys TEXT[] NOT NULL,
    immutable_unit_keys TEXT[] NOT NULL,
    user_feedback TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

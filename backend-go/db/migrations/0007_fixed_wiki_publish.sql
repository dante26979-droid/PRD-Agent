CREATE TABLE IF NOT EXISTS go_publish_intents (
    publish_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES go_control_tasks(task_id),
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    draft_id TEXT NOT NULL REFERENCES go_working_draft_versions(draft_id),
    task_version INTEGER NOT NULL CHECK (task_version > 0),
    draft_version INTEGER NOT NULL CHECK (draft_version > 0),
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'PREVIEW', 'PENDING', 'RUNNING', 'SUCCEEDED',
        'FAILED_RETRYABLE', 'RECONCILING', 'MANUAL_REVIEW'
    )),
    safe_url TEXT,
    provider_revision TEXT,
    error_code TEXT,
    retryable BOOLEAN NOT NULL DEFAULT FALSE,
    expires_at TIMESTAMPTZ NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    claim_worker TEXT,
    claim_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_go_publish_intents_claim
    ON go_publish_intents(status, available_at, created_at)
    WHERE status IN ('PENDING', 'FAILED_RETRYABLE', 'RECONCILING');

CREATE UNIQUE INDEX IF NOT EXISTS ux_go_publish_intents_active_task
    ON go_publish_intents(task_id)
    WHERE status IN ('PENDING', 'RUNNING', 'RECONCILING');

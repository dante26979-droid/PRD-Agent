-- Expand the production control plane. This migration is additive and can be
-- applied after the original Step 10 schema on an existing installation.

ALTER TABLE production_run_control
    ADD COLUMN IF NOT EXISTS queue_slot_acquired BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE production_run_control
    ADD COLUMN IF NOT EXISTS queue_admitted_at TIMESTAMPTZ;

ALTER TABLE production_run_control
    DROP CONSTRAINT IF EXISTS production_run_control_status_check;

ALTER TABLE production_run_control
    ADD CONSTRAINT production_run_control_status_check CHECK (status IN (
        'WAITING_CAPACITY', 'WAITING_PROVIDER', 'QUEUED', 'RUNNING',
        'WAITING_USER', 'SUCCEEDED', 'FAILED', 'STOPPING', 'STOPPED'
    ));

CREATE INDEX IF NOT EXISTS ix_production_run_admission
    ON production_run_control(queue_slot_acquired, status, created_at, run_id)
    WHERE status IN ('WAITING_CAPACITY', 'QUEUED', 'RUNNING');

CREATE INDEX IF NOT EXISTS ix_production_run_owner_admission
    ON production_run_control(tenant_id, owner_id, status, created_at)
    WHERE queue_slot_acquired = TRUE;

CREATE TABLE IF NOT EXISTS queue_slots (
    slot_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES production_run_control(run_id),
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ACQUIRED', 'RELEASED')),
    acquired_at TIMESTAMPTZ NOT NULL,
    released_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_queue_slots_active_owner
    ON queue_slots(tenant_id, owner_id, state)
    WHERE state = 'ACQUIRED';

CREATE TABLE IF NOT EXISTS scheduler_cursors (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    last_admitted_at TIMESTAMPTZ,
    last_run_id TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, owner_id)
);

CREATE TABLE IF NOT EXISTS model_attempts (
    attempt_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    attempt_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    provider TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'STARTED', 'SUCCEEDED', 'FAILED', 'RESULT_UNKNOWN'
    )),
    response_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    token_usage JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_category TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    UNIQUE (run_id, attempt_key)
);

CREATE INDEX IF NOT EXISTS ix_model_attempts_run
    ON model_attempts(tenant_id, owner_id, run_id, started_at);

INSERT INTO schema_migrations(version)
VALUES ('20260728_production_queue_and_attempts')
ON CONFLICT (version) DO NOTHING;

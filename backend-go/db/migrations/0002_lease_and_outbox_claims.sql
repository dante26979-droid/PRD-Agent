ALTER TABLE go_agent_runs
    ADD COLUMN IF NOT EXISTS lease_id TEXT,
    ADD COLUMN IF NOT EXISTS worker_id TEXT,
    ADD COLUMN IF NOT EXISTS fencing_token BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;

ALTER TABLE go_outbox_messages
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ix_go_agent_runs_lease
    ON go_agent_runs(status, lease_expires_at)
    WHERE status = 'RUNNING';

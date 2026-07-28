CREATE TABLE IF NOT EXISTS go_agent_dispatches (
    dispatch_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES go_agent_runs(run_id),
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    last_error TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    UNIQUE (run_id, attempt_no)
);

CREATE INDEX IF NOT EXISTS ix_go_agent_dispatches_recovery
    ON go_agent_dispatches(status, started_at);

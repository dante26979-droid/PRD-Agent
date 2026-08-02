CREATE TABLE IF NOT EXISTS go_run_unit_scopes (
    run_id TEXT PRIMARY KEY REFERENCES go_agent_runs(run_id),
    run_purpose TEXT NOT NULL CHECK (run_purpose IN ('PLAN_OUTLINE','GENERATE_UNIT','REVISE_UNIT','FULL_REVIEW')),
    scope_payload JSONB NOT NULL,
    scope_hash TEXT NOT NULL,
    expected_task_version INTEGER NOT NULL CHECK (expected_task_version > 0),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS go_run_outputs (
    run_id TEXT NOT NULL REFERENCES go_agent_runs(run_id),
    output_key TEXT NOT NULL,
    output_kind TEXT NOT NULL CHECK (output_kind IN ('OUTLINE_CANDIDATE','UNIT_CANDIDATE','UNIT_PATCH','FULL_REVIEW_REPORT')),
    run_purpose TEXT NOT NULL CHECK (run_purpose IN ('PLAN_OUTLINE','GENERATE_UNIT','REVISE_UNIT','FULL_REVIEW')),
    scope_hash TEXT NOT NULL,
    expected_task_version INTEGER NOT NULL CHECK (expected_task_version > 0),
    content_hash TEXT NOT NULL,
    payload BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (run_id, output_key)
);

CREATE INDEX IF NOT EXISTS ix_go_run_outputs_purpose
    ON go_run_outputs(run_purpose, created_at DESC);

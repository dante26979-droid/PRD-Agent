CREATE TABLE IF NOT EXISTS go_run_checkpoints (
    run_id TEXT NOT NULL REFERENCES go_agent_runs(run_id),
    sequence BIGINT NOT NULL CHECK (sequence > 0),
    checkpoint_blob BYTEA NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (run_id, sequence)
);

CREATE INDEX IF NOT EXISTS ix_go_run_checkpoints_latest
    ON go_run_checkpoints(run_id, sequence DESC);

CREATE TABLE IF NOT EXISTS go_model_attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES go_agent_runs(run_id),
    attempt_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    prompt_version TEXT,
    provider TEXT,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    response_metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    token_usage_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_category TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (run_id, attempt_key)
);

CREATE INDEX IF NOT EXISTS ix_go_model_attempts_run
    ON go_model_attempts(run_id, created_at DESC);

CREATE TABLE IF NOT EXISTS go_evidence (
    evidence_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES go_agent_runs(run_id),
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    locator TEXT NOT NULL,
    excerpt_hash TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (run_id, source_type, source_id, locator, excerpt_hash)
);

CREATE INDEX IF NOT EXISTS ix_go_evidence_run
    ON go_evidence(run_id, created_at);

CREATE TABLE IF NOT EXISTS go_working_draft_versions (
    draft_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES go_control_tasks(task_id),
    run_id TEXT NOT NULL REFERENCES go_agent_runs(run_id),
    draft_key TEXT NOT NULL,
    patch BYTEA NOT NULL,
    patch_hash TEXT NOT NULL,
    task_version INTEGER NOT NULL CHECK (task_version > 0),
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (task_id, draft_key)
);

CREATE INDEX IF NOT EXISTS ix_go_working_drafts_task
    ON go_working_draft_versions(task_id, task_version DESC);

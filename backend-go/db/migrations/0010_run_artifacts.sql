CREATE TABLE IF NOT EXISTS go_run_artifacts (
    run_id TEXT NOT NULL REFERENCES go_agent_runs(run_id),
    artifact_key TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    generation BIGINT NOT NULL CHECK (generation >= 0),
    request_hash TEXT NOT NULL,
    content BYTEA NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (run_id, artifact_key)
);

CREATE INDEX IF NOT EXISTS ix_go_run_artifacts_expiry
    ON go_run_artifacts(expires_at);

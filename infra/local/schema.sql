CREATE TABLE IF NOT EXISTS eval_dataset (
    dataset_version TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL,
    resolved_commit_sha CHAR(40) NOT NULL,
    case_count INTEGER NOT NULL CHECK (case_count >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS eval_case (
    dataset_version TEXT NOT NULL REFERENCES eval_dataset(dataset_version),
    case_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (dataset_version, case_id)
);

CREATE TABLE IF NOT EXISTS eval_run (
    eval_run_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    config_id TEXT NOT NULL,
    trial_no INTEGER NOT NULL CHECK (trial_no >= 1),
    model_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    repository_commit CHAR(40) NOT NULL,
    input_hash TEXT NOT NULL,
    output_hash TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    token_usage JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
    output TEXT,
    error TEXT,
    UNIQUE (config_id, case_id, trial_no)
);

CREATE TABLE IF NOT EXISTS eval_metric (
    eval_run_id TEXT NOT NULL REFERENCES eval_run(eval_run_id),
    metric_name TEXT NOT NULL,
    value DOUBLE PRECISION,
    status TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (eval_run_id, metric_name)
);

CREATE TABLE IF NOT EXISTS eval_artifact (
    artifact_id BIGSERIAL PRIMARY KEY,
    eval_run_id TEXT REFERENCES eval_run(eval_run_id),
    artifact_type TEXT NOT NULL,
    path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

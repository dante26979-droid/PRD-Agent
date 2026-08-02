ALTER TABLE go_agent_runs
    ADD COLUMN execution_ledger_version TEXT;

ALTER TABLE go_agent_runs
    ADD CONSTRAINT ck_go_agent_runs_execution_ledger_version
    CHECK (execution_ledger_version IS NULL OR execution_ledger_version = 'run-ledger.v1');

CREATE TABLE go_run_budget_state (
    run_id TEXT PRIMARY KEY REFERENCES go_agent_runs(run_id),
    policy_hash TEXT NOT NULL,
    policy_json JSONB NOT NULL,
    reserved_json JSONB NOT NULL,
    consumed_json JSONB NOT NULL,
    overage_json JSONB NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE go_run_ledger_entries (
    entry_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES go_agent_runs(run_id),
    operation_key TEXT NOT NULL,
    entry_kind TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    reservation_json JSONB NOT NULL,
    consumption_json JSONB NOT NULL,
    output_artifact_key TEXT,
    output_artifact_hash TEXT,
    evidence_refs TEXT[] NOT NULL DEFAULT '{}',
    error_category TEXT,
    retryable BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL,
    call_started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (run_id, operation_key),
    CHECK (entry_kind IN ('MODEL','CAPABILITY','LOCAL_TRANSITION')),
    CHECK (status IN ('RESERVED','CALL_STARTED','SUCCEEDED','FAILED','OUTCOME_UNKNOWN'))
);

CREATE INDEX idx_go_run_ledger_entries_run_created
    ON go_run_ledger_entries(run_id, created_at, entry_id);

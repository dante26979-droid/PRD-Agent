CREATE TABLE IF NOT EXISTS go_agent_rollout_assignments (
    run_id TEXT PRIMARY KEY REFERENCES go_agent_runs(run_id),
    authoritative_workflow_version TEXT NOT NULL,
    evaluation_mode TEXT NOT NULL CHECK (evaluation_mode IN ('OFF','SHADOW','ENFORCE')),
    shadow_workflow_version TEXT,
    cohort TEXT NOT NULL CHECK (cohort IN ('CONTROL','INTERNAL','CANARY','DEFAULT')),
    policy_version TEXT NOT NULL,
    assignment_reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS go_rollout_gate_decisions (
    decision_id TEXT PRIMARY KEY,
    candidate_version TEXT NOT NULL,
    baseline_version TEXT NOT NULL,
    report_hash TEXT NOT NULL,
    gate_manifest_hash TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('PASSED','FAILED')),
    failed_gate_codes JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS go_rollout_drains (
    drain_id TEXT PRIMARY KEY,
    workflow_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('STARTED','BLOCKED','COMPLETED')),
    active_run_count BIGINT NOT NULL CHECK (active_run_count >= 0),
    active_dispatch_count BIGINT NOT NULL CHECK (active_dispatch_count >= 0),
    snapshot_count BIGINT NOT NULL CHECK (snapshot_count >= 0),
    evidence JSONB NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ
);

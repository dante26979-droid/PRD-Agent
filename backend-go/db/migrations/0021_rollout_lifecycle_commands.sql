ALTER TABLE go_rollout_commands
    DROP CONSTRAINT IF EXISTS go_rollout_commands_command_kind_check;

ALTER TABLE go_rollout_commands
    ADD CONSTRAINT go_rollout_commands_command_kind_check CHECK (command_kind IN (
        'PAUSE', 'RESUME', 'ROLLBACK_NEW_ASSIGNMENTS',
        'RECORD_GATE', 'TRANSITION_STAGE', 'RECORD_READINESS'
    ));

CREATE TABLE IF NOT EXISTS go_rollout_readiness_records (
    readiness_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL CHECK (schema_version = 'agent-runtime-readiness.v1'),
    candidate_version TEXT NOT NULL,
    baseline_version TEXT NOT NULL,
    commit_hash TEXT NOT NULL,
    migration_set_hash TEXT NOT NULL,
    migration_head TEXT NOT NULL,
    contract_report_hash TEXT NOT NULL,
    postgres_report_hash TEXT NOT NULL,
    eval_manifest_hash TEXT NOT NULL,
    shadow_policy_hash TEXT NOT NULL,
    gate_decision_id TEXT REFERENCES go_rollout_gate_decisions(decision_id),
    rollout_state_version BIGINT NOT NULL CHECK (rollout_state_version > 0),
    blocking_gate_codes JSONB NOT NULL,
    record_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('BLOCKED', 'STAGING_READY')),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_go_rollout_readiness_created
    ON go_rollout_readiness_records(created_at DESC, readiness_id);

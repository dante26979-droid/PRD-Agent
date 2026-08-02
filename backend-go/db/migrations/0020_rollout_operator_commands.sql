ALTER TABLE go_rollout_stage_state
    ADD COLUMN IF NOT EXISTS paused BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS pause_reason_code TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS rollback_policy_version TEXT REFERENCES go_rollout_policies(policy_version);

CREATE TABLE IF NOT EXISTS go_rollout_commands (
    command_id TEXT PRIMARY KEY,
    command_kind TEXT NOT NULL CHECK (command_kind IN (
        'PAUSE', 'RESUME', 'ROLLBACK_NEW_ASSIGNMENTS'
    )),
    request_hash TEXT NOT NULL,
    expected_state_version BIGINT NOT NULL CHECK (expected_state_version > 0),
    result_state_version BIGINT NOT NULL CHECK (result_state_version > 0),
    evidence_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    result_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_go_rollout_commands_created
    ON go_rollout_commands(created_at, command_id);

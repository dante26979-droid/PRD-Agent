ALTER TABLE go_rollout_gate_decisions
    ADD COLUMN IF NOT EXISTS dataset_hash TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS config_hash TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS gate_manifest_version TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS evidence_hash TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS go_rollout_stage_state (
    singleton_key TEXT PRIMARY KEY CHECK (singleton_key = 'agent-runtime'),
    stage TEXT NOT NULL CHECK (stage IN (
        'LOCAL_ONLY', 'LOCALLY_VERIFIED', 'STAGING_SHADOW', 'STAGING_ENFORCE',
        'PRODUCTION_CANARY', 'PRODUCTION_DEFAULT', 'LEGACY_DRAIN', 'LEGACY_RETIRED'
    )),
    policy_version TEXT NOT NULL,
    last_gate_decision_id TEXT REFERENCES go_rollout_gate_decisions(decision_id),
    evidence_hash TEXT NOT NULL,
    version BIGINT NOT NULL CHECK (version > 0),
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS go_legacy_inventory_snapshots (
    inventory_id TEXT PRIMARY KEY,
    workflow_version TEXT NOT NULL,
    counts_json JSONB NOT NULL,
    inventory_hash TEXT NOT NULL,
    observed_from TIMESTAMPTZ NOT NULL,
    observed_until TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    CHECK (observed_until >= observed_from)
);

CREATE TABLE IF NOT EXISTS go_legacy_reader_observations (
    observation_window TSTZRANGE NOT NULL,
    reader_kind TEXT NOT NULL,
    hit_count BIGINT NOT NULL CHECK (hit_count >= 0),
    evidence_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (observation_window, reader_kind)
);

ALTER TABLE go_rollout_drains
    ADD COLUMN IF NOT EXISTS inventory_id TEXT REFERENCES go_legacy_inventory_snapshots(inventory_id),
    ADD COLUMN IF NOT EXISTS non_terminal_ledger_count BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS unpublished_outbox_count BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS reader_hit_count BIGINT NOT NULL DEFAULT 0;

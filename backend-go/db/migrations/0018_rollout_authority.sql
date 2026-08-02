CREATE TABLE IF NOT EXISTS go_rollout_policies (
    policy_version TEXT PRIMARY KEY,
    policy_payload JSONB NOT NULL,
    policy_hash TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_go_rollout_policies_active
    ON go_rollout_policies(active) WHERE active;

ALTER TABLE go_agent_rollout_assignments
    ADD COLUMN IF NOT EXISTS assignment_hash TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS go_prd_outlines (
    outline_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES go_control_tasks(task_id),
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (task_id)
);

CREATE TABLE IF NOT EXISTS go_prd_outline_versions (
    outline_version_id TEXT PRIMARY KEY,
    outline_id TEXT NOT NULL REFERENCES go_prd_outlines(outline_id),
    version BIGINT NOT NULL CHECK (version > 0),
    status TEXT NOT NULL CHECK (status IN ('DRAFT', 'LOCKED')),
    payload JSONB NOT NULL,
    content_hash TEXT NOT NULL,
    source_run_id TEXT NOT NULL REFERENCES go_agent_runs(run_id),
    created_at TIMESTAMPTZ NOT NULL,
    locked_at TIMESTAMPTZ,
    UNIQUE (outline_id, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_go_prd_outline_locked
    ON go_prd_outline_versions(outline_id)
    WHERE status = 'LOCKED';

ALTER TABLE go_confirmation_unit_versions
    ALTER COLUMN draft_id DROP NOT NULL;

ALTER TABLE go_confirmation_unit_versions
    ADD COLUMN IF NOT EXISTS outline_version_id TEXT REFERENCES go_prd_outline_versions(outline_version_id),
    ADD COLUMN IF NOT EXISTS source_run_id TEXT REFERENCES go_agent_runs(run_id),
    ADD COLUMN IF NOT EXISTS unit_version_no BIGINT;

ALTER TABLE go_confirmation_units
    ADD COLUMN IF NOT EXISTS outline_version_id TEXT REFERENCES go_prd_outline_versions(outline_version_id),
    ADD COLUMN IF NOT EXISTS title TEXT,
    ADD COLUMN IF NOT EXISTS ordinal INTEGER,
    ADD COLUMN IF NOT EXISTS node_keys TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS depends_on TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS review_status TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_go_confirmation_unit_review_status'
    ) THEN
        ALTER TABLE go_confirmation_units
            ADD CONSTRAINT ck_go_confirmation_unit_review_status CHECK (
                review_status IS NULL OR review_status IN ('PENDING', 'REVIEWING', 'CONFIRMED', 'REOPENED')
            );
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_go_confirmation_version_source'
    ) THEN
        ALTER TABLE go_confirmation_unit_versions
            ADD CONSTRAINT ck_go_confirmation_version_source CHECK (
                (draft_id IS NOT NULL AND outline_version_id IS NULL)
                OR
                (draft_id IS NULL AND outline_version_id IS NOT NULL
                 AND source_run_id IS NOT NULL AND unit_version_no > 0)
            );
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS ux_go_confirmation_v4_version
    ON go_confirmation_unit_versions(unit_id, outline_version_id, unit_version_no)
    WHERE outline_version_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS go_full_review_reports (
    report_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES go_control_tasks(task_id),
    outline_version_id TEXT NOT NULL REFERENCES go_prd_outline_versions(outline_version_id),
    source_run_id TEXT NOT NULL REFERENCES go_agent_runs(run_id),
    payload JSONB NOT NULL,
    content_hash TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('PASSED', 'NEEDS_REVISION')),
    unit_hash_set_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_go_full_review_task
    ON go_full_review_reports(task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS go_review_transition_idempotency (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    transition_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, owner_id, operation, idempotency_key)
);

ALTER TABLE go_run_outputs
    ADD COLUMN IF NOT EXISTS materialized_kind TEXT,
    ADD COLUMN IF NOT EXISTS materialized_id TEXT,
    ADD COLUMN IF NOT EXISTS materialized_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS materialized_task_version INTEGER;

ALTER TABLE go_confirmation_decisions
    ADD COLUMN IF NOT EXISTS created_run_id TEXT REFERENCES go_agent_runs(run_id);

ALTER TABLE go_publish_intents
    ALTER COLUMN draft_id DROP NOT NULL;

ALTER TABLE go_publish_intents
    ADD COLUMN IF NOT EXISTS source_version_id TEXT,
    ADD COLUMN IF NOT EXISTS publish_content BYTEA;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_go_publish_intent_source'
    ) THEN
        ALTER TABLE go_publish_intents
            ADD CONSTRAINT ck_go_publish_intent_source CHECK (
                (draft_id IS NOT NULL AND source_version_id IS NULL AND publish_content IS NULL)
                OR
                (draft_id IS NULL AND source_version_id IS NOT NULL AND publish_content IS NOT NULL)
            );
    END IF;
END $$;

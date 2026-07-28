ALTER TABLE outline_nodes
    ADD COLUMN IF NOT EXISTS parent_id TEXT REFERENCES outline_nodes(node_id),
    ADD COLUMN IF NOT EXISTS level INTEGER NOT NULL DEFAULT 1 CHECK (level BETWEEN 1 AND 3),
    ADD COLUMN IF NOT EXISTS stable_key TEXT;

ALTER TABLE confirmation_units
    DROP CONSTRAINT IF EXISTS confirmation_units_status_check;

ALTER TABLE confirmation_units
    ADD CONSTRAINT confirmation_units_status_check CHECK (status IN (
        'PENDING', 'PREPARING', 'INVESTIGATING', 'GENERATING',
        'PENDING_CONFIRMATION', 'CONFIRMED', 'REVISION_REQUIRED',
        'HUMAN_INPUT_REQUIRED', 'SUPERSEDED', 'FAILED'
    ));

ALTER TABLE confirmation_units
    ADD COLUMN IF NOT EXISTS node_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS depends_on_unit_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS content_hash TEXT,
    ADD COLUMN IF NOT EXISTS grounding_run_id TEXT,
    ADD COLUMN IF NOT EXISTS quality_run_id TEXT,
    ADD COLUMN IF NOT EXISTS section_drafts_json JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE prd_section_versions
    ADD COLUMN IF NOT EXISTS node_id TEXT REFERENCES outline_nodes(node_id),
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'CONFIRMED',
    ADD COLUMN IF NOT EXISTS content_hash TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS source_run_id TEXT REFERENCES agent_runs(run_id),
    ADD COLUMN IF NOT EXISTS grounding_run_id TEXT,
    ADD COLUMN IF NOT EXISTS quality_run_id TEXT,
    ADD COLUMN IF NOT EXISTS supersedes_section_id TEXT REFERENCES prd_section_versions(section_id),
    ADD COLUMN IF NOT EXISTS confirmed_by TEXT,
    ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ;

ALTER TABLE prd_section_versions
    DROP CONSTRAINT IF EXISTS prd_section_versions_status_check;

ALTER TABLE prd_section_versions
    ADD CONSTRAINT prd_section_versions_status_check CHECK (status IN (
        'DRAFT', 'PENDING_CONFIRMATION', 'CONFIRMED', 'INVALIDATED', 'SUPERSEDED'
    ));

ALTER TABLE prd_document_versions
    ADD COLUMN IF NOT EXISTS content_hash TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'FINAL_REVIEW',
    ADD COLUMN IF NOT EXISTS section_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS confirmed_by TEXT,
    ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ;

ALTER TABLE prd_document_versions
    DROP CONSTRAINT IF EXISTS prd_document_versions_status_check;

ALTER TABLE prd_document_versions
    ADD CONSTRAINT prd_document_versions_status_check CHECK (status IN (
        'CHECKING', 'FINAL_REVIEW', 'CONFIRMED', 'SUPERSEDED'
    ));

CREATE TABLE IF NOT EXISTS grounding_results (
    grounding_run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES prd_tasks(task_id),
    unit_id TEXT NOT NULL REFERENCES confirmation_units(unit_id),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS quality_results (
    quality_run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES prd_tasks(task_id),
    scope_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_quality_results_task_scope
    ON quality_results(task_id, scope_id);

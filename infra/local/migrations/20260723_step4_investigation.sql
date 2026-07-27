CREATE TABLE IF NOT EXISTS information_needs (
    information_need_id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES prd_tasks(task_id),
    run_id TEXT REFERENCES agent_runs(run_id),
    unit_id TEXT REFERENCES confirmation_units(unit_id),
    trigger_stage TEXT NOT NULL,
    question TEXT NOT NULL,
    requiredness TEXT NOT NULL CHECK (requiredness IN ('NONE', 'OPTIONAL', 'REQUIRED')),
    source_types_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    required_coverage_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    fallback TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'PLANNED', 'SKIPPED', 'INVESTIGATING', 'SATISFIED', 'UNSATISFIED'
    )),
    planner_version TEXT NOT NULL,
    context_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

ALTER TABLE eval_run
    ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS investigations (
    investigation_id TEXT PRIMARY KEY,
    information_need_id TEXT NOT NULL REFERENCES information_needs(information_need_id),
    task_id TEXT REFERENCES prd_tasks(task_id),
    run_id TEXT REFERENCES agent_runs(run_id),
    unit_id TEXT REFERENCES confirmation_units(unit_id),
    repository_id TEXT NOT NULL,
    resolved_commit_sha CHAR(40) NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'PLANNED', 'RUNNING', 'COMPLETE', 'PARTIAL', 'EMPTY',
        'FAILED', 'CANCELLED', 'HUMAN_INPUT_REQUIRED'
    )),
    coverage_json JSONB NOT NULL,
    budget_json JSONB NOT NULL,
    iteration_count INTEGER NOT NULL DEFAULT 0 CHECK (iteration_count >= 0),
    tool_call_count INTEGER NOT NULL DEFAULT 0 CHECK (tool_call_count >= 0),
    replan_count INTEGER NOT NULL DEFAULT 0 CHECK (replan_count >= 0),
    no_progress_rounds INTEGER NOT NULL DEFAULT 0 CHECK (no_progress_rounds >= 0),
    token_usage INTEGER NOT NULL DEFAULT 0 CHECK (token_usage >= 0),
    completed_action_signatures_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    fact_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    unknown_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    conflict_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    stop_reason TEXT,
    policy_version TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_investigations_one_active_per_need
    ON investigations(information_need_id)
    WHERE status IN ('PLANNED', 'RUNNING');

CREATE TABLE IF NOT EXISTS investigation_steps (
    investigation_step_id TEXT PRIMARY KEY,
    investigation_id TEXT NOT NULL REFERENCES investigations(investigation_id),
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    iteration INTEGER NOT NULL CHECK (iteration >= 0),
    step_type TEXT NOT NULL,
    status TEXT NOT NULL,
    public_summary TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    output_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (investigation_id, sequence)
);

ALTER TABLE tool_calls
    ADD COLUMN IF NOT EXISTS investigation_id TEXT REFERENCES investigations(investigation_id),
    ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    ADD COLUMN IF NOT EXISTS retry_of_tool_call_id TEXT REFERENCES tool_calls(tool_call_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_investigation_successful_action
    ON tool_calls(investigation_id, action_signature)
    WHERE investigation_id IS NOT NULL
      AND status IN ('SUCCEEDED', 'PARTIAL', 'EMPTY');

ALTER TABLE verified_facts
    ADD COLUMN IF NOT EXISTS investigation_id TEXT REFERENCES investigations(investigation_id);

ALTER TABLE unknown_items
    ADD COLUMN IF NOT EXISTS investigation_id TEXT REFERENCES investigations(investigation_id);

ALTER TABLE source_conflicts
    ADD COLUMN IF NOT EXISTS investigation_id TEXT REFERENCES investigations(investigation_id);

CREATE TABLE go_memory_spaces (
    space_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    project_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','ARCHIVED')),
    memory_epoch BIGINT NOT NULL DEFAULT 0 CHECK (memory_epoch >= 0),
    policy_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (tenant_id, owner_id, project_key)
);

CREATE TABLE go_task_memory_bindings (
    task_id TEXT PRIMARY KEY REFERENCES go_control_tasks(task_id) ON DELETE CASCADE,
    space_id TEXT NOT NULL REFERENCES go_memory_spaces(space_id),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE go_run_memory_assignments (
    run_id TEXT PRIMARY KEY REFERENCES go_agent_runs(run_id) ON DELETE CASCADE,
    space_id TEXT NOT NULL REFERENCES go_memory_spaces(space_id),
    memory_watermark BIGINT NOT NULL CHECK (memory_watermark >= 0),
    policy_version TEXT NOT NULL,
    access_scope_hash TEXT NOT NULL,
    assignment_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE go_project_memory_records (
    memory_id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL REFERENCES go_memory_spaces(space_id),
    semantic_key TEXT NOT NULL,
    current_version BIGINT NOT NULL CHECK (current_version > 0),
    current_status TEXT NOT NULL CHECK (current_status IN ('ACTIVE','SUPERSEDED','REVOKED','STALE')),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_go_project_memory_records_space_status
    ON go_project_memory_records(space_id, current_status);
CREATE INDEX idx_go_project_memory_records_semantic
    ON go_project_memory_records(space_id, semantic_key);

CREATE TABLE go_project_memory_versions (
    memory_id TEXT NOT NULL REFERENCES go_project_memory_records(memory_id) ON DELETE CASCADE,
    version BIGINT NOT NULL CHECK (version > 0),
    schema_version TEXT NOT NULL,
    space_id TEXT NOT NULL REFERENCES go_memory_spaces(space_id),
    memory_type TEXT NOT NULL CHECK (memory_type IN ('DOMAIN_TERM','PROJECT_DECISION','PROJECT_CONSTRAINT','WORKFLOW_PREFERENCE','ACCEPTANCE_PATTERN','SOURCE_POINTER','OPEN_QUESTION','RISK_NOTE')),
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    value_json JSONB NOT NULL,
    statement TEXT NOT NULL,
    authority_class TEXT NOT NULL CHECK (authority_class IN ('USER_CONFIRMED','SOURCE_VERIFIED','WORKFLOW_CONFIRMED','DERIVED_PROPOSAL')),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','SUPERSEDED','REVOKED','STALE')),
    tags TEXT[] NOT NULL DEFAULT '{}',
    sensitivity TEXT NOT NULL,
    source_revision_set_hash TEXT NOT NULL,
    source_refs_json JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(source_refs_json) = 'array'),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ,
    committed_epoch BIGINT NOT NULL CHECK (committed_epoch > 0),
    content_hash TEXT NOT NULL,
    created_by_kind TEXT NOT NULL,
    created_by_ref TEXT NOT NULL,
    search_document TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (memory_id, version),
    UNIQUE (space_id, committed_epoch)
);

CREATE INDEX idx_go_project_memory_versions_watermark
    ON go_project_memory_versions(space_id, committed_epoch DESC);
CREATE INDEX idx_go_project_memory_versions_search
    ON go_project_memory_versions USING GIN (to_tsvector('simple', search_document));
CREATE INDEX idx_go_project_memory_versions_tags
    ON go_project_memory_versions USING GIN (tags);

CREATE TABLE go_project_memory_candidates (
    candidate_id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL REFERENCES go_memory_spaces(space_id),
    memory_type TEXT NOT NULL CHECK (memory_type IN ('DOMAIN_TERM','PROJECT_DECISION','PROJECT_CONSTRAINT','WORKFLOW_PREFERENCE','ACCEPTANCE_PATTERN','SOURCE_POINTER','OPEN_QUESTION','RISK_NOTE')),
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    value_json JSONB NOT NULL,
    statement TEXT NOT NULL,
    authority_class TEXT NOT NULL CHECK (authority_class IN ('USER_CONFIRMED','SOURCE_VERIFIED','WORKFLOW_CONFIRMED','DERIVED_PROPOSAL')),
    tags TEXT[] NOT NULL DEFAULT '{}',
    sensitivity TEXT NOT NULL,
    source_refs_json JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(source_refs_json) = 'array'),
    reason_code TEXT NOT NULL,
    proposed_by_run_id TEXT,
    extractor_version TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING_REVIEW','CONFIRMED','REJECTED','EXPIRED')),
    created_at TIMESTAMPTZ NOT NULL,
    reviewed_at TIMESTAMPTZ,
    reviewed_by TEXT
);

CREATE INDEX idx_go_project_memory_candidates_review
    ON go_project_memory_candidates(space_id, status, created_at);

CREATE TABLE go_project_memory_conflicts (
    conflict_id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL REFERENCES go_memory_spaces(space_id),
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    memory_ids TEXT[] NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OPEN','RESOLVED')),
    committed_epoch BIGINT NOT NULL,
    resolution_memory_id TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    resolved_at TIMESTAMPTZ,
    resolved_epoch BIGINT CHECK (resolved_epoch IS NULL OR resolved_epoch > 0)
);

CREATE INDEX idx_go_project_memory_conflicts_open
    ON go_project_memory_conflicts(space_id, committed_epoch) WHERE status = 'OPEN';

CREATE TABLE go_project_memory_events (
    event_id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL REFERENCES go_memory_spaces(space_id),
    event_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_go_project_memory_events_space
    ON go_project_memory_events(space_id, occurred_at, event_id);

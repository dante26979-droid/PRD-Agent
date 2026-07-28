CREATE TABLE IF NOT EXISTS eval_dataset (
    dataset_version TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL,
    resolved_commit_sha CHAR(40) NOT NULL,
    case_count INTEGER NOT NULL CHECK (case_count >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS eval_case (
    dataset_version TEXT NOT NULL REFERENCES eval_dataset(dataset_version),
    case_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (dataset_version, case_id)
);

CREATE TABLE IF NOT EXISTS eval_run (
    eval_run_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    config_id TEXT NOT NULL,
    trial_no INTEGER NOT NULL CHECK (trial_no >= 1),
    model_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    repository_commit CHAR(40) NOT NULL,
    input_hash TEXT NOT NULL,
    output_hash TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    token_usage JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
    output TEXT,
    error TEXT,
    UNIQUE (config_id, case_id, trial_no)
);

CREATE TABLE IF NOT EXISTS eval_metric (
    eval_run_id TEXT NOT NULL REFERENCES eval_run(eval_run_id),
    metric_name TEXT NOT NULL,
    value DOUBLE PRECISION,
    status TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (eval_run_id, metric_name)
);

CREATE TABLE IF NOT EXISTS eval_artifact (
    artifact_id BIGSERIAL PRIMARY KEY,
    eval_run_id TEXT REFERENCES eval_run(eval_run_id),
    artifact_type TEXT NOT NULL,
    path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS prd_tasks (
    task_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL DEFAULT 'local-user',
    title TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'DRAFT', 'CLARIFYING', 'OUTLINE_REVIEW', 'GENERATING',
        'FINAL_REVIEW', 'COMPLETED', 'FAILED', 'STOPPED', 'DELETING'
    )),
    version INTEGER NOT NULL CHECK (version >= 1),
    current_outline_version INTEGER,
    current_unit_sequence INTEGER,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_prd_tasks_owner_updated
    ON prd_tasks(owner_id, updated_at DESC, task_id DESC);

CREATE TABLE IF NOT EXISTS task_messages (
    message_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES prd_tasks(task_id),
    actor_id TEXT NOT NULL,
    content TEXT NOT NULL CHECK (length(btrim(content)) > 0),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS requirement_brief_versions (
    task_id TEXT NOT NULL REFERENCES prd_tasks(task_id),
    version INTEGER NOT NULL CHECK (version >= 1),
    payload JSONB NOT NULL,
    confirmed BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (task_id, version)
);

CREATE TABLE IF NOT EXISTS outline_versions (
    outline_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES prd_tasks(task_id),
    version INTEGER NOT NULL CHECK (version >= 1),
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'DRAFT', 'PENDING_CONFIRMATION', 'CONFIRMED', 'SUPERSEDED'
    )),
    created_at TIMESTAMPTZ NOT NULL,
    confirmed_at TIMESTAMPTZ,
    UNIQUE (task_id, version)
);

CREATE TABLE IF NOT EXISTS outline_nodes (
    node_id TEXT PRIMARY KEY,
    outline_id TEXT NOT NULL REFERENCES outline_versions(outline_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    title TEXT NOT NULL,
    purpose TEXT NOT NULL,
    complexity TEXT NOT NULL CHECK (complexity IN ('LOW', 'MEDIUM', 'HIGH')),
    required_information JSONB NOT NULL DEFAULT '[]'::jsonb,
    parent_id TEXT REFERENCES outline_nodes(node_id),
    level INTEGER NOT NULL DEFAULT 1 CHECK (level BETWEEN 1 AND 3),
    stable_key TEXT,
    UNIQUE (outline_id, sequence)
);

CREATE TABLE IF NOT EXISTS confirmation_units (
    unit_id TEXT PRIMARY KEY,
    outline_id TEXT NOT NULL REFERENCES outline_versions(outline_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'PREPARING', 'INVESTIGATING', 'GENERATING',
        'PENDING_CONFIRMATION', 'CONFIRMED', 'REVISION_REQUIRED',
        'HUMAN_INPUT_REQUIRED', 'SUPERSEDED', 'FAILED'
    )),
    content TEXT,
    version INTEGER NOT NULL CHECK (version >= 1),
    node_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    depends_on_unit_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    content_hash TEXT,
    grounding_run_id TEXT,
    quality_run_id TEXT,
    section_drafts_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    UNIQUE (outline_id, sequence)
);

CREATE TABLE IF NOT EXISTS prd_section_versions (
    section_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES prd_tasks(task_id),
    unit_id TEXT NOT NULL REFERENCES confirmation_units(unit_id),
    version INTEGER NOT NULL CHECK (version >= 1),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    node_id TEXT REFERENCES outline_nodes(node_id),
    status TEXT NOT NULL DEFAULT 'CONFIRMED' CHECK (status IN (
        'DRAFT', 'PENDING_CONFIRMATION', 'CONFIRMED', 'INVALIDATED', 'SUPERSEDED'
    )),
    content_hash TEXT NOT NULL DEFAULT '',
    source_run_id TEXT,
    grounding_run_id TEXT,
    quality_run_id TEXT,
    supersedes_section_id TEXT REFERENCES prd_section_versions(section_id),
    confirmed_by TEXT,
    confirmed_at TIMESTAMPTZ,
    UNIQUE (unit_id, version)
);

CREATE TABLE IF NOT EXISTS prd_document_versions (
    document_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES prd_tasks(task_id),
    version INTEGER NOT NULL CHECK (version >= 1),
    markdown TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    content_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'FINAL_REVIEW' CHECK (status IN (
        'CHECKING', 'FINAL_REVIEW', 'CONFIRMED', 'SUPERSEDED'
    )),
    section_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    confirmed_by TEXT,
    confirmed_at TIMESTAMPTZ,
    UNIQUE (task_id, version)
);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES prd_tasks(task_id),
    thread_id TEXT NOT NULL,
    graph_name TEXT NOT NULL,
    graph_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'QUEUED', 'RUNNING', 'WAITING_USER', 'SUCCEEDED', 'FAILED', 'STOPPED'
    )),
    input_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    error TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_runs_one_active_per_task
    ON agent_runs(task_id)
    WHERE status IN ('QUEUED', 'RUNNING', 'WAITING_USER');

CREATE TABLE IF NOT EXISTS idempotency_records (
    actor_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES prd_tasks(task_id),
    run_id TEXT NOT NULL REFERENCES agent_runs(run_id),
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (actor_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS domain_events (
    event_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES prd_tasks(task_id),
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (task_id, sequence)
);

CREATE TABLE IF NOT EXISTS workflow_checkpoints (
    thread_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES prd_tasks(task_id),
    graph_version TEXT NOT NULL,
    task_version INTEGER NOT NULL,
    state JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS repository_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL,
    resolved_commit_sha CHAR(40) NOT NULL,
    allowed_prefix TEXT NOT NULL,
    resolver_version TEXT NOT NULL,
    resolved_at TIMESTAMPTZ NOT NULL,
    UNIQUE (repository_id, resolved_commit_sha, allowed_prefix)
);

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

CREATE TABLE IF NOT EXISTS tool_calls (
    tool_call_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    task_id TEXT REFERENCES prd_tasks(task_id),
    run_id TEXT REFERENCES agent_runs(run_id),
    unit_id TEXT REFERENCES confirmation_units(unit_id),
    investigation_id TEXT REFERENCES investigations(investigation_id),
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    retry_of_tool_call_id TEXT REFERENCES tool_calls(tool_call_id),
    repository_id TEXT NOT NULL,
    resolved_commit_sha CHAR(40) NOT NULL,
    tool_id TEXT NOT NULL,
    tool_schema_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    purpose TEXT NOT NULL,
    arguments_json JSONB NOT NULL,
    action_signature TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'RUNNING', 'SUCCEEDED', 'PARTIAL', 'EMPTY', 'FAILED', 'BLOCKED'
    )),
    public_summary TEXT NOT NULL DEFAULT '',
    error_code TEXT,
    call_json JSONB NOT NULL,
    tool_result_json JSONB,
    bundle_json JSONB,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_tool_calls_action_signature
    ON tool_calls(action_signature);

CREATE UNIQUE INDEX IF NOT EXISTS uq_investigation_successful_action
    ON tool_calls(investigation_id, action_signature)
    WHERE investigation_id IS NOT NULL
      AND status IN ('SUCCEEDED', 'PARTIAL', 'EMPTY');

CREATE TABLE IF NOT EXISTS tool_idempotency_records (
    actor_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    tool_call_id TEXT NOT NULL REFERENCES tool_calls(tool_call_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (actor_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS source_evidence (
    evidence_id TEXT PRIMARY KEY,
    tool_call_id TEXT NOT NULL REFERENCES tool_calls(tool_call_id),
    source_type TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    resolved_commit_sha CHAR(40) NOT NULL,
    path TEXT NOT NULL,
    line_start INTEGER,
    line_end INTEGER,
    symbol TEXT,
    excerpt TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source_blob_id TEXT,
    extraction_method TEXT NOT NULL,
    redaction_applied BOOLEAN NOT NULL DEFAULT FALSE,
    retrieved_at TIMESTAMPTZ NOT NULL,
    CHECK (line_start IS NULL OR line_start >= 1),
    CHECK (line_end IS NULL OR line_end >= line_start)
);

CREATE TABLE IF NOT EXISTS verified_facts (
    fact_id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES prd_tasks(task_id),
    tool_call_id TEXT NOT NULL REFERENCES tool_calls(tool_call_id),
    investigation_id TEXT REFERENCES investigations(investigation_id),
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    value_json JSONB NOT NULL,
    fact_scope TEXT NOT NULL,
    fact_type TEXT NOT NULL,
    confidence TEXT NOT NULL,
    verification_status TEXT NOT NULL,
    extractor_id TEXT NOT NULL,
    extractor_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_evidence_links (
    fact_id TEXT NOT NULL REFERENCES verified_facts(fact_id),
    evidence_id TEXT NOT NULL REFERENCES source_evidence(evidence_id),
    support_type TEXT NOT NULL DEFAULT 'DIRECT',
    PRIMARY KEY (fact_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS unknown_items (
    unknown_id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES prd_tasks(task_id),
    tool_call_id TEXT NOT NULL REFERENCES tool_calls(tool_call_id),
    investigation_id TEXT REFERENCES investigations(investigation_id),
    statement TEXT NOT NULL,
    reason TEXT NOT NULL,
    severity TEXT NOT NULL,
    resolution_type TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS source_conflicts (
    conflict_id TEXT PRIMARY KEY,
    investigation_id TEXT REFERENCES investigations(investigation_id),
    subject TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS source_conflict_facts (
    conflict_id TEXT NOT NULL REFERENCES source_conflicts(conflict_id),
    fact_id TEXT NOT NULL REFERENCES verified_facts(fact_id),
    PRIMARY KEY (conflict_id, fact_id)
);

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

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE tool_calls
    ADD COLUMN IF NOT EXISTS source_kind TEXT NOT NULL DEFAULT 'CODE_REPOSITORY',
    ADD COLUMN IF NOT EXISTS source_id TEXT,
    ADD COLUMN IF NOT EXISTS source_version TEXT,
    ADD COLUMN IF NOT EXISTS source_binding_json JSONB,
    ADD COLUMN IF NOT EXISTS access_scope_hash TEXT,
    ALTER COLUMN repository_id DROP NOT NULL,
    ALTER COLUMN resolved_commit_sha DROP NOT NULL;

UPDATE tool_calls
   SET source_id = repository_id, source_version = resolved_commit_sha
 WHERE source_id IS NULL OR source_version IS NULL;

ALTER TABLE source_evidence
    ADD COLUMN IF NOT EXISTS source_kind TEXT NOT NULL DEFAULT 'CODE_REPOSITORY',
    ADD COLUMN IF NOT EXISTS source_id TEXT,
    ADD COLUMN IF NOT EXISTS source_version TEXT,
    ADD COLUMN IF NOT EXISTS locator_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS access_scope_hash TEXT,
    ALTER COLUMN repository_id DROP NOT NULL,
    ALTER COLUMN resolved_commit_sha DROP NOT NULL,
    ALTER COLUMN path DROP NOT NULL;

UPDATE source_evidence
   SET source_id = repository_id,
       source_version = resolved_commit_sha,
       locator_json = jsonb_strip_nulls(jsonb_build_object(
           'path', path, 'line_start', line_start,
           'line_end', line_end, 'symbol', symbol
       ))
 WHERE source_id IS NULL OR source_version IS NULL;

ALTER TABLE tool_calls ADD CONSTRAINT ck_tool_calls_source_contract CHECK (
    source_id IS NOT NULL AND source_version IS NOT NULL
    AND (
        source_kind <> 'CODE_REPOSITORY'
        OR (repository_id IS NOT NULL AND resolved_commit_sha IS NOT NULL)
    )
);

ALTER TABLE source_evidence
    ADD CONSTRAINT ck_source_evidence_source_contract CHECK (
        source_id IS NOT NULL AND source_version IS NOT NULL
        AND (
            source_kind <> 'CODE_REPOSITORY'
            OR (
                repository_id IS NOT NULL
                AND resolved_commit_sha IS NOT NULL
                AND path IS NOT NULL
            )
        )
    );

CREATE TABLE IF NOT EXISTS historical_corpora (
    corpus_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    corpus_version TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    document_count INTEGER NOT NULL CHECK (document_count >= 0),
    chunk_count INTEGER NOT NULL CHECK (chunk_count >= 0),
    tokenizer_version TEXT NOT NULL,
    embedding_model_id TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('BUILDING', 'READY', 'FAILED', 'SUPERSEDED')
    ),
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (corpus_id, corpus_version)
);

CREATE TABLE IF NOT EXISTS historical_prd_documents (
    document_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    access_labels TEXT[] NOT NULL DEFAULT '{}',
    product_tags TEXT[] NOT NULL DEFAULT '{}',
    created_at_source TIMESTAMPTZ,
    updated_at_source TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'ARCHIVED', 'DELETED'))
);

CREATE INDEX IF NOT EXISTS ix_historical_documents_access
    ON historical_prd_documents(owner_id, project_id, status);

CREATE TABLE IF NOT EXISTS historical_prd_versions (
    document_version_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES historical_prd_documents(document_id),
    source_revision TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    markdown TEXT NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL,
    supersedes_version_id TEXT REFERENCES historical_prd_versions(document_version_id),
    UNIQUE (document_id, content_hash)
);

CREATE TABLE IF NOT EXISTS historical_prd_chunks (
    chunk_id TEXT PRIMARY KEY,
    document_version_id TEXT NOT NULL
        REFERENCES historical_prd_versions(document_version_id),
    section_path TEXT[] NOT NULL DEFAULT '{}',
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    content TEXT NOT NULL CHECK (length(content) BETWEEN 1 AND 1200),
    content_hash TEXT NOT NULL,
    token_text TEXT NOT NULL,
    token_count INTEGER NOT NULL CHECK (token_count >= 0),
    UNIQUE (document_version_id, ordinal)
);

CREATE INDEX IF NOT EXISTS ix_historical_chunks_fts
    ON historical_prd_chunks
    USING GIN (to_tsvector('simple', token_text));

CREATE TABLE IF NOT EXISTS historical_corpus_documents (
    corpus_id TEXT NOT NULL,
    corpus_version TEXT NOT NULL,
    document_version_id TEXT NOT NULL
        REFERENCES historical_prd_versions(document_version_id),
    document_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    access_labels TEXT[] NOT NULL DEFAULT '{}',
    product_tags TEXT[] NOT NULL DEFAULT '{}',
    created_at_source TIMESTAMPTZ,
    updated_at_source TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'ARCHIVED', 'DELETED')),
    PRIMARY KEY (corpus_id, corpus_version, document_version_id),
    FOREIGN KEY (corpus_id, corpus_version)
        REFERENCES historical_corpora(corpus_id, corpus_version)
);

CREATE INDEX IF NOT EXISTS ix_historical_corpus_documents_access
    ON historical_corpus_documents(
        corpus_id, corpus_version, owner_id, project_id, status
    );

CREATE TABLE IF NOT EXISTS historical_corpus_chunks (
    corpus_id TEXT NOT NULL,
    corpus_version TEXT NOT NULL,
    chunk_id TEXT NOT NULL REFERENCES historical_prd_chunks(chunk_id),
    PRIMARY KEY (corpus_id, corpus_version, chunk_id),
    FOREIGN KEY (corpus_id, corpus_version)
        REFERENCES historical_corpora(corpus_id, corpus_version)
);

CREATE TABLE IF NOT EXISTS retrieval_runs (
    retrieval_run_id TEXT PRIMARY KEY,
    investigation_id TEXT NOT NULL,
    corpus_id TEXT NOT NULL,
    corpus_version TEXT NOT NULL,
    query_hash TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('KEYWORD', 'HYBRID')),
    filters_hash TEXT NOT NULL,
    result_limit INTEGER NOT NULL CHECK (result_limit BETWEEN 1 AND 5),
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    status TEXT NOT NULL CHECK (
        status IN ('SUCCEEDED', 'EMPTY', 'FAILED', 'BLOCKED')
    ),
    fallback_mode TEXT CHECK (fallback_mode IN ('KEYWORD', 'HYBRID')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (corpus_id, corpus_version)
        REFERENCES historical_corpora(corpus_id, corpus_version)
);

CREATE INDEX IF NOT EXISTS ix_retrieval_runs_investigation
    ON retrieval_runs(investigation_id, created_at);

CREATE TABLE IF NOT EXISTS retrieval_hits (
    retrieval_run_id TEXT NOT NULL REFERENCES retrieval_runs(retrieval_run_id),
    chunk_id TEXT NOT NULL REFERENCES historical_prd_chunks(chunk_id),
    rank INTEGER NOT NULL CHECK (rank BETWEEN 1 AND 5),
    keyword_rank INTEGER CHECK (keyword_rank >= 1),
    vector_rank INTEGER CHECK (vector_rank >= 1),
    fused_score DOUBLE PRECISION,
    stale_hint BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (retrieval_run_id, rank),
    UNIQUE (retrieval_run_id, chunk_id)
);

CREATE TABLE IF NOT EXISTS investigation_source_bindings (
    investigation_id TEXT NOT NULL REFERENCES investigations(investigation_id),
    binding_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_version TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    access_scope_hash TEXT NOT NULL,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (investigation_id, binding_id)
);

INSERT INTO schema_migrations(version)
VALUES ('20260727_step8_historical_prd_rag')
ON CONFLICT (version) DO NOTHING;

CREATE TABLE IF NOT EXISTS external_connections (
    connection_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (
        provider IN ('GITHUB', 'GITLAB', 'FEISHU', 'EXTERNAL_CODING_AGENT')
    ),
    credential_ref TEXT NOT NULL,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('ACTIVE', 'DISABLED', 'REAUTH_REQUIRED')
    ),
    allowed_resources_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_external_connections_owner_provider
    ON external_connections(owner_id, provider, status);

CREATE TABLE IF NOT EXISTS remote_repository_bindings (
    binding_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    connection_id TEXT NOT NULL REFERENCES external_connections(connection_id),
    provider TEXT NOT NULL CHECK (provider IN ('GITHUB', 'GITLAB')),
    provider_repository_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    default_revision TEXT NOT NULL,
    allowed_prefix TEXT NOT NULL DEFAULT '',
    access_scope_hash TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (owner_id, connection_id, provider_repository_id)
);

CREATE TABLE IF NOT EXISTS repository_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    binding_id TEXT NOT NULL REFERENCES remote_repository_bindings(binding_id),
    resolved_commit_sha CHAR(40) NOT NULL,
    resolved_from_ref TEXT NOT NULL,
    resolver_version TEXT NOT NULL,
    resolved_at TIMESTAMPTZ NOT NULL,
    UNIQUE (binding_id, resolved_commit_sha)
);

CREATE TABLE IF NOT EXISTS external_document_bindings (
    binding_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES prd_tasks(task_id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider = 'FEISHU'),
    external_id_ciphertext TEXT NOT NULL,
    safe_url TEXT NOT NULL,
    display_title TEXT NOT NULL,
    last_export_hash TEXT NOT NULL,
    last_document_version INTEGER NOT NULL CHECK (last_document_version >= 1),
    provider_revision TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (owner_id, task_id, provider)
);

CREATE TABLE IF NOT EXISTS export_intents (
    intent_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES prd_tasks(task_id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK (mode IN ('CREATE', 'OVERWRITE_BOUND')),
    task_version INTEGER NOT NULL CHECK (task_version >= 1),
    document_id TEXT NOT NULL,
    document_version INTEGER NOT NULL CHECK (document_version >= 1),
    content_hash TEXT NOT NULL,
    preview_title TEXT NOT NULL,
    unresolved_items_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    bound_title TEXT,
    bound_safe_url TEXT,
    idempotency_key_hash TEXT,
    idempotency_input_hash TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (owner_id, idempotency_key_hash)
);

CREATE INDEX IF NOT EXISTS ix_export_intents_task
    ON export_intents(owner_id, task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS export_runs (
    export_run_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES export_intents(intent_id),
    owner_id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES prd_tasks(task_id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK (mode IN ('CREATE', 'OVERWRITE_BOUND')),
    task_version INTEGER NOT NULL CHECK (task_version >= 1),
    document_version INTEGER NOT NULL CHECK (document_version >= 1),
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'RUNNING', 'SUCCEEDED', 'FAILED', 'RESULT_UNKNOWN', 'MANUAL_REVIEW'
        )
    ),
    idempotency_key_hash TEXT NOT NULL,
    idempotency_input_hash TEXT,
    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 1),
    binding_id TEXT REFERENCES external_document_bindings(binding_id),
    safe_url TEXT,
    display_title TEXT,
    error_code TEXT,
    retryable BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    UNIQUE (owner_id, idempotency_key_hash)
);

CREATE INDEX IF NOT EXISTS ix_export_runs_task
    ON export_runs(owner_id, task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS external_investigation_runs (
    external_run_id TEXT PRIMARY KEY,
    investigation_id TEXT NOT NULL REFERENCES investigations(investigation_id),
    profile TEXT NOT NULL CHECK (
        profile IN ('NATIVE', 'EXTERNAL_CODING_AGENT')
    ),
    provider TEXT,
    repository_id TEXT NOT NULL,
    resolved_commit_sha CHAR(40) NOT NULL,
    status TEXT NOT NULL,
    candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
    accepted_count INTEGER NOT NULL DEFAULT 0 CHECK (accepted_count >= 0),
    rejected_count INTEGER NOT NULL DEFAULT 0 CHECK (rejected_count >= 0),
    usage_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS integration_call_attempts (
    attempt_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    task_id TEXT,
    investigation_id TEXT,
    export_run_id TEXT REFERENCES export_runs(export_run_id),
    provider TEXT NOT NULL,
    operation TEXT NOT NULL,
    target_binding_id TEXT,
    request_hash TEXT NOT NULL,
    idempotency_key_hash TEXT,
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    status TEXT NOT NULL,
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_integration_attempts_task
    ON integration_call_attempts(owner_id, task_id, created_at DESC);

INSERT INTO schema_migrations(version)
VALUES ('20260727_step9_external_integrations')
ON CONFLICT (version) DO NOTHING;

CREATE TABLE IF NOT EXISTS tenants (
    tenant_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'DISABLED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'DISABLED')),
    display_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (tenant_id, user_id)
);

CREATE TABLE IF NOT EXISTS user_identities (
    identity_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    user_id TEXT NOT NULL REFERENCES users(user_id),
    issuer_hash TEXT NOT NULL,
    subject_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (tenant_id, issuer_hash, subject_hash)
);

CREATE TABLE IF NOT EXISTS production_run_control (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'local',
    owner_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'QUEUED', 'RUNNING', 'WAITING_USER', 'SUCCEEDED',
        'FAILED', 'STOPPING', 'STOPPED'
    )),
    fencing_token BIGINT NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    cancellation_requested_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_production_run_owner
    ON production_run_control(tenant_id, owner_id, task_id);

CREATE INDEX IF NOT EXISTS ix_production_run_recovery
    ON production_run_control(status, lease_expires_at)
    WHERE status IN ('QUEUED', 'RUNNING', 'STOPPING');

CREATE TABLE IF NOT EXISTS outbox_messages (
    message_id TEXT PRIMARY KEY,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    aggregate_version INTEGER NOT NULL CHECK (aggregate_version >= 1),
    topic TEXT NOT NULL,
    payload_version INTEGER NOT NULL CHECK (payload_version >= 1),
    payload JSONB NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    published_at TIMESTAMPTZ,
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    quarantined_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (aggregate_type, aggregate_id, aggregate_version)
);

CREATE INDEX IF NOT EXISTS ix_outbox_publishable
    ON outbox_messages(available_at, message_id)
    WHERE published_at IS NULL AND quarantined_at IS NULL;

CREATE TABLE IF NOT EXISTS inbox_receipts (
    consumer_name TEXT NOT NULL,
    message_id TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (consumer_name, message_id)
);

CREATE TABLE IF NOT EXISTS oauth_authorization_transactions (
    transaction_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    state_hash TEXT NOT NULL UNIQUE,
    pkce_verifier_ref TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS oauth_refresh_leases (
    connection_id TEXT PRIMARY KEY,
    lease_owner TEXT NOT NULL,
    fencing_token BIGINT NOT NULL CHECK (fencing_token >= 1),
    expires_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS provider_webhook_deliveries (
    provider TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (provider, delivery_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    audit_event_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    principal_hash TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    result TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_audit_events_target
    ON audit_events(tenant_id, target_type, target_id, occurred_at DESC);

INSERT INTO schema_migrations(version)
VALUES ('20260727_step10_production_profile')
ON CONFLICT (version) DO NOTHING;

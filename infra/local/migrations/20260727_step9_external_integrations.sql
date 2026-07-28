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

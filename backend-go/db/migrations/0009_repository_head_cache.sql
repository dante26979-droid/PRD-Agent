CREATE TABLE IF NOT EXISTS go_repository_heads (
    binding_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    default_branch TEXT NOT NULL,
    head_revision TEXT NOT NULL,
    last_success_at TIMESTAMPTZ NOT NULL,
    last_attempt_at TIMESTAMPTZ NOT NULL,
    last_error_code TEXT,
    CHECK (binding_id <> ''),
    CHECK (repository ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
    CHECK (default_branch <> ''),
    CHECK (head_revision ~ '^[0-9a-f]{40}$')
);

CREATE INDEX IF NOT EXISTS ix_go_repository_heads_last_success
    ON go_repository_heads(last_success_at);

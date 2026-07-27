CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE tool_calls
    ADD COLUMN IF NOT EXISTS source_kind TEXT NOT NULL DEFAULT 'CODE_REPOSITORY',
    ADD COLUMN IF NOT EXISTS source_id TEXT,
    ADD COLUMN IF NOT EXISTS source_version TEXT,
    ADD COLUMN IF NOT EXISTS source_binding_json JSONB,
    ADD COLUMN IF NOT EXISTS access_scope_hash TEXT;

UPDATE tool_calls
   SET source_id = repository_id,
       source_version = resolved_commit_sha
 WHERE source_kind = 'CODE_REPOSITORY'
   AND (source_id IS NULL OR source_version IS NULL);

ALTER TABLE tool_calls
    ALTER COLUMN repository_id DROP NOT NULL,
    ALTER COLUMN resolved_commit_sha DROP NOT NULL;

ALTER TABLE source_evidence
    ADD COLUMN IF NOT EXISTS source_kind TEXT NOT NULL DEFAULT 'CODE_REPOSITORY',
    ADD COLUMN IF NOT EXISTS source_id TEXT,
    ADD COLUMN IF NOT EXISTS source_version TEXT,
    ADD COLUMN IF NOT EXISTS locator_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS access_scope_hash TEXT;

UPDATE source_evidence
   SET source_id = repository_id,
       source_version = resolved_commit_sha,
       locator_json = jsonb_strip_nulls(jsonb_build_object(
           'path', path,
           'line_start', line_start,
           'line_end', line_end,
           'symbol', symbol
       ))
 WHERE source_kind = 'CODE_REPOSITORY'
   AND (source_id IS NULL OR source_version IS NULL);

ALTER TABLE source_evidence
    ALTER COLUMN repository_id DROP NOT NULL,
    ALTER COLUMN resolved_commit_sha DROP NOT NULL,
    ALTER COLUMN path DROP NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_tool_calls_source_contract'
    ) THEN
        ALTER TABLE tool_calls ADD CONSTRAINT ck_tool_calls_source_contract CHECK (
            source_id IS NOT NULL
            AND source_version IS NOT NULL
            AND (
                source_kind <> 'CODE_REPOSITORY'
                OR (repository_id IS NOT NULL AND resolved_commit_sha IS NOT NULL)
            )
        );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_source_evidence_source_contract'
    ) THEN
        ALTER TABLE source_evidence
            ADD CONSTRAINT ck_source_evidence_source_contract CHECK (
                source_id IS NOT NULL
                AND source_version IS NOT NULL
                AND (
                    source_kind <> 'CODE_REPOSITORY'
                    OR (
                        repository_id IS NOT NULL
                        AND resolved_commit_sha IS NOT NULL
                        AND path IS NOT NULL
                    )
                )
            );
    END IF;
END $$;

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

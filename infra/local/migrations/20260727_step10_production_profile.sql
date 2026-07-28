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

ALTER TABLE export_intents
    ADD COLUMN IF NOT EXISTS claimed_by TEXT;

ALTER TABLE export_intents
    ADD COLUMN IF NOT EXISTS claim_expires_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ix_export_intents_active_claim
    ON export_intents(owner_id, intent_id, claim_expires_at)
    WHERE consumed_at IS NULL;

CREATE TABLE IF NOT EXISTS command_idempotency (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    idempotency_key_hash TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (
        tenant_id, owner_id, operation, idempotency_key_hash
    )
);

INSERT INTO schema_migrations(version)
VALUES ('20260728_server_deployment_remediation')
ON CONFLICT (version) DO NOTHING;

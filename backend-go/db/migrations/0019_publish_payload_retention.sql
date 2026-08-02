ALTER TABLE go_publish_intents
    DROP CONSTRAINT IF EXISTS ck_go_publish_intent_source;

ALTER TABLE go_publish_intents
    ADD COLUMN IF NOT EXISTS payload_purged_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS retention_claim_worker TEXT,
    ADD COLUMN IF NOT EXISTS retention_claim_expires_at TIMESTAMPTZ;

ALTER TABLE go_publish_intents
    ADD CONSTRAINT ck_go_publish_intent_source CHECK (
        (draft_id IS NOT NULL AND source_version_id IS NULL
         AND publish_content IS NULL AND payload_purged_at IS NULL)
        OR
        (draft_id IS NULL AND source_version_id IS NOT NULL AND (
            (publish_content IS NOT NULL AND payload_purged_at IS NULL)
            OR (publish_content IS NULL AND payload_purged_at IS NOT NULL)
        ))
    );

CREATE INDEX IF NOT EXISTS ix_go_publish_retention_claim
    ON go_publish_intents(updated_at)
    WHERE source_version_id IS NOT NULL
      AND publish_content IS NOT NULL
      AND status IN ('SUCCEEDED','MANUAL_REVIEW');

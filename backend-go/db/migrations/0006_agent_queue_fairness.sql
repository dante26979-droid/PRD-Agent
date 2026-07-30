CREATE TABLE IF NOT EXISTS go_user_schedule_cursor (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    last_dispatched_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, owner_id)
);

CREATE INDEX IF NOT EXISTS ix_go_user_schedule_cursor_fairness
    ON go_user_schedule_cursor(last_dispatched_at, tenant_id, owner_id);

CREATE TABLE IF NOT EXISTS go_task_events (
    event_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES go_control_tasks(task_id),
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    sequence BIGINT NOT NULL CHECK (sequence > 0),
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    UNIQUE (task_id, sequence)
);

CREATE INDEX IF NOT EXISTS ix_go_task_events_owner_task
    ON go_task_events(tenant_id, owner_id, task_id, sequence);

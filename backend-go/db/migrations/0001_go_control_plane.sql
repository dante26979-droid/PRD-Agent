CREATE TABLE IF NOT EXISTS go_control_tasks (
    task_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_go_control_tasks_owner
    ON go_control_tasks(tenant_id, owner_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS go_agent_runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES go_control_tasks(task_id),
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'WAITING_CAPACITY', 'QUEUED', 'RUNNING', 'WAITING_USER',
        'SUCCEEDED', 'FAILED', 'STOPPING', 'STOPPED'
    )),
    queue_slot_acquired BOOLEAN NOT NULL DEFAULT FALSE,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    lease_id TEXT,
    worker_id TEXT,
    fencing_token BIGINT NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    lease_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_go_agent_runs_owner
    ON go_agent_runs(tenant_id, owner_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_go_agent_runs_admission
    ON go_agent_runs(status, queue_slot_acquired, created_at)
    WHERE status IN ('WAITING_CAPACITY', 'QUEUED', 'RUNNING');

CREATE INDEX IF NOT EXISTS ix_go_agent_runs_lease
    ON go_agent_runs(status, lease_expires_at)
    WHERE status = 'RUNNING';

CREATE TABLE IF NOT EXISTS go_queue_slots (
    slot_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES go_agent_runs(run_id),
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ACQUIRED', 'RELEASED')),
    acquired_at TIMESTAMPTZ NOT NULL,
    released_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS go_command_idempotency (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, owner_id, operation, idempotency_key)
);

CREATE TABLE IF NOT EXISTS go_outbox_messages (
    message_id TEXT PRIMARY KEY,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    published_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0,
    claimed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_go_outbox_pending
    ON go_outbox_messages(available_at, message_id)
    WHERE published_at IS NULL;

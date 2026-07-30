ALTER TABLE go_control_tasks
    ADD COLUMN IF NOT EXISTS repository_binding_id TEXT,
    ADD COLUMN IF NOT EXISTS repository_revision TEXT;

ALTER TABLE go_control_tasks
    DROP CONSTRAINT IF EXISTS ck_go_control_tasks_repository_binding;

ALTER TABLE go_control_tasks
    ADD CONSTRAINT ck_go_control_tasks_repository_binding
    CHECK (
        (repository_binding_id IS NULL AND repository_revision IS NULL)
        OR
        (
            repository_binding_id IS NOT NULL
            AND repository_binding_id <> ''
            AND repository_revision ~ '^[0-9a-f]{40}$'
        )
    );

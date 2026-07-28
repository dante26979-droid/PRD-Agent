ALTER TABLE prd_tasks
    ADD COLUMN IF NOT EXISTS owner_id TEXT;

UPDATE prd_tasks
   SET owner_id = 'local-user'
 WHERE owner_id IS NULL;

ALTER TABLE prd_tasks
    ALTER COLUMN owner_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS ix_prd_tasks_owner_updated
    ON prd_tasks(owner_id, updated_at DESC, task_id DESC);

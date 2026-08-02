ALTER TABLE go_agent_runs
    ADD COLUMN workflow_version TEXT;

UPDATE go_agent_runs
   SET workflow_version = 'agent-runtime.v1'
 WHERE workflow_version IS NULL;

ALTER TABLE go_agent_runs
    ALTER COLUMN workflow_version SET NOT NULL;

ALTER TABLE go_agent_runs
    ADD CONSTRAINT ck_go_agent_runs_workflow_version
    CHECK (workflow_version ~ '^agent-runtime[.]v[1-9][0-9]*$');

package storage

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// This fixture starts from a real 0015 schema with durable v1 rows, then runs
// the remaining migrations exactly as production does. It catches upgrade-only
// failures which a green-field database cannot expose.
func TestMigrationsUpgradeLegacy0015RowsThrough0023(t *testing.T) {
	dsn := os.Getenv("PRD_AGENT_TEST_DATABASE_DSN")
	if dsn == "" {
		t.Skip("PRD_AGENT_TEST_DATABASE_DSN is not configured")
	}
	ctx := context.Background()
	admin, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer admin.Close()
	schema := "migration_fixture_" + strconv.FormatInt(time.Now().UnixNano(), 10)
	identifier := pgx.Identifier{schema}.Sanitize()
	if _, err := admin.Exec(ctx, "CREATE SCHEMA "+identifier); err != nil {
		t.Fatal(err)
	}
	defer func() { _, _ = admin.Exec(ctx, "DROP SCHEMA "+identifier+" CASCADE") }()

	config, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		t.Fatal(err)
	}
	config.ConnConfig.RuntimeParams["search_path"] = schema
	pool, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()

	preDir, postDir := t.TempDir(), t.TempDir()
	copyMigrationSet(t, "0015", true, preDir)
	copyMigrationSet(t, "0015", false, postDir)
	if err := ApplyMigrations(ctx, pool, preDir); err != nil {
		t.Fatalf("apply legacy migration set: %v", err)
	}
	now := time.Now().UTC()
	if _, err := pool.Exec(ctx, `INSERT INTO go_control_tasks (task_id,tenant_id,owner_id,message,status,version,created_at,updated_at) VALUES ('legacy-task','legacy-tenant','legacy-owner','legacy PRD','DRAFT',1,$1,$1)`, now); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO go_agent_runs (run_id,task_id,tenant_id,owner_id,status,queue_slot_acquired,workflow_version,created_at,updated_at) VALUES ('legacy-run','legacy-task','legacy-tenant','legacy-owner','SUCCEEDED',FALSE,'agent-runtime.v1',$1,$1)`, now); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO go_agent_rollout_assignments (run_id,authoritative_workflow_version,evaluation_mode,cohort,policy_version,assignment_reason,created_at) VALUES ('legacy-run','agent-runtime.v1','OFF','CONTROL','legacy-policy','pre-0021',$1)`, now); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO go_run_checkpoints (run_id,sequence,checkpoint_blob,content_hash,created_at) VALUES ('legacy-run',1,'legacy-snapshot','legacy-hash',$1)`, now); err != nil {
		t.Fatal(err)
	}

	if err := ApplyMigrations(ctx, pool, postDir); err != nil {
		t.Fatalf("upgrade legacy database through 0023: %v", err)
	}
	// Running the exact set again must be a no-op.
	if err := ApplyMigrations(ctx, pool, postDir); err != nil {
		t.Fatalf("migration replay was not idempotent: %v", err)
	}
	var workflow, assignmentHash, checkpointHash string
	if err := pool.QueryRow(ctx, `SELECT r.workflow_version,a.assignment_hash,c.content_hash FROM go_agent_runs r JOIN go_agent_rollout_assignments a ON a.run_id=r.run_id JOIN go_run_checkpoints c ON c.run_id=r.run_id WHERE r.run_id='legacy-run'`).Scan(&workflow, &assignmentHash, &checkpointHash); err != nil {
		t.Fatal(err)
	}
	if workflow != "agent-runtime.v1" || assignmentHash != "" || checkpointHash != "legacy-hash" {
		t.Fatalf("legacy semantics changed during upgrade: workflow=%q assignment_hash=%q checkpoint=%q", workflow, assignmentHash, checkpointHash)
	}
	var migrationCount int
	if err := pool.QueryRow(ctx, `SELECT COUNT(*) FROM go_schema_migrations`).Scan(&migrationCount); err != nil {
		t.Fatal(err)
	}
	if migrationCount != 23 {
		t.Fatalf("expected migration head 0023, got %d applied files", migrationCount)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO go_rollout_commands (command_id,command_kind,request_hash,expected_state_version,result_state_version,evidence_hash,actor_ref,result_json,created_at) VALUES ('fixture-command','RECORD_READINESS','sha256:request',1,1,'sha256:evidence','fixture:test','{}',$1)`, now); err != nil {
		t.Fatalf("0021 command kind was not accepted: %v", err)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO go_rollout_commands (command_id,command_kind,request_hash,expected_state_version,result_state_version,evidence_hash,actor_ref,result_json,created_at) VALUES ('invalid-command','DELETE_LEGACY','sha256:request',1,1,'sha256:evidence','fixture:test','{}',$1)`, now); err == nil {
		t.Fatal("0021 command constraint accepted an unknown/destructive command kind")
	}
	var readinessTable bool
	if err := pool.QueryRow(ctx, `SELECT to_regclass(current_schema() || '.go_rollout_readiness_records') IS NOT NULL`).Scan(&readinessTable); err != nil || !readinessTable {
		t.Fatalf("readiness table missing: exists=%v err=%v", readinessTable, err)
	}
}

func copyMigrationSet(t *testing.T, boundary string, throughBoundary bool, target string) {
	t.Helper()
	source := filepath.Clean("../../db/migrations")
	entries, err := os.ReadDir(source)
	if err != nil {
		t.Fatal(err)
	}
	names := make([]string, 0, len(entries))
	for _, entry := range entries {
		if !entry.IsDir() && strings.HasSuffix(entry.Name(), ".sql") {
			names = append(names, entry.Name())
		}
	}
	sort.Strings(names)
	for _, name := range names {
		prefix := strings.SplitN(name, "_", 2)[0]
		selected := prefix <= boundary
		if !throughBoundary {
			selected = prefix > boundary
		}
		if !selected {
			continue
		}
		payload, err := os.ReadFile(filepath.Join(source, name))
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(target, name), payload, 0o600); err != nil {
			t.Fatal(fmt.Errorf("copy migration %s: %w", name, err))
		}
	}
}

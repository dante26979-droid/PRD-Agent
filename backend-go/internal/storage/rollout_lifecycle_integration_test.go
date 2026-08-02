package storage

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

func TestPostgresShadowArtifactBindsPersistedAuthoritativeDraft(t *testing.T) {
	store, cleanup := newIsolatedPostgresStore(t)
	defer cleanup()
	ctx := context.Background()
	policy := runcontrol.RolloutPolicy{
		PolicyVersion: "shadow-v4", DefaultWorkflow: runcontrol.WorkflowVersionV1, Shadow: true,
	}
	policyPayload, _ := json.Marshal(policy)
	if _, err := store.pool.Exec(ctx, `INSERT INTO go_rollout_policies (policy_version,policy_payload,policy_hash,active,created_at) VALUES ($1,$2,$3,TRUE,$4)`, policy.PolicyVersion, policyPayload, integrationHash(policyPayload), time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
	created, err := store.CreateTaskWithRun(ctx, "shadow-tenant", "shadow-owner", "shadow task", "shadow-start")
	if err != nil {
		t.Fatal(err)
	}
	run, err := store.AcquireRun(ctx, created.Run.RunID, "shadow-worker", time.Now().UTC(), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	lease := integrationLease(run)
	draft := []byte(`{"markdown":"# authoritative"}`)
	if _, err := store.SubmitDraft(ctx, lease, "shadow-draft", created.Task.Version, draft); err != nil {
		t.Fatal(err)
	}
	assignment, err := store.GetRolloutAssignment(ctx, run.RunID)
	if err != nil {
		t.Fatal(err)
	}
	buildArtifact := func(draftKey, draftHash string) runcontrol.RunArtifact {
		trace := map[string]any{
			"run_id": run.RunID, "task_id": run.TaskID,
			"authoritative_workflow_version": string(runcontrol.WorkflowVersionV1), "run_purpose": 0,
			"checkpoint_sequence": 0, "output_key": "", "output_kind": 0, "output_content_hash": "",
			"draft_key": draftKey, "draft_content_hash": draftHash, "submission_disposition": "SUBMIT_NEW",
		}
		tracePayload, _ := json.Marshal(trace)
		traceHash := "sha256:" + integrationHash(tracePayload)
		content, _ := json.Marshal(runcontrol.ShadowEvaluationArtifact{
			SchemaVersion: "shadow-evaluation.v1", AuthoritativeTraceHash: traceHash,
			AuthoritativeTrace: trace, CandidatePolicyVersion: assignment.PolicyVersion, AssignmentHash: assignment.AssignmentHash,
		})
		digest := sha256.Sum256([]byte(strings.Join([]string{traceHash, assignment.PolicyVersion, assignment.AssignmentHash}, "\x00")))
		return runcontrol.RunArtifact{
			ArtifactKey: run.RunID + ":shadow-evaluation:1", ArtifactType: "SHADOW_EVALUATION", Generation: 1,
			RequestHash: "sha256:" + hex.EncodeToString(digest[:]), Content: content,
		}
	}
	artifact := buildArtifact("shadow-draft", "sha256:"+integrationHash(draft))
	if _, err := store.SaveRunArtifact(ctx, lease, artifact); err != nil {
		t.Fatalf("persisted trace was rejected: %v", err)
	}
	drift := buildArtifact("worker-local-draft", "sha256:"+strings.Repeat("a", 64))
	if _, err := store.SaveRunArtifact(ctx, lease, drift); !errors.Is(err, runcontrol.ErrInvalidPayload) {
		t.Fatalf("worker-local trace was accepted: %v", err)
	}
}

func TestPostgresLifecycleCommandsAndReadinessAreAtomic(t *testing.T) {
	store, cleanup := newIsolatedPostgresStore(t)
	defer cleanup()
	ctx := context.Background()
	policy := runcontrol.RolloutPolicy{PolicyVersion: "lifecycle-v4", DefaultWorkflow: runcontrol.WorkflowVersionV4}
	payload, err := json.Marshal(policy)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.pool.Exec(ctx, `INSERT INTO go_rollout_policies (policy_version,policy_payload,policy_hash,active,created_at) VALUES ($1,$2,$3,TRUE,$4)`, policy.PolicyVersion, payload, integrationHash(payload), time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
	minimum := 0.9
	gate := runcontrol.RolloutGateOperatorCommand{
		CommandID: "pg-gate", ExpectedVersion: 1, ManifestHash: "sha256:manifest",
		EvidenceHash: "sha256:evidence", ActorRef: "integration:test", Apply: true,
		Manifest: runcontrol.GateManifest{
			SchemaVersion: "agent-runtime-gate.v1", CandidateVersion: runcontrol.WorkflowVersionV4,
			BaselineVersion: runcontrol.WorkflowVersionV1, DatasetHash: "sha256:dataset", MinimumRepetitions: 3,
			Thresholds: []runcontrol.GateThreshold{{Metric: "quality", Minimum: &minimum, HardRequired: true}},
		},
		Report: runcontrol.GateReport{
			DatasetHash: "sha256:dataset", Repetitions: 3, Metrics: map[string]float64{"quality": 0.95},
			ReportHash: "sha256:report", ConfigHash: "sha256:config",
			Candidate: runcontrol.WorkflowVersionV4, Baseline: runcontrol.WorkflowVersionV1,
		},
	}
	gateResult, err := store.ApplyGateCommand(ctx, gate)
	if err != nil || !gateResult.Applied || !gateResult.Record.Decision.Passed {
		t.Fatalf("gate command failed: result=%+v err=%v", gateResult, err)
	}
	replay, err := store.ApplyGateCommand(ctx, gate)
	if err != nil || !replay.Replayed || replay.Record.DecisionID != gateResult.Record.DecisionID {
		t.Fatalf("gate replay drift: result=%+v err=%v", replay, err)
	}
	drift := gate
	drift.Report.ConfigHash = "sha256:drift"
	if _, err := store.ApplyGateCommand(ctx, drift); !errors.Is(err, runcontrol.ErrInvalidIdempotency) {
		t.Fatalf("gate idempotency drift was accepted: %v", err)
	}
	transition := runcontrol.RolloutStageOperatorCommand{
		CommandID: "pg-transition", ExpectedVersion: 1, Target: runcontrol.RolloutLocallyVerified,
		GateDecisionID: gateResult.Record.DecisionID, PolicyVersion: policy.PolicyVersion,
		EvidenceHash: gate.EvidenceHash, ActorRef: "integration:test", Apply: true,
	}
	stageResult, err := store.ApplyStageCommand(ctx, transition)
	if err != nil || stageResult.State.Version != 2 || stageResult.State.Stage != runcontrol.RolloutLocallyVerified {
		t.Fatalf("transition failed: result=%+v err=%v", stageResult, err)
	}
	readiness := runcontrol.ReadinessOperatorCommand{
		CommandID: "pg-readiness", ExpectedVersion: 2, ActorRef: "integration:test", Apply: true,
		Input: runcontrol.ReadinessInput{
			CommitHash: "sha256:commit", MigrationSetHash: "sha256:migrations", MigrationHead: "0021",
			ContractReportHash: "sha256:contract", PostgresReportHash: "sha256:postgres",
			EvalManifestHash: gate.ManifestHash, ShadowPolicyHash: "sha256:shadow",
			GateDecisionID: gateResult.Record.DecisionID,
		},
	}
	ready, err := store.ApplyReadinessCommand(ctx, readiness)
	if err != nil || ready.Record.Status != runcontrol.ReadinessStagingReady {
		t.Fatalf("readiness record failed: result=%+v err=%v", ready, err)
	}
	verified, err := store.VerifyReadiness(ctx, ready.Record.ReadinessID)
	if err != nil || !verified.Verified || verified.StateStale {
		t.Fatalf("readiness rebuild failed: result=%+v err=%v", verified, err)
	}
	var commandCount int
	if err := store.pool.QueryRow(ctx, `SELECT COUNT(*) FROM go_rollout_commands`).Scan(&commandCount); err != nil || commandCount != 3 {
		t.Fatalf("lifecycle commands were not durably unified: count=%d err=%v", commandCount, err)
	}
}

func newIsolatedPostgresStore(t *testing.T) (*PostgresStore, func()) {
	return newIsolatedPostgresStoreWithConfig(t, Config{
		MinConns: 1, MaxConns: 4, MaxGlobalRunnable: 10, MaxRunnablePerOwner: 10,
		MaxWaitingRuns: 20, ConfirmationSecret: "integration-confirmation-secret-32-bytes",
		DefaultWorkflowVersion:        runcontrol.WorkflowVersionV4,
		DefaultExecutionLedgerVersion: runcontrol.ExecutionLedgerVersionV1,
	})
}

func newIsolatedPostgresStoreWithConfig(t *testing.T, storeConfig Config) (*PostgresStore, func()) {
	t.Helper()
	dsn := os.Getenv("PRD_AGENT_TEST_DATABASE_DSN")
	if dsn == "" {
		t.Skip("PRD_AGENT_TEST_DATABASE_DSN is not configured")
	}
	ctx := context.Background()
	admin, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	schema := "store_fixture_" + strconv.FormatInt(time.Now().UnixNano(), 10)
	identifier := pgx.Identifier{schema}.Sanitize()
	if _, err := admin.Exec(ctx, "CREATE SCHEMA "+identifier); err != nil {
		admin.Close()
		t.Fatal(err)
	}
	storeConfig.DSN, storeConfig.SearchPath = dsn, schema
	store, err := NewPostgresStore(ctx, storeConfig)
	if err != nil {
		admin.Close()
		t.Fatal(err)
	}
	if err := ApplyMigrations(ctx, store.pool, "../../db/migrations"); err != nil {
		store.Close()
		_, _ = admin.Exec(ctx, "DROP SCHEMA "+identifier+" CASCADE")
		admin.Close()
		t.Fatal(err)
	}
	return store, func() {
		store.Close()
		_, _ = admin.Exec(ctx, "DROP SCHEMA "+identifier+" CASCADE")
		admin.Close()
	}
}

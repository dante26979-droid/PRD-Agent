package runcontrol

import (
	"context"
	"errors"
	"strings"
	"testing"
)

func passingLifecycleGate() (GateManifest, GateReport) {
	minimum := 0.9
	manifest := GateManifest{
		SchemaVersion: "agent-runtime-gate.v1", CandidateVersion: WorkflowVersionV4,
		BaselineVersion: WorkflowVersionV1, DatasetHash: "sha256:dataset", MinimumRepetitions: 3,
		Thresholds: []GateThreshold{{Metric: "quality", Minimum: &minimum, HardRequired: true}},
	}
	report := GateReport{
		DatasetHash: "sha256:dataset", Repetitions: 3, Metrics: map[string]float64{"quality": 0.95},
		ReportHash: "sha256:report", ConfigHash: "sha256:config",
		Candidate: WorkflowVersionV4, Baseline: WorkflowVersionV1,
	}
	return manifest, report
}

func TestMemoryLifecycleCommandsAreDryRunApplyReplayAndHashBound(t *testing.T) {
	policy := &RolloutPolicy{PolicyVersion: "policy-v4", DefaultWorkflow: WorkflowVersionV4}
	store := NewMemoryStore(QueuePolicy{RolloutPolicy: policy})
	manifest, report := passingLifecycleGate()
	gateCommand := RolloutGateOperatorCommand{
		CommandID: "gate-command", ExpectedVersion: 1, Manifest: manifest, Report: report,
		ManifestHash: "sha256:manifest", EvidenceHash: "sha256:evidence", ActorRef: "operator:test",
	}
	dryRun, err := store.ApplyGateCommand(context.Background(), gateCommand)
	if err != nil || dryRun.Applied || !dryRun.Record.Decision.Passed {
		t.Fatalf("gate dry-run failed: result=%+v err=%v", dryRun, err)
	}
	gateCommand.Apply = true
	appliedGate, err := store.ApplyGateCommand(context.Background(), gateCommand)
	if err != nil || !appliedGate.Applied {
		t.Fatalf("gate apply failed: result=%+v err=%v", appliedGate, err)
	}
	replayedGate, err := store.ApplyGateCommand(context.Background(), gateCommand)
	if err != nil || !replayedGate.Replayed || replayedGate.Record.DecisionID != appliedGate.Record.DecisionID {
		t.Fatalf("gate replay drift: result=%+v err=%v", replayedGate, err)
	}
	drift := gateCommand
	drift.Report.ReportHash = "sha256:different"
	if _, err := store.ApplyGateCommand(context.Background(), drift); !errors.Is(err, ErrInvalidIdempotency) {
		t.Fatalf("gate command hash drift was accepted: %v", err)
	}

	transition := RolloutStageOperatorCommand{
		CommandID: "transition-command", ExpectedVersion: 1, Target: RolloutLocallyVerified,
		GateDecisionID: appliedGate.Record.DecisionID, PolicyVersion: policy.PolicyVersion,
		EvidenceHash: gateCommand.EvidenceHash, ActorRef: "operator:test", Apply: true,
	}
	stage, err := store.ApplyStageCommand(context.Background(), transition)
	if err != nil || !stage.Applied || stage.State.Stage != RolloutLocallyVerified || stage.State.Version != 2 {
		t.Fatalf("stage transition failed: result=%+v err=%v", stage, err)
	}
	replayedStage, err := store.ApplyStageCommand(context.Background(), transition)
	if err != nil || !replayedStage.Replayed || replayedStage.State != stage.State {
		t.Fatalf("stage replay drift: result=%+v err=%v", replayedStage, err)
	}

	readiness := ReadinessOperatorCommand{
		CommandID: "readiness-command", ExpectedVersion: 2, ActorRef: "operator:test", Apply: true,
		Input: ReadinessInput{
			CommitHash: "sha256:commit", MigrationSetHash: "sha256:migrations", MigrationHead: "0021",
			ContractReportHash: "sha256:contract", PostgresReportHash: "sha256:postgres",
			EvalManifestHash: gateCommand.ManifestHash, ShadowPolicyHash: "sha256:shadow",
			GateDecisionID: appliedGate.Record.DecisionID,
		},
	}
	ready, err := store.ApplyReadinessCommand(context.Background(), readiness)
	if err != nil || ready.Record.Status != ReadinessStagingReady || len(ready.Record.BlockingGateCodes) != 0 {
		t.Fatalf("readiness did not reach STAGING_READY: result=%+v err=%v", ready, err)
	}
	verification, err := store.VerifyReadiness(context.Background(), ready.Record.ReadinessID)
	if err != nil || !verification.Verified || verification.StateStale {
		t.Fatalf("durable readiness did not rebuild: result=%+v err=%v", verification, err)
	}
	replayedReadiness, err := store.ApplyReadinessCommand(context.Background(), readiness)
	if err != nil || !replayedReadiness.Replayed || replayedReadiness.Record.RecordHash != ready.Record.RecordHash {
		t.Fatalf("readiness replay drift: result=%+v err=%v", replayedReadiness, err)
	}
}

func TestReadinessCannotClearComputedBlockers(t *testing.T) {
	store := NewMemoryStore(QueuePolicy{})
	result, err := store.ApplyReadinessCommand(context.Background(), ReadinessOperatorCommand{
		CommandID: "blocked", ExpectedVersion: 1, ActorRef: "operator:test",
		Input: ReadinessInput{MigrationHead: "0020"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.Record.Status != ReadinessBlocked || len(result.Record.BlockingGateCodes) < 5 {
		t.Fatalf("missing inputs did not fail closed: %+v", result.Record)
	}
}

func TestPersistedAuthoritativeTraceRejectsWorkerLocalOutput(t *testing.T) {
	trace := AuthoritativeTrace{
		RunID: "run", TaskID: "task", OutputKey: "output", OutputKind: 3,
		OutputContentHash: "sha256:" + strings.Repeat("a", 64),
	}
	persisted := PersistedAuthoritativeResult{
		RunID: "run", TaskID: "task", OutputKey: "output", OutputKind: RunOutputUnitPatch,
		OutputContentHash: "sha256:" + strings.Repeat("b", 64),
	}
	if err := ValidatePersistedAuthoritativeTrace(trace, persisted); !errors.Is(err, ErrInvalidPayload) {
		t.Fatalf("unpersisted output hash was accepted: %v", err)
	}
}

package runcontrol

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"math"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestShadowEvaluationArtifactIsHashBoundAndIdempotent(t *testing.T) {
	store := NewMemoryStore(QueuePolicy{})
	created, err := store.CreateTaskWithRun(context.Background(), "tenant", "owner", "message", "shadow-artifact-task")
	if err != nil {
		t.Fatal(err)
	}
	assignment := RolloutAssignment{
		AuthoritativeWorkflowVersion: WorkflowVersionV1,
		EvaluationMode:               EvaluationShadow, ShadowWorkflowVersion: WorkflowVersionV4,
		Cohort: CohortControl, PolicyVersion: "policy-v4", AssignmentReason: "shadow",
	}.WithHash()
	if err := store.SaveRolloutAssignment(context.Background(), created.Run.RunID, assignment); err != nil {
		t.Fatal(err)
	}
	run, err := store.AcquireRun(context.Background(), created.Run.RunID, "worker", time.Now().UTC(), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	lease := leaseFromRun(run)
	draft := []byte(`{"markdown":"# authoritative"}`)
	if _, err := store.SubmitDraft(context.Background(), lease, "shadow-draft", created.Task.Version, draft); err != nil {
		t.Fatal(err)
	}
	trace := map[string]any{
		"run_id": created.Run.RunID, "task_id": created.Task.TaskID,
		"authoritative_workflow_version": string(WorkflowVersionV1), "run_purpose": 0,
		"checkpoint_sequence": 0, "output_key": "", "output_kind": 0, "output_content_hash": "",
		"draft_key": "shadow-draft", "draft_content_hash": "sha256:" + stableHash(string(draft)),
		"submission_disposition": "SUBMIT_NEW",
	}
	tracePayload, err := canonicalJSON(trace)
	if err != nil {
		t.Fatal(err)
	}
	traceDigest := sha256.Sum256(tracePayload)
	traceHash := "sha256:" + hex.EncodeToString(traceDigest[:])
	payload, err := json.Marshal(ShadowEvaluationArtifact{
		SchemaVersion: "shadow-evaluation.v1", AuthoritativeTraceHash: traceHash,
		AuthoritativeTrace: trace, CandidatePolicyVersion: assignment.PolicyVersion, AssignmentHash: assignment.AssignmentHash,
	})
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256([]byte(strings.Join([]string{traceHash, assignment.PolicyVersion, assignment.AssignmentHash}, "\x00")))
	artifact := RunArtifact{
		ArtifactKey: created.Run.RunID + ":shadow-evaluation:1", ArtifactType: "SHADOW_EVALUATION", Generation: 1,
		RequestHash: "sha256:" + hex.EncodeToString(digest[:]), Content: payload,
	}
	first, err := store.SaveRunArtifact(context.Background(), lease, artifact)
	if err != nil {
		t.Fatal(err)
	}
	second, err := store.SaveRunArtifact(context.Background(), lease, artifact)
	if err != nil || second != first {
		t.Fatalf("shadow artifact replay drift: first=%+v second=%+v err=%v", first, second, err)
	}
	var drift map[string]any
	if err := json.Unmarshal(payload, &drift); err != nil {
		t.Fatal(err)
	}
	drift["assignment_hash"] = "sha256:drift"
	artifact.Content, _ = json.Marshal(drift)
	artifact.ContentHash = ""
	if _, err := store.SaveRunArtifact(context.Background(), lease, artifact); !errors.Is(err, ErrInvalidPayload) {
		t.Fatalf("assignment drift was not rejected: %v", err)
	}
	artifact.Content, _ = json.Marshal(ShadowEvaluationArtifact{
		SchemaVersion: "shadow-evaluation.v1", AuthoritativeTraceHash: traceHash,
		AuthoritativeTrace: map[string]any{
			"run_id": created.Run.RunID, "task_id": created.Task.TaskID,
			"authoritative_workflow_version": string(WorkflowVersionV1), "run_purpose": 0,
			"checkpoint_sequence": 0, "output_key": "", "output_kind": 0, "output_content_hash": "",
			"draft_key": "missing-draft", "draft_content_hash": "sha256:" + strings.Repeat("a", 64),
			"submission_disposition": "SUBMIT_NEW",
		},
		CandidatePolicyVersion: assignment.PolicyVersion, AssignmentHash: assignment.AssignmentHash,
	})
	artifact.ContentHash = ""
	if _, err := store.SaveRunArtifact(context.Background(), lease, artifact); !errors.Is(err, ErrInvalidPayload) && !errors.Is(err, ErrNotFound) {
		t.Fatalf("unpersisted authoritative result was accepted: %v", err)
	}
}

func TestRolloutAssignmentIsStableAndPolicyChangesOnlyNewDecision(t *testing.T) {
	request := AssignmentRequest{TenantID: "tenant", OwnerID: "owner", TaskID: "task"}
	policy := RolloutPolicy{PolicyVersion: "rollout-policy.v1:a", DefaultWorkflow: WorkflowVersionV1, CanaryBasisPoints: 5000}
	first, err := policy.Assign(request)
	if err != nil {
		t.Fatal(err)
	}
	second, err := policy.Assign(request)
	if err != nil {
		t.Fatal(err)
	}
	if first != second {
		t.Fatalf("assignment is not stable: %+v != %+v", first, second)
	}
	policy.PolicyVersion = "rollout-policy.v1:b"
	changed, err := policy.Assign(request)
	if err != nil {
		t.Fatal(err)
	}
	if changed.PolicyVersion == first.PolicyVersion {
		t.Fatal("policy version was not persisted in assignment")
	}
}

func TestRolloutPauseCommandSharesDryRunApplyAndReplayValidation(t *testing.T) {
	store := NewMemoryStore(QueuePolicy{RolloutPolicy: &RolloutPolicy{
		PolicyVersion: "policy-safe", DefaultWorkflow: WorkflowVersionV1,
	}})
	command := RolloutOperatorCommand{
		CommandID: "pause-1", Kind: RolloutCommandPause, ExpectedVersion: 1,
		ReasonCode: "SHADOW_EFFECT_VIOLATION", EvidenceHash: "sha256:evidence",
		ActorRef: "operator:test", Now: time.Now().UTC(),
	}
	dryRun, err := store.ApplyRolloutCommand(context.Background(), command)
	if err != nil {
		t.Fatal(err)
	}
	if dryRun.Applied || !dryRun.State.Paused || dryRun.State.Version != 2 {
		t.Fatalf("unexpected dry-run result: %+v", dryRun)
	}
	state, err := store.InspectRolloutState(context.Background())
	if err != nil || state.Paused || state.Version != 1 {
		t.Fatalf("dry-run mutated state: state=%+v err=%v", state, err)
	}
	command.Apply = true
	applied, err := store.ApplyRolloutCommand(context.Background(), command)
	if err != nil {
		t.Fatal(err)
	}
	replayed, err := store.ApplyRolloutCommand(context.Background(), command)
	if err != nil || !replayed.Replayed || replayed.State != applied.State {
		t.Fatalf("command replay drift: applied=%+v replayed=%+v err=%v", applied, replayed, err)
	}
	conflict := command
	conflict.ReasonCode = "DIFFERENT_REASON"
	if _, err := store.ApplyRolloutCommand(context.Background(), conflict); !errors.Is(err, ErrInvalidIdempotency) {
		t.Fatalf("idempotency drift was not rejected: %v", err)
	}
}

func TestRolloutStageTransitionRequiresDurablePassedGateEvidence(t *testing.T) {
	store := NewMemoryStore(QueuePolicy{})
	maximum := 0.0
	manifest := GateManifest{
		SchemaVersion: "agent-runtime-gate.v1", CandidateVersion: WorkflowVersionV4,
		BaselineVersion: WorkflowVersionV1, DatasetHash: "dataset-hash", MinimumRepetitions: 3,
		Thresholds: []GateThreshold{{Metric: "shadow_extra_model_calls", Maximum: &maximum, HardRequired: true}},
	}
	failed, err := store.RecordGateDecision(context.Background(), RecordGateDecisionCommand{
		Manifest:     manifest,
		Report:       GateReport{DatasetHash: "dataset-hash", Repetitions: 3, Metrics: map[string]float64{"shadow_extra_model_calls": 1}, ReportHash: "report-failed", ConfigHash: "config", Candidate: WorkflowVersionV4, Baseline: WorkflowVersionV1},
		ManifestHash: "manifest-hash", EvidenceHash: "evidence-failed", Now: time.Now().UTC(),
	})
	if err != nil || failed.Decision.Passed {
		t.Fatalf("failed decision was not persisted: %+v err=%v", failed, err)
	}
	if _, err := store.AuthorizeRolloutTransition(context.Background(), RolloutTransitionCommand{Target: RolloutLocallyVerified, GateDecisionID: failed.DecisionID, EvidenceHash: "evidence", ExpectedVersion: 1}); !errors.Is(err, ErrInvalidRunStatus) {
		t.Fatalf("failed gate authorized rollout: %v", err)
	}

	passed, err := store.RecordGateDecision(context.Background(), RecordGateDecisionCommand{
		Manifest:     manifest,
		Report:       GateReport{DatasetHash: "dataset-hash", Repetitions: 3, Metrics: map[string]float64{"shadow_extra_model_calls": 0}, ReportHash: "report-passed", ConfigHash: "config", Candidate: WorkflowVersionV4, Baseline: WorkflowVersionV1},
		ManifestHash: "manifest-hash", EvidenceHash: "evidence-passed", Now: time.Now().UTC(),
	})
	if err != nil || !passed.Decision.Passed {
		t.Fatalf("passed decision was not persisted: %+v err=%v", passed, err)
	}
	stage, err := store.AuthorizeRolloutTransition(context.Background(), RolloutTransitionCommand{
		Target: RolloutLocallyVerified, GateDecisionID: passed.DecisionID,
		PolicyVersion: "rollout-policy.v1", EvidenceHash: "evidence-passed", ExpectedVersion: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	if stage.Stage != RolloutLocallyVerified || stage.Version != 2 || stage.LastGateDecisionID != passed.DecisionID {
		t.Fatalf("unexpected durable rollout stage: %+v", stage)
	}
}

func TestGateFailsClosedOnNonFiniteMetric(t *testing.T) {
	maximum := 0.0
	decision := EvaluateRolloutGate(
		GateManifest{SchemaVersion: "agent-runtime-gate.v1", CandidateVersion: WorkflowVersionV4, BaselineVersion: WorkflowVersionV1, DatasetHash: "dataset", MinimumRepetitions: 1, Thresholds: []GateThreshold{{Metric: "shadow_extra_model_calls", Maximum: &maximum, HardRequired: true}}},
		GateReport{DatasetHash: "dataset", Repetitions: 1, Metrics: map[string]float64{"shadow_extra_model_calls": math.NaN()}, ReportHash: "report", ConfigHash: "config", Candidate: WorkflowVersionV4, Baseline: WorkflowVersionV1},
	)
	if decision.Passed || !reflect.DeepEqual(decision.FailedGateCodes, []string{"NON_FINITE_METRIC:shadow_extra_model_calls"}) {
		t.Fatalf("non-finite metric did not fail closed: %+v", decision)
	}
}

func TestRolloutPriorityEmergencyDenyBeforeExplicitTask(t *testing.T) {
	policy := RolloutPolicy{
		PolicyVersion: "rollout-policy.v1:a", DefaultWorkflow: WorkflowVersionV4,
		EmergencyDeny: map[string]bool{"tenant:owner": true}, ExplicitV4Tasks: map[string]bool{"task": true},
	}
	assignment, err := policy.Assign(AssignmentRequest{TenantID: "tenant", OwnerID: "owner", TaskID: "task"})
	if err != nil {
		t.Fatal(err)
	}
	if assignment.AuthoritativeWorkflowVersion != WorkflowVersionV1 || assignment.AssignmentReason != "emergency_deny" {
		t.Fatalf("unexpected assignment: %+v", assignment)
	}
}

func TestShadowHasZeroAdditionalRemoteEffects(t *testing.T) {
	baseline := RemoteEffectCounters{ModelPhysicalCalls: 1, CapabilityPhysicalCalls: 2, DraftWrites: 1}
	if err := ValidateShadowEffects(baseline, baseline); err != nil {
		t.Fatal(err)
	}
	shadow := baseline
	shadow.ModelPhysicalCalls++
	if err := ValidateShadowEffects(baseline, shadow); !errors.Is(err, errNoRemoteEffects) {
		t.Fatalf("expected remote effect rejection, got %v", err)
	}
}

func TestShadowRunCannotReserveRemoteLedgerEffects(t *testing.T) {
	store := NewMemoryStore(QueuePolicy{DefaultWorkflowVersion: WorkflowVersionV4})
	created, err := store.CreateTaskWithRun(context.Background(), "tenant", "owner", "message", "shadow-task")
	if err != nil {
		t.Fatal(err)
	}
	assignment := RolloutAssignment{
		AuthoritativeWorkflowVersion: WorkflowVersionV4,
		EvaluationMode:               EvaluationShadow,
		ShadowWorkflowVersion:        WorkflowVersionV4,
		Cohort:                       CohortControl,
		PolicyVersion:                "shadow-policy.v1",
		AssignmentReason:             "test-shadow",
	}
	if err := store.SaveRolloutAssignment(context.Background(), created.Run.RunID, assignment); err != nil {
		t.Fatal(err)
	}
	run, err := store.AcquireRun(context.Background(), created.Run.RunID, "worker", time.Now().UTC(), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	_, err = store.ReserveLedgerEntry(context.Background(), leaseFromRun(run), LedgerEntry{
		OperationKey: "model-1", EntryKind: LedgerEntryModel,
		Operation: "generate", RequestHash: "request-hash", Reservation: BudgetDelta{ModelAttempts: 1},
	})
	if !errors.Is(err, errNoRemoteEffects) {
		t.Fatalf("shadow remote reservation was not rejected: %v", err)
	}
}

func TestLegacyDrainCompletesOnlyAfterAuthoritativeInventoryIsZero(t *testing.T) {
	store := NewMemoryStore(QueuePolicy{})
	created, err := store.CreateTaskWithRun(context.Background(), "tenant", "owner", "message", "legacy-task")
	if err != nil {
		t.Fatal(err)
	}
	drain, err := store.BeginDrain(context.Background(), BeginDrainCommand{WorkflowVersion: WorkflowVersionV1, EvidenceHash: "drain-evidence", Now: time.Now().UTC()})
	if err != nil {
		t.Fatal(err)
	}
	if drain.Status != DrainBlocked || drain.Inventory.ActiveRunCount != 1 || drain.Inventory.UnpublishedOutboxCount != 1 {
		t.Fatalf("active legacy work did not block drain: %+v", drain)
	}
	run, err := store.AcquireRun(context.Background(), created.Run.RunID, "legacy-worker", time.Now().UTC(), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.CompleteRun(context.Background(), leaseFromRun(run), RunSucceeded, time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
	if err := store.MarkOutboxPublished(context.Background(), "outbox-"+created.Run.RunID, time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
	completed, err := store.RefreshDrain(context.Background(), drain.DrainID, time.Now().UTC())
	if err != nil {
		t.Fatal(err)
	}
	if completed.Status != DrainCompleted || !completed.Inventory.Empty() || completed.CompletedAt == nil {
		t.Fatalf("zero legacy inventory did not complete drain: %+v", completed)
	}
}

func TestGateFailsClosedOnMissingMetricBlockingCaseAndLowRepetitions(t *testing.T) {
	zero, one := 0.0, 1.0
	manifest := GateManifest{
		SchemaVersion: "agent-runtime-gate.v1", CandidateVersion: WorkflowVersionV4,
		BaselineVersion: WorkflowVersionV1, DatasetHash: "dataset", MinimumRepetitions: 3,
		Thresholds: []GateThreshold{
			{Metric: "unsupported_current_state", Maximum: &zero, HardRequired: true},
			{Metric: "critical_unknown_recall", Minimum: &one, HardRequired: true},
		},
		BlockingCases: []string{"case-1"},
	}
	report := GateReport{
		DatasetHash: "dataset", Repetitions: 2,
		Metrics:     map[string]float64{"unsupported_current_state": 0},
		FailedCases: []string{"case-1"}, ReportHash: "report", ConfigHash: "config",
		Candidate: WorkflowVersionV4, Baseline: WorkflowVersionV1,
	}
	decision := EvaluateRolloutGate(manifest, report)
	if decision.Passed {
		t.Fatal("gate unexpectedly passed")
	}
	want := []string{"BLOCKING_CASE:case-1", "INSUFFICIENT_REPETITIONS", "MISSING_METRIC:critical_unknown_recall"}
	if !reflect.DeepEqual(decision.FailedGateCodes, want) {
		t.Fatalf("failed gate codes = %#v, want %#v", decision.FailedGateCodes, want)
	}
}

func TestMemoryStorePersistsResolvedRolloutAssignmentAtRunCreation(t *testing.T) {
	policy := RolloutPolicy{
		PolicyVersion: "rollout-policy.v1:a", DefaultWorkflow: WorkflowVersionV1,
		ExplicitV4Tasks: map[string]bool{}, InternalOwners: map[string]bool{"tenant:owner": true},
	}
	store := NewMemoryStore(QueuePolicy{RolloutPolicy: &policy})
	created, err := store.CreateTaskWithRun(context.Background(), "tenant", "owner", "message", "idem")
	if err != nil {
		t.Fatal(err)
	}
	assignment, err := store.GetRolloutAssignment(context.Background(), created.Run.RunID)
	if err != nil {
		t.Fatal(err)
	}
	if created.Run.WorkflowVersion != WorkflowVersionV4 || assignment.AssignmentReason != "owner_allowlist" {
		t.Fatalf("unexpected persisted rollout: run=%+v assignment=%+v", created.Run, assignment)
	}
	if created.Run.ExecutionLedgerVersion != ExecutionLedgerVersionV1 {
		t.Fatal("v4 rollout must receive the execution ledger")
	}
	if scope, ok := store.unitScopes[created.Run.RunID]; !ok || scope.Purpose != RunPurposePlanOutline {
		t.Fatalf("v4 initial run did not receive PLAN_OUTLINE scope: %+v", scope)
	}
}

func TestRetryInheritsPersistedRolloutAssignmentAfterPolicyChanges(t *testing.T) {
	policy := RolloutPolicy{
		PolicyVersion: "rollout-policy.v1:a", DefaultWorkflow: WorkflowVersionV1,
		InternalOwners: map[string]bool{"tenant:owner": true},
	}
	store := NewMemoryStore(QueuePolicy{RolloutPolicy: &policy})
	created, err := store.CreateTaskWithRun(context.Background(), "tenant", "owner", "message", "idem")
	if err != nil {
		t.Fatal(err)
	}
	store.mu.Lock()
	run := store.runs[created.Run.RunID]
	run.Status = RunSucceeded
	run.QueueSlotAcquired = false
	store.runs[run.RunID] = run
	store.policy.RolloutPolicy = &RolloutPolicy{PolicyVersion: "rollout-policy.v1:b", DefaultWorkflow: WorkflowVersionV1}
	store.mu.Unlock()

	retry, err := store.RetryTask(context.Background(), "tenant", "owner", created.Task.TaskID, "retry-idem", created.Task.Version)
	if err != nil {
		t.Fatal(err)
	}
	assignment, err := store.GetRolloutAssignment(context.Background(), retry.RunID)
	if err != nil {
		t.Fatal(err)
	}
	if retry.WorkflowVersion != WorkflowVersionV4 || assignment.PolicyVersion != "rollout-policy.v1:a" {
		t.Fatalf("retry did not inherit original assignment: run=%+v assignment=%+v", retry, assignment)
	}
}

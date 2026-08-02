package storage

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/testsupport/reviewcontract"
)

func TestPostgresStoreReviewWorkflowContract(t *testing.T) {
	reviewcontract.Run(t, func(t *testing.T) (runcontrol.AgentExecutionStore, func()) {
		store, cleanup := newIsolatedPostgresStoreWithConfig(t, Config{
			MinConns: 1, MaxConns: 4,
			MaxGlobalRunnable: 10, MaxRunnablePerOwner: 10,
			DefaultWorkflowVersion:        runcontrol.WorkflowVersionV4,
			DefaultExecutionLedgerVersion: runcontrol.ExecutionLedgerVersionV1,
		})
		suffix := strconv.FormatInt(time.Now().UnixNano(), 10)
		policy := runcontrol.RolloutPolicy{PolicyVersion: "contract-v4-" + suffix, DefaultWorkflow: runcontrol.WorkflowVersionV4}
		payload, err := json.Marshal(policy)
		if err != nil {
			store.Close()
			t.Fatal(err)
		}
		tx, err := store.pool.Begin(context.Background())
		if err != nil {
			store.Close()
			t.Fatal(err)
		}
		if _, err = tx.Exec(context.Background(), `UPDATE go_rollout_policies SET active=FALSE WHERE active=TRUE`); err == nil {
			_, err = tx.Exec(context.Background(), `INSERT INTO go_rollout_policies (policy_version,policy_payload,policy_hash,active,created_at) VALUES ($1,$2,$3,TRUE,$4)`, policy.PolicyVersion, payload, integrationHash(payload), time.Now().UTC())
		}
		if err != nil {
			_ = tx.Rollback(context.Background())
			store.Close()
			t.Fatal(err)
		}
		if err := tx.Commit(context.Background()); err != nil {
			store.Close()
			t.Fatal(err)
		}
		return store, cleanup
	})
}

func TestPostgresShadowContextAndLedgerDenyRemoteReservation(t *testing.T) {
	store, cleanup := newIsolatedPostgresStoreWithConfig(t, Config{
		MinConns: 1, MaxConns: 2,
		MaxGlobalRunnable: 10, MaxRunnablePerOwner: 10,
		DefaultExecutionLedgerVersion: runcontrol.ExecutionLedgerVersionV1,
	})
	defer cleanup()
	suffix := strconv.FormatInt(time.Now().UnixNano(), 10)
	policy := runcontrol.RolloutPolicy{
		PolicyVersion:   "integration-shadow-" + suffix,
		DefaultWorkflow: runcontrol.WorkflowVersionV1,
		Shadow:          true,
	}
	payload, err := json.Marshal(policy)
	if err != nil {
		t.Fatal(err)
	}
	tx, err := store.pool.Begin(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(context.Background(), `UPDATE go_rollout_policies SET active=FALSE WHERE active=TRUE`); err != nil {
		_ = tx.Rollback(context.Background())
		t.Fatal(err)
	}
	if _, err := tx.Exec(context.Background(), `INSERT INTO go_rollout_policies (policy_version,policy_payload,policy_hash,active,created_at) VALUES ($1,$2,$3,TRUE,$4)`, policy.PolicyVersion, payload, integrationHash(payload), time.Now().UTC()); err != nil {
		_ = tx.Rollback(context.Background())
		t.Fatal(err)
	}
	if err := tx.Commit(context.Background()); err != nil {
		t.Fatal(err)
	}
	created, err := store.CreateTaskWithRun(context.Background(), "shadow-tenant-"+suffix, "shadow-owner-"+suffix, "shadow task", "shadow-start-"+suffix)
	if err != nil {
		t.Fatal(err)
	}
	run, err := store.AcquireRun(context.Background(), created.Run.RunID, "shadow-worker-"+suffix, time.Now().UTC(), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	lease := integrationLease(run)
	input, err := store.GetRunContext(context.Background(), lease)
	if err != nil {
		t.Fatal(err)
	}
	if input.EvaluationMode != runcontrol.EvaluationShadow || input.ShadowWorkflow != runcontrol.WorkflowVersionV4 || input.AssignmentHash == "" {
		t.Fatalf("durable shadow context missing: %+v", input)
	}
	_, err = store.ReserveLedgerEntry(context.Background(), lease, runcontrol.LedgerEntry{
		OperationKey: "shadow-model", EntryKind: runcontrol.LedgerEntryModel,
		Operation: "generate", RequestHash: "sha256:shadow-request",
		Reservation: runcontrol.BudgetDelta{ModelAttempts: 1},
	})
	if !errors.Is(err, runcontrol.ErrNoRemoteEffects) {
		t.Fatalf("PostgreSQL shadow reservation was not denied: %v", err)
	}
	state, err := store.InspectRolloutState(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	command := runcontrol.RolloutOperatorCommand{
		CommandID: "pause-" + suffix, Kind: runcontrol.RolloutCommandPause,
		ExpectedVersion: state.Version, ReasonCode: "INTEGRATION_PROOF",
		EvidenceHash: "sha256:evidence-" + suffix, ActorRef: "integration:test",
	}
	dryRun, err := store.ApplyRolloutCommand(context.Background(), command)
	if err != nil || dryRun.Applied || !dryRun.State.Paused {
		t.Fatalf("PostgreSQL command dry-run failed: result=%+v err=%v", dryRun, err)
	}
	command.Apply = true
	applied, err := store.ApplyRolloutCommand(context.Background(), command)
	if err != nil || !applied.Applied || !applied.State.Paused {
		t.Fatalf("PostgreSQL command apply failed: result=%+v err=%v", applied, err)
	}
	replayed, err := store.ApplyRolloutCommand(context.Background(), command)
	if err != nil || !replayed.Replayed || replayed.State.Version != applied.State.Version {
		t.Fatalf("PostgreSQL command replay failed: result=%+v err=%v", replayed, err)
	}
	safePolicy := runcontrol.RolloutPolicy{PolicyVersion: "integration-safe-" + suffix, DefaultWorkflow: runcontrol.WorkflowVersionV1}
	safePayload, err := json.Marshal(safePolicy)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.pool.Exec(context.Background(), `INSERT INTO go_rollout_policies (policy_version,policy_payload,policy_hash,active,created_at) VALUES ($1,$2,$3,FALSE,$4)`, safePolicy.PolicyVersion, safePayload, integrationHash(safePayload), time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
	rollback := runcontrol.RolloutOperatorCommand{
		CommandID: "rollback-" + suffix, Kind: runcontrol.RolloutCommandRollback,
		ExpectedVersion: applied.State.Version, ReasonCode: "INTEGRATION_ROLLBACK",
		PolicyVersion: safePolicy.PolicyVersion, EvidenceHash: "sha256:rollback-" + suffix,
		ActorRef: "integration:test", Apply: true,
	}
	rolledBack, err := store.ApplyRolloutCommand(context.Background(), rollback)
	if err != nil || rolledBack.State.PolicyVersion != safePolicy.PolicyVersion || !rolledBack.State.Paused {
		t.Fatalf("PostgreSQL rollback command failed: result=%+v err=%v", rolledBack, err)
	}
	resolved, err := store.ResolveNewRun(context.Background(), runcontrol.AssignmentRequest{TenantID: "rollback-tenant", OwnerID: "rollback-owner", TaskID: "rollback-task"})
	if err != nil || resolved.PolicyVersion != safePolicy.PolicyVersion {
		t.Fatalf("rollback policy did not become assignment authority: assignment=%+v err=%v", resolved, err)
	}
	if _, err := store.CompleteRun(context.Background(), lease, runcontrol.RunFailed, time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
}

func integrationHash(value []byte) string {
	digest := sha256.Sum256(value)
	return hex.EncodeToString(digest[:])
}

func integrationLease(run runcontrol.AgentRun) runcontrol.LeaseContext {
	return runcontrol.LeaseContext{
		RunID: run.RunID, LeaseID: run.LeaseID, WorkerID: run.WorkerID,
		FencingToken: run.FencingToken, ExpiresAt: run.LeaseExpiresAt,
	}
}

func integrationOutlinePayload(t *testing.T) []byte {
	t.Helper()
	payload, err := json.Marshal(map[string]any{
		"schema_version": "outline-candidate.v1", "title": "Integration PRD", "requirement_size": "SMALL",
		"nodes": []map[string]any{
			{"node_key": "goal", "parent_key": "", "ordinal": 10, "title": "目标", "questions": []string{}, "required_content": []string{"goal"}, "unit_key": "goal"},
			{"node_key": "acceptance", "parent_key": "", "ordinal": 20, "title": "验收标准", "questions": []string{}, "required_content": []string{"precondition", "trigger", "expected_result"}, "unit_key": "acceptance"},
		},
		"units": []map[string]any{
			{"unit_key": "goal", "title": "目标", "ordinal": 10, "node_keys": []string{"goal"}, "depends_on": []string{}},
			{"unit_key": "acceptance", "title": "验收标准", "ordinal": 20, "node_keys": []string{"acceptance"}, "depends_on": []string{"goal"}},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	return payload
}

func TestPostgresStoreCreatesTaskAndInitialAgentRun(t *testing.T) {
	store, cleanup := newIsolatedPostgresStoreWithConfig(t, Config{
		MinConns:            1,
		MaxConns:            2,
		MaxGlobalRunnable:   10,
		MaxRunnablePerOwner: 10,
		RepositoryBindingID: "github:dante26979-droid/PRD-Agent",
		RepositoryRevision:  strings.Repeat("a", 40),
	})
	defer cleanup()

	suffix := strconv.FormatInt(time.Now().UnixNano(), 10)
	cachedRevision := strings.Repeat("b", 40)
	if err := store.RecordRepositoryHead(context.Background(), RepositoryHead{
		BindingID:     "github:dante26979-droid/PRD-Agent",
		Repository:    "dante26979-droid/PRD-Agent",
		DefaultBranch: "main",
		Revision:      cachedRevision,
		LastSuccessAt: time.Now().UTC(),
		LastAttemptAt: time.Now().UTC(),
	}); err != nil {
		t.Fatal(err)
	}
	created, err := store.CreateTaskWithRun(
		context.Background(),
		"integration-"+suffix,
		"owner-"+suffix,
		"create a PRD",
		"idempotency-"+suffix,
	)
	if err != nil {
		t.Fatal(err)
	}
	if created.Task.TaskID == "" || created.Run.RunID == "" {
		t.Fatalf("missing durable identifiers: %+v", created)
	}
	if created.Run.TaskID != created.Task.TaskID {
		t.Fatalf("run does not belong to task: %+v", created)
	}
	acquired, err := store.AcquireRun(context.Background(), created.Run.RunID, "repository-worker-"+suffix, time.Now().UTC(), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	input, err := store.GetRunContext(context.Background(), runcontrol.LeaseContext{
		RunID: acquired.RunID, LeaseID: acquired.LeaseID, WorkerID: acquired.WorkerID,
		FencingToken: acquired.FencingToken, ExpiresAt: acquired.LeaseExpiresAt,
	})
	if err != nil {
		t.Fatal(err)
	}
	if input.RepositoryBindingID != "github:dante26979-droid/PRD-Agent" || input.RepositoryRevision != cachedRevision {
		t.Fatalf("latest mirrored repository HEAD was not bound to task: %+v", input)
	}
	if _, err := store.CompleteRun(context.Background(), runcontrol.LeaseContext{
		RunID: acquired.RunID, LeaseID: acquired.LeaseID, WorkerID: acquired.WorkerID,
		FencingToken: acquired.FencingToken, ExpiresAt: acquired.LeaseExpiresAt,
	}, runcontrol.RunSucceeded, time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
}

func TestPostgresStorePromotesWaitingRunWithMissingScheduleCursor(t *testing.T) {
	store, cleanup := newIsolatedPostgresStoreWithConfig(t, Config{
		MinConns:            1,
		MaxConns:            2,
		MaxGlobalRunnable:   1,
		MaxRunnablePerOwner: 1,
	})
	defer cleanup()

	suffix := strconv.FormatInt(time.Now().UnixNano(), 10)
	first, err := store.CreateTaskWithRun(
		context.Background(),
		"promotion-tenant-"+suffix,
		"promotion-owner-a-"+suffix,
		"occupy the only queue slot",
		"promotion-first-"+suffix,
	)
	if err != nil {
		t.Fatal(err)
	}
	waiting, err := store.CreateTaskWithRun(
		context.Background(),
		"promotion-tenant-"+suffix,
		"promotion-owner-b-"+suffix,
		"wait for queue capacity",
		"promotion-waiting-"+suffix,
	)
	if err != nil {
		t.Fatal(err)
	}
	if waiting.Run.Status != runcontrol.RunWaitingCapacity {
		t.Fatalf("second run should wait for capacity: %+v", waiting.Run)
	}

	now := time.Now().UTC()
	acquired, err := store.AcquireRun(context.Background(), first.Run.RunID, "promotion-worker-"+suffix, now, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	lease := runcontrol.LeaseContext{
		RunID: acquired.RunID, LeaseID: acquired.LeaseID, WorkerID: acquired.WorkerID,
		FencingToken: acquired.FencingToken, ExpiresAt: acquired.LeaseExpiresAt,
	}
	if _, err := store.CompleteRun(context.Background(), lease, runcontrol.RunSucceeded, now.Add(time.Second)); err != nil {
		t.Fatal(err)
	}

	promoted, err := store.PromoteWaiting(context.Background(), now.Add(2*time.Second))
	if err != nil {
		t.Fatal(err)
	}
	if len(promoted) != 1 || promoted[0].RunID != waiting.Run.RunID || promoted[0].Status != runcontrol.RunQueued {
		t.Fatalf("waiting run was not promoted: %+v", promoted)
	}
}

func TestPostgresFixedWikiPublishLifecycleAndReconciliation(t *testing.T) {
	store, cleanup := newIsolatedPostgresStoreWithConfig(t, Config{
		MinConns: 1, MaxConns: 2,
		MaxGlobalRunnable: 10, MaxRunnablePerOwner: 10,
		ConfirmationSecret: "integration-confirmation-secret-32-bytes",
	})
	defer cleanup()

	suffix := strconv.FormatInt(time.Now().UnixNano(), 10)
	tenantID, ownerID := "publish-tenant-"+suffix, "publish-owner-"+suffix
	created, err := store.CreateTaskWithRun(context.Background(), tenantID, ownerID, "publish a PRD", "start-"+suffix)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	run, err := store.AcquireRun(context.Background(), created.Run.RunID, "agent-"+suffix, now, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	lease := runcontrol.LeaseContext{
		RunID: run.RunID, LeaseID: run.LeaseID, WorkerID: run.WorkerID,
		FencingToken: run.FencingToken, ExpiresAt: run.LeaseExpiresAt,
	}
	draft := []byte(`{"markdown":"# Integration PRD"}`)
	receipt, err := store.SubmitDraft(context.Background(), lease, "draft-"+suffix, 1, draft)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.CompleteRun(context.Background(), lease, runcontrol.RunSucceeded, now.Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	preview, err := store.CreatePublishPreview(
		context.Background(), tenantID, ownerID, created.Task.TaskID,
		"preview-"+suffix, receipt.TaskVersion, now.Add(2*time.Second),
	)
	if err != nil {
		t.Fatal(err)
	}
	record, err := store.ConfirmPublish(
		context.Background(), tenantID, ownerID, created.Task.TaskID,
		preview.PublishID, preview.ConfirmationToken, "confirm-"+suffix,
		receipt.TaskVersion, now.Add(3*time.Second),
	)
	if err != nil || record.Status != runcontrol.PublishPending {
		t.Fatalf("publish confirmation failed: %+v %v", record, err)
	}
	jobs, err := store.ClaimPendingPublishes(context.Background(), "integration-"+suffix, 1, now.Add(4*time.Second), time.Minute)
	if err != nil || len(jobs) != 1 || jobs[0].ReconcileOnly || string(jobs[0].Content) != string(draft) {
		t.Fatalf("publish claim failed: %+v %v", jobs, err)
	}
	if err := store.CompletePublish(context.Background(), "integration-"+suffix, preview.PublishID, runcontrol.PublishResult{
		Status: runcontrol.PublishReconciling, ErrorCode: "RESULT_UNKNOWN",
	}, now.Add(5*time.Second)); err != nil {
		t.Fatal(err)
	}
	jobs, err = store.ClaimPendingPublishes(context.Background(), "integration-"+suffix, 1, now.Add(16*time.Second), time.Minute)
	if err != nil || len(jobs) != 1 || !jobs[0].ReconcileOnly {
		t.Fatalf("reconciliation claim failed: %+v %v", jobs, err)
	}
	if err := store.CompletePublish(context.Background(), "integration-"+suffix, preview.PublishID, runcontrol.PublishResult{
		Status: runcontrol.PublishSucceeded, SafeURL: "https://example.feishu.cn/wiki/fixed",
		ProviderRevision: "8",
	}, now.Add(17*time.Second)); err != nil {
		t.Fatal(err)
	}
	task, err := store.GetTask(context.Background(), tenantID, ownerID, created.Task.TaskID)
	if err != nil || task.Status != "PUBLISHED" {
		t.Fatalf("task did not reach published state: %+v %v", task, err)
	}
}

func TestPostgresV4ReviewWorkflowFromOutlineToPublish(t *testing.T) {
	store, cleanup := newIsolatedPostgresStoreWithConfig(t, Config{
		MinConns: 1, MaxConns: 4,
		MaxGlobalRunnable: 10, MaxRunnablePerOwner: 10,
		ConfirmationSecret:            "integration-confirmation-secret-32-bytes",
		DefaultWorkflowVersion:        runcontrol.WorkflowVersionV4,
		DefaultExecutionLedgerVersion: runcontrol.ExecutionLedgerVersionV1,
	})
	defer cleanup()

	suffix := strconv.FormatInt(time.Now().UnixNano(), 10)
	tenantID, ownerID := "review-tenant-"+suffix, "review-owner-"+suffix
	policy := runcontrol.RolloutPolicy{
		PolicyVersion:   "integration-v4-" + suffix,
		DefaultWorkflow: runcontrol.WorkflowVersionV4,
	}
	policyPayload, err := json.Marshal(policy)
	if err != nil {
		t.Fatal(err)
	}
	tx, err := store.pool.Begin(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(context.Background(), `UPDATE go_rollout_policies SET active=FALSE WHERE active=TRUE`); err != nil {
		_ = tx.Rollback(context.Background())
		t.Fatal(err)
	}
	if _, err := tx.Exec(context.Background(), `INSERT INTO go_rollout_policies (policy_version,policy_payload,policy_hash,active,created_at) VALUES ($1,$2,$3,TRUE,$4)`, policy.PolicyVersion, policyPayload, integrationHash(policyPayload), time.Now().UTC()); err != nil {
		_ = tx.Rollback(context.Background())
		t.Fatal(err)
	}
	if err := tx.Commit(context.Background()); err != nil {
		t.Fatal(err)
	}
	created, err := store.CreateTaskWithRun(context.Background(), tenantID, ownerID, "write a reviewable PRD", "review-start-"+suffix)
	if err != nil {
		t.Fatal(err)
	}
	if created.Run.WorkflowVersion != runcontrol.WorkflowVersionV4 {
		t.Fatalf("initial run is not v4: %+v", created.Run)
	}
	firstAssignment, err := store.GetRolloutAssignment(context.Background(), created.Run.RunID)
	if err != nil {
		t.Fatal(err)
	}
	// Once bootstrapped, the durable database policy is authoritative. A
	// process-local config drift must not change assignment for a new Task.
	store.defaultWorkflowVersion = runcontrol.WorkflowVersionV1
	store.rolloutPolicy = &runcontrol.RolloutPolicy{
		PolicyVersion:   "process-drift-" + suffix,
		DefaultWorkflow: runcontrol.WorkflowVersionV1,
	}
	driftTask, err := store.CreateTaskWithRun(context.Background(), tenantID, ownerID+"-drift", "verify durable rollout authority", "review-drift-"+suffix)
	if err != nil {
		t.Fatal(err)
	}
	driftAssignment, err := store.GetRolloutAssignment(context.Background(), driftTask.Run.RunID)
	if err != nil {
		t.Fatal(err)
	}
	if driftTask.Run.WorkflowVersion != runcontrol.WorkflowVersionV4 || driftAssignment.PolicyVersion != firstAssignment.PolicyVersion {
		t.Fatalf("process config overrode durable rollout policy: first=%+v drift=%+v run=%+v", firstAssignment, driftAssignment, driftTask.Run)
	}
	driftRun, err := store.AcquireRun(context.Background(), driftTask.Run.RunID, "drift-worker-"+suffix, time.Now().UTC(), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.CompleteRun(context.Background(), integrationLease(driftRun), runcontrol.RunSucceeded, time.Now().UTC()); err != nil {
		t.Fatal(err)
	}

	now := time.Now().UTC()
	outlineRun, err := store.AcquireRun(context.Background(), created.Run.RunID, "outline-worker-"+suffix, now, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	outlineLease := integrationLease(outlineRun)
	outlineInput, err := store.GetRunContext(context.Background(), outlineLease)
	if err != nil {
		t.Fatal(err)
	}
	outlinePayload := integrationOutlinePayload(t)
	outlineReceipt, err := store.SubmitRunOutput(context.Background(), outlineLease, runcontrol.RunOutput{
		SchemaVersion: "run-output.v1", OutputKey: "outline-" + suffix,
		OutputKind: runcontrol.RunOutputOutlineCandidate, RunPurpose: runcontrol.RunPurposePlanOutline,
		ScopeHash: outlineInput.UnitScope.ScopeHash, ExpectedTaskVersion: created.Task.Version,
		ContentHash: integrationHash(outlinePayload), Payload: outlinePayload,
	})
	if err != nil {
		t.Fatal(err)
	}
	if outlineReceipt.TaskVersion != 2 {
		t.Fatalf("unexpected outline task version: %+v", outlineReceipt)
	}
	if _, err := store.CompleteRun(context.Background(), outlineLease, runcontrol.RunSucceeded, now.Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	outline, err := store.GetReviewOutline(context.Background(), tenantID, ownerID, created.Task.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	confirmCommand := runcontrol.ConfirmOutlineCommand{
		TenantID: tenantID, OwnerID: ownerID, TaskID: created.Task.TaskID,
		OutlineVersionID: outline.OutlineVersionID, IdempotencyKey: "confirm-outline-" + suffix,
		ExpectedTaskVersion: 2,
	}
	type confirmResult struct {
		transition runcontrol.ReviewTransition
		err        error
	}
	start := make(chan struct{})
	results := make(chan confirmResult, 2)
	var ready sync.WaitGroup
	ready.Add(2)
	for range 2 {
		go func() {
			ready.Done()
			<-start
			transition, confirmErr := store.ConfirmOutline(context.Background(), confirmCommand)
			results <- confirmResult{transition: transition, err: confirmErr}
		}()
	}
	ready.Wait()
	close(start)
	first, second := <-results, <-results
	if first.err != nil || second.err != nil {
		t.Fatalf("concurrent confirm outline failed: first=%v second=%v", first.err, second.err)
	}
	confirmedOutline := first.transition
	if confirmedOutline.CreatedRun == nil || second.transition.CreatedRun == nil ||
		confirmedOutline.CreatedRun.RunID != second.transition.CreatedRun.RunID ||
		confirmedOutline.EventSequence != second.transition.EventSequence {
		t.Fatalf("concurrent confirm did not replay one transition: first=%+v second=%+v", first.transition, second.transition)
	}
	replayedOutline, err := store.ConfirmOutline(context.Background(), confirmCommand)
	if err != nil || replayedOutline.CreatedRun == nil || replayedOutline.CreatedRun.RunID != confirmedOutline.CreatedRun.RunID {
		t.Fatalf("confirm outline replay failed: transition=%+v err=%v", replayedOutline, err)
	}

	materializeUnit := func(runID, worker, markdown string, expectedVersion int) runcontrol.ConfirmationUnitVersion {
		t.Helper()
		run, acquireErr := store.AcquireRun(context.Background(), runID, worker+suffix, time.Now().UTC(), time.Minute)
		if acquireErr != nil {
			t.Fatal(acquireErr)
		}
		lease := integrationLease(run)
		input, inputErr := store.GetRunContext(context.Background(), lease)
		if inputErr != nil {
			t.Fatal(inputErr)
		}
		scope := input.UnitScope
		payload, marshalErr := json.Marshal(map[string]any{
			"schema_version": "unit-candidate.v1", "unit_key": scope.CurrentUnitKey,
			"title": scope.CurrentUnitTitle, "ordinal": scope.CurrentUnitOrdinal,
			"node_keys": scope.SectionNodeKeys, "markdown": markdown,
			"content_hash": integrationHash([]byte(markdown)), "claim_ids": []string{"claim-" + scope.CurrentUnitKey},
			"unknown_ids": []string{}, "used_fact_ids": []string{},
			"quality_report": map[string]any{"outcome": "PASSED"},
		})
		if marshalErr != nil {
			t.Fatal(marshalErr)
		}
		if _, submitErr := store.SubmitRunOutput(context.Background(), lease, runcontrol.RunOutput{
			SchemaVersion: "run-output.v1", OutputKey: scope.CurrentUnitKey + "-" + suffix,
			OutputKind: runcontrol.RunOutputUnitCandidate, RunPurpose: runcontrol.RunPurposeGenerateUnit,
			ScopeHash: scope.ScopeHash, ExpectedTaskVersion: expectedVersion,
			ContentHash: integrationHash(payload), Payload: payload,
		}); submitErr != nil {
			t.Fatal(submitErr)
		}
		if _, completeErr := store.CompleteRun(context.Background(), lease, runcontrol.RunSucceeded, time.Now().UTC()); completeErr != nil {
			t.Fatal(completeErr)
		}
		items, listErr := store.ListConfirmationUnits(context.Background(), tenantID, ownerID, created.Task.TaskID)
		if listErr != nil {
			t.Fatal(listErr)
		}
		for _, item := range items {
			if item.UnitKey == scope.CurrentUnitKey {
				return item
			}
		}
		t.Fatalf("materialized unit %q not found", scope.CurrentUnitKey)
		return runcontrol.ConfirmationUnitVersion{}
	}

	goal := materializeUnit(confirmedOutline.CreatedRun.RunID, "goal-worker-", "# 目标\n\n定义产品目标。", 3)
	goalDecision, err := store.ConfirmConfirmationUnit(context.Background(), tenantID, ownerID, created.Task.TaskID, goal.UnitVersionID, "confirm-goal-"+suffix, 4)
	if err != nil || goalDecision.Run == nil {
		t.Fatalf("confirm goal: decision=%+v err=%v", goalDecision, err)
	}
	acceptance := materializeUnit(goalDecision.Run.RunID, "acceptance-worker-", "# 验收标准\n\n前置条件：已登录；触发条件：提交；预期结果：成功。", 5)
	fullDecision, err := store.ConfirmConfirmationUnit(context.Background(), tenantID, ownerID, created.Task.TaskID, acceptance.UnitVersionID, "confirm-acceptance-"+suffix, 6)
	if err != nil || fullDecision.Run == nil {
		t.Fatalf("confirm acceptance: decision=%+v err=%v", fullDecision, err)
	}

	reviewRun, err := store.AcquireRun(context.Background(), fullDecision.Run.RunID, "review-worker-"+suffix, time.Now().UTC(), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	reviewLease := integrationLease(reviewRun)
	reviewInput, err := store.GetRunContext(context.Background(), reviewLease)
	if err != nil {
		t.Fatal(err)
	}
	unitHashes := make(map[string]string, len(reviewInput.UnitScope.ConfirmedContext))
	for _, item := range reviewInput.UnitScope.ConfirmedContext {
		unitHashes[item.UnitKey] = item.ContentHash
	}
	reportPayload, err := json.Marshal(map[string]any{
		"schema_version": "full-review-report.v1", "outline_hash": reviewInput.UnitScope.OutlineHash,
		"unit_hashes": unitHashes, "outcome": "PASSED", "issues": []map[string]any{},
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.SubmitRunOutput(context.Background(), reviewLease, runcontrol.RunOutput{
		SchemaVersion: "run-output.v1", OutputKey: "full-review-" + suffix,
		OutputKind: runcontrol.RunOutputFullReviewReport, RunPurpose: runcontrol.RunPurposeFullReview,
		ScopeHash: reviewInput.UnitScope.ScopeHash, ExpectedTaskVersion: 7,
		ContentHash: integrationHash(reportPayload), Payload: reportPayload,
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := store.CompleteRun(context.Background(), reviewLease, runcontrol.RunSucceeded, time.Now().UTC()); err != nil {
		t.Fatal(err)
	}

	preview, err := store.CreatePublishPreview(context.Background(), tenantID, ownerID, created.Task.TaskID, "preview-"+suffix, 8, time.Now().UTC())
	if err != nil {
		t.Fatal(err)
	}
	if preview.ContentHash == "" || preview.DraftVersion != 1 {
		t.Fatalf("invalid v4 preview: %+v", preview)
	}
	if _, err := store.ConfirmPublish(context.Background(), tenantID, ownerID, created.Task.TaskID, preview.PublishID, preview.ConfirmationToken, "publish-"+suffix, 8, time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
	jobs, err := store.ClaimPendingPublishes(context.Background(), "publisher-"+suffix, 1, time.Now().UTC(), time.Minute)
	if err != nil || len(jobs) != 1 {
		t.Fatalf("claim v4 publish: jobs=%+v err=%v", jobs, err)
	}
	want := "# Integration PRD\n\n# 目标\n\n定义产品目标。\n\n# 验收标准\n\n前置条件：已登录；触发条件：提交；预期结果：成功。"
	if string(jobs[0].Content) != want || jobs[0].Record.ContentHash != integrationHash([]byte(want)) {
		t.Fatalf("unexpected v4 publish document: content=%q record=%+v", jobs[0].Content, jobs[0].Record)
	}
	completedAt := time.Now().UTC()
	if err := store.CompletePublish(context.Background(), "publisher-"+suffix, preview.PublishID, runcontrol.PublishResult{
		Status: runcontrol.PublishSucceeded, SafeURL: "https://example.feishu.cn/wiki/integration", ProviderRevision: "1",
	}, completedAt); err != nil {
		t.Fatal(err)
	}
	retentionLeases, err := store.ClaimExpiredPublishPayloads(context.Background(), "retention-"+suffix, completedAt.Add(25*time.Hour), 1, time.Minute)
	if err != nil || len(retentionLeases) != 1 {
		t.Fatalf("claim expired v4 payload: leases=%+v err=%v", retentionLeases, err)
	}
	if err := store.PurgePublishPayload(context.Background(), retentionLeases[0], completedAt.Add(25*time.Hour)); err != nil {
		t.Fatal(err)
	}
	var contentPurged bool
	if err := store.pool.QueryRow(context.Background(), `SELECT publish_content IS NULL AND payload_purged_at IS NOT NULL FROM go_publish_intents WHERE publish_id=$1`, preview.PublishID).Scan(&contentPurged); err != nil || !contentPurged {
		t.Fatalf("v4 payload was not purged while retaining the intent: purged=%v err=%v", contentPurged, err)
	}
}

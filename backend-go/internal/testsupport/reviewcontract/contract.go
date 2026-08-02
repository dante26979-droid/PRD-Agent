// Package reviewcontract contains the storage-independent contract for the
// authoritative v4 review workflow. Every AgentExecutionStore implementation
// must pass this suite; adapter-specific tests should only cover persistence
// and transaction details which cannot be observed through the public API.
package reviewcontract

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
)

// Factory returns a fresh store whose active rollout policy assigns new runs
// to langgraph_v4. Cleanup must release all resources owned by the adapter.
type Factory func(t *testing.T) (runcontrol.AgentExecutionStore, func())

// Run executes behavior which must be identical for the in-memory and
// PostgreSQL adapters: output replay, one-winner outline confirmation and
// dependency-ordered unit materialization.
func Run(t *testing.T, factory Factory) {
	t.Helper()
	store, cleanup := factory(t)
	defer cleanup()

	ctx := context.Background()
	suffix := fmt.Sprintf("%d", time.Now().UnixNano())
	tenantID, ownerID := "contract-tenant-"+suffix, "contract-owner-"+suffix
	created, err := store.CreateTaskWithRun(ctx, tenantID, ownerID, "write a reviewable PRD", "start-"+suffix)
	if err != nil {
		t.Fatal(err)
	}
	if created.Run.WorkflowVersion != runcontrol.WorkflowVersionV4 {
		t.Fatalf("factory did not assign v4: %+v", created.Run)
	}

	run, err := store.AcquireRun(ctx, created.Run.RunID, "contract-worker-"+suffix, time.Now().UTC(), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	lease := leaseFromRun(run)
	input, err := store.GetRunContext(ctx, lease)
	if err != nil {
		t.Fatal(err)
	}
	payload := outlinePayload(t)
	output := runcontrol.RunOutput{
		SchemaVersion: "run-output.v1", OutputKey: "outline-" + suffix,
		OutputKind: runcontrol.RunOutputOutlineCandidate, RunPurpose: runcontrol.RunPurposePlanOutline,
		ScopeHash: input.UnitScope.ScopeHash, ExpectedTaskVersion: created.Task.Version,
		ContentHash: hash(payload), Payload: payload,
	}
	firstReceipt, err := store.SubmitRunOutput(ctx, lease, output)
	if err != nil {
		t.Fatal(err)
	}
	replayedReceipt, err := store.SubmitRunOutput(ctx, lease, output)
	if err != nil {
		t.Fatal(err)
	}
	if firstReceipt != replayedReceipt || firstReceipt.TaskVersion != 2 {
		t.Fatalf("output replay changed the materialization: first=%+v replay=%+v", firstReceipt, replayedReceipt)
	}
	if _, err := store.CompleteRun(ctx, lease, runcontrol.RunSucceeded, time.Now().UTC()); err != nil {
		t.Fatal(err)
	}

	outline, err := store.GetReviewOutline(ctx, tenantID, ownerID, created.Task.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	command := runcontrol.ConfirmOutlineCommand{
		TenantID: tenantID, OwnerID: ownerID, TaskID: created.Task.TaskID,
		OutlineVersionID: outline.OutlineVersionID, IdempotencyKey: "confirm-" + suffix,
		ExpectedTaskVersion: 2,
	}
	type result struct {
		transition runcontrol.ReviewTransition
		err        error
	}
	start := make(chan struct{})
	results := make(chan result, 2)
	var ready sync.WaitGroup
	ready.Add(2)
	for i := 0; i < 2; i++ {
		go func() {
			ready.Done()
			<-start
			transition, confirmErr := store.ConfirmOutline(ctx, command)
			results <- result{transition: transition, err: confirmErr}
		}()
	}
	ready.Wait()
	close(start)
	left, right := <-results, <-results
	if left.err != nil || right.err != nil {
		t.Fatalf("concurrent confirmation failed: left=%v right=%v", left.err, right.err)
	}
	if left.transition.CreatedRun == nil || right.transition.CreatedRun == nil ||
		left.transition.CreatedRun.RunID != right.transition.CreatedRun.RunID ||
		left.transition.EventSequence != right.transition.EventSequence {
		t.Fatalf("confirmation did not have exactly one winner: left=%+v right=%+v", left.transition, right.transition)
	}

	locked, err := store.GetReviewOutline(ctx, tenantID, ownerID, created.Task.TaskID)
	if err != nil || locked.Status != runcontrol.OutlineLocked || locked.LockedAt == nil {
		t.Fatalf("outline was not locked: outline=%+v err=%v", locked, err)
	}
	units, err := store.ListReviewUnits(ctx, tenantID, ownerID, created.Task.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	if len(units) != 2 || units[0].UnitKey != "goal" || units[1].UnitKey != "acceptance" ||
		len(units[1].DependsOn) != 1 || units[1].DependsOn[0] != "goal" {
		t.Fatalf("unit dependency order diverged: %+v", units)
	}

	// Do not leak a runnable slot into another adapter contract or integration
	// test sharing the same database.
	unitRun, err := store.AcquireRun(ctx, left.transition.CreatedRun.RunID, "contract-cleanup-"+suffix, time.Now().UTC(), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.CompleteRun(ctx, leaseFromRun(unitRun), runcontrol.RunFailed, time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
}

func outlinePayload(t *testing.T) []byte {
	t.Helper()
	payload, err := json.Marshal(map[string]any{
		"schema_version": "outline-candidate.v1", "title": "Contract PRD", "requirement_size": "SMALL",
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

func hash(value []byte) string {
	digest := sha256.Sum256(value)
	return hex.EncodeToString(digest[:])
}

func leaseFromRun(run runcontrol.AgentRun) runcontrol.LeaseContext {
	return runcontrol.LeaseContext{
		RunID: run.RunID, LeaseID: run.LeaseID, WorkerID: run.WorkerID,
		FencingToken: run.FencingToken, ExpiresAt: run.LeaseExpiresAt,
	}
}

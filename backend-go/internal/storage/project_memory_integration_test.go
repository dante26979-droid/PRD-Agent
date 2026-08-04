package storage

import (
	"context"
	"testing"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
)

func TestPostgresProjectMemoryGovernanceAndWatermark(t *testing.T) {
	store, cleanup := newIsolatedPostgresStoreWithConfig(t, Config{
		MinConns: 1, MaxConns: 4, MaxGlobalRunnable: 10, MaxRunnablePerOwner: 10,
		MaxWaitingRuns: 20,
	})
	defer cleanup()
	ctx := context.Background()
	first, err := store.CreateTaskWithRun(ctx, "memory-tenant", "memory-owner", "first task", "memory-task-1")
	if err != nil {
		t.Fatal(err)
	}
	firstRun, err := store.AcquireRun(ctx, first.Run.RunID, "memory-worker-1", time.Now().UTC(), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	firstInput, err := store.GetRunContext(ctx, integrationLease(firstRun))
	if err != nil {
		t.Fatal(err)
	}
	if firstInput.MemorySpaceID == "" || firstInput.MemoryWatermark != 0 || firstInput.MemoryAssignmentHash == "" {
		t.Fatalf("first memory assignment missing: %+v", firstInput)
	}
	candidate, err := store.ProposeProjectMemory(ctx, runcontrol.ProposeMemoryCommand{
		TenantID: "memory-tenant", OwnerID: "memory-owner", SpaceID: firstInput.MemorySpaceID,
		MemoryType: runcontrol.MemoryProjectDecision, Subject: "runtime", Predicate: "control_plane",
		Value: map[string]any{"language": "go"}, Statement: "The control plane is implemented in Go.",
		AuthorityClass: runcontrol.MemoryDerivedProposal, Tags: []string{"architecture"}, IdempotencyKey: "memory-propose-1",
	})
	if err != nil {
		t.Fatal(err)
	}
	record, err := store.ConfirmProjectMemoryCandidate(ctx, runcontrol.ReviewMemoryCandidateCommand{
		TenantID: "memory-tenant", OwnerID: "memory-owner", SpaceID: firstInput.MemorySpaceID,
		CandidateID: candidate.CandidateID, ExpectedHash: candidate.RequestHash,
		ActorRef: "user:memory-owner", IdempotencyKey: "memory-confirm-1",
	})
	if err != nil {
		t.Fatal(err)
	}
	oldResult, err := store.SearchProjectMemory(ctx, runcontrol.ProjectMemorySearchRequest{TenantID: "memory-tenant", OwnerID: "memory-owner", SpaceID: firstInput.MemorySpaceID, Watermark: firstInput.MemoryWatermark, AccessScopeHash: firstInput.MemoryAccessScopeHash, Query: "control plane", Limit: 10})
	if err != nil || len(oldResult.Records) != 0 {
		t.Fatalf("future memory leaked into first run: result=%+v err=%v", oldResult, err)
	}
	second, err := store.CreateTaskWithRun(ctx, "memory-tenant", "memory-owner", "second task", "memory-task-2")
	if err != nil {
		t.Fatal(err)
	}
	secondRun, err := store.AcquireRun(ctx, second.Run.RunID, "memory-worker-2", time.Now().UTC(), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	secondInput, err := store.GetRunContext(ctx, integrationLease(secondRun))
	if err != nil {
		t.Fatal(err)
	}
	result, err := store.SearchProjectMemory(ctx, runcontrol.ProjectMemorySearchRequest{TenantID: "memory-tenant", OwnerID: "memory-owner", SpaceID: secondInput.MemorySpaceID, Watermark: secondInput.MemoryWatermark, AccessScopeHash: secondInput.MemoryAccessScopeHash, Query: "control plane", Limit: 10})
	if err != nil || secondInput.MemoryWatermark != 1 || len(result.Records) != 1 || result.Records[0].MemoryID != record.MemoryID {
		t.Fatalf("confirmed memory was not recalled: input=%+v result=%+v err=%v", secondInput, result, err)
	}
	revoked, err := store.RevokeProjectMemory(ctx, runcontrol.RevokeMemoryCommand{TenantID: "memory-tenant", OwnerID: "memory-owner", SpaceID: secondInput.MemorySpaceID, MemoryID: record.MemoryID, ExpectedVersion: 1, ActorRef: "user:memory-owner", IdempotencyKey: "memory-revoke-1"})
	if err != nil || revoked.CurrentStatus != runcontrol.MemoryRevoked {
		t.Fatalf("revoke failed: record=%+v err=%v", revoked, err)
	}
	history, err := store.GetProjectMemoryHistory(ctx, "memory-tenant", "memory-owner", secondInput.MemorySpaceID, record.MemoryID)
	if err != nil || len(history) != 2 {
		t.Fatalf("version history missing: history=%+v err=%v", history, err)
	}
}

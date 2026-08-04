package runcontrol

import (
	"context"
	"errors"
	"testing"
)

func TestProjectMemoryUsesCandidateConfirmationAndFixedRunWatermark(t *testing.T) {
	ctx := context.Background()
	store := NewMemoryStore(QueuePolicy{MaxGlobalRunnable: 2, MaxRunnablePerOwner: 2, MaxWaitingRuns: 10})
	first, err := store.CreateTaskWithRun(ctx, "tenant", "owner", "first task", "task-1")
	if err != nil {
		t.Fatal(err)
	}
	firstAssignment := store.runMemoryAssignments[first.Run.RunID]
	if firstAssignment.MemoryWatermark != 0 || firstAssignment.AssignmentHash == "" {
		t.Fatalf("unexpected first assignment: %+v", firstAssignment)
	}
	candidate, err := store.ProposeProjectMemory(ctx, ProposeMemoryCommand{
		TenantID: "tenant", OwnerID: "owner", SpaceID: firstAssignment.SpaceID,
		MemoryType: MemoryProjectDecision, Subject: "runtime", Predicate: "control_plane",
		Value: "go", Statement: "The control plane is implemented in Go.",
		AuthorityClass: MemoryDerivedProposal, Tags: []string{"architecture"},
		ReasonCode: "confirmed architecture", IdempotencyKey: "candidate-1",
	})
	if err != nil {
		t.Fatal(err)
	}
	record, err := store.ConfirmProjectMemoryCandidate(ctx, ReviewMemoryCandidateCommand{
		TenantID: "tenant", OwnerID: "owner", SpaceID: firstAssignment.SpaceID,
		CandidateID: candidate.CandidateID, ExpectedHash: candidate.RequestHash,
		ActorRef: "user:owner", IdempotencyKey: "confirm-1",
	})
	if err != nil {
		t.Fatal(err)
	}
	if record.Current.AuthorityClass != MemoryUserConfirmed || record.Current.CommittedEpoch != 1 {
		t.Fatalf("candidate was not promoted with user authority: %+v", record)
	}
	oldView, err := store.SearchProjectMemory(ctx, ProjectMemorySearchRequest{
		TenantID: "tenant", OwnerID: "owner", SpaceID: firstAssignment.SpaceID,
		Watermark: firstAssignment.MemoryWatermark, AccessScopeHash: firstAssignment.AccessScopeHash,
		Query: "control plane", Limit: 10,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(oldView.Records) != 0 {
		t.Fatalf("run-fixed watermark leaked future memory: %+v", oldView)
	}
	second, err := store.CreateTaskWithRun(ctx, "tenant", "owner", "second task", "task-2")
	if err != nil {
		t.Fatal(err)
	}
	secondAssignment := store.runMemoryAssignments[second.Run.RunID]
	if secondAssignment.MemoryWatermark != 1 {
		t.Fatalf("new run did not capture current memory epoch: %+v", secondAssignment)
	}
	currentView, err := store.SearchProjectMemory(ctx, ProjectMemorySearchRequest{
		TenantID: "tenant", OwnerID: "owner", SpaceID: secondAssignment.SpaceID,
		Watermark: secondAssignment.MemoryWatermark, AccessScopeHash: secondAssignment.AccessScopeHash,
		Query: "control plane", Limit: 10,
	})
	if err != nil || len(currentView.Records) != 1 || currentView.Records[0].MemoryID != record.MemoryID {
		t.Fatalf("current memory was not recalled: result=%+v err=%v", currentView, err)
	}
}

func TestProjectMemoryTracksConflictsAndRevokeHistory(t *testing.T) {
	ctx := context.Background()
	store := NewMemoryStore(QueuePolicy{MaxGlobalRunnable: 1, MaxRunnablePerOwner: 1, MaxWaitingRuns: 10})
	created, err := store.CreateTaskWithRun(ctx, "tenant", "owner", "task", "task")
	if err != nil {
		t.Fatal(err)
	}
	assignment := store.runMemoryAssignments[created.Run.RunID]
	confirm := func(idempotency, value string) ProjectMemoryRecord {
		candidate, err := store.ProposeProjectMemory(ctx, ProposeMemoryCommand{
			TenantID: "tenant", OwnerID: "owner", SpaceID: assignment.SpaceID,
			MemoryType: MemoryProjectConstraint, Subject: "deployment", Predicate: "region",
			Value: value, Statement: "Deployment region is " + value + ".",
			AuthorityClass: MemoryDerivedProposal, IdempotencyKey: "propose-" + idempotency,
		})
		if err != nil {
			t.Fatal(err)
		}
		record, err := store.ConfirmProjectMemoryCandidate(ctx, ReviewMemoryCandidateCommand{
			TenantID: "tenant", OwnerID: "owner", SpaceID: assignment.SpaceID,
			CandidateID: candidate.CandidateID, ExpectedHash: candidate.RequestHash,
			ActorRef: "user:owner", IdempotencyKey: "confirm-" + idempotency,
		})
		if err != nil {
			t.Fatal(err)
		}
		return record
	}
	first := confirm("one", "cn-north")
	_ = confirm("two", "cn-east")
	conflicted, err := store.SearchProjectMemory(ctx, ProjectMemorySearchRequest{
		TenantID: "tenant", OwnerID: "owner", SpaceID: assignment.SpaceID,
		Watermark: 2, AccessScopeHash: assignment.AccessScopeHash, Query: "deployment region", Limit: 10,
	})
	if err != nil || len(conflicted.Records) != 2 || len(conflicted.Conflicts) != 1 {
		t.Fatalf("conflict group was not returned: result=%+v err=%v", conflicted, err)
	}
	partial, err := store.SearchProjectMemory(ctx, ProjectMemorySearchRequest{
		TenantID: "tenant", OwnerID: "owner", SpaceID: assignment.SpaceID,
		Watermark: 2, AccessScopeHash: assignment.AccessScopeHash, Query: "deployment region", Limit: 1,
	})
	if err != nil || len(partial.Records) != 0 || len(partial.Conflicts) != 0 {
		t.Fatalf("partial conflict group must be excluded atomically: result=%+v err=%v", partial, err)
	}
	revoked, err := store.RevokeProjectMemory(ctx, RevokeMemoryCommand{
		TenantID: "tenant", OwnerID: "owner", SpaceID: assignment.SpaceID,
		MemoryID: first.MemoryID, ExpectedVersion: 1, ActorRef: "user:owner", IdempotencyKey: "revoke-1",
	})
	if err != nil || revoked.Current.Version != 2 || revoked.CurrentStatus != MemoryRevoked {
		t.Fatalf("memory revoke failed: record=%+v err=%v", revoked, err)
	}
	beforeRevoke, _ := store.SearchProjectMemory(ctx, ProjectMemorySearchRequest{TenantID: "tenant", OwnerID: "owner", SpaceID: assignment.SpaceID, Watermark: 2, AccessScopeHash: assignment.AccessScopeHash, Query: "deployment region", Limit: 10})
	afterRevoke, _ := store.SearchProjectMemory(ctx, ProjectMemorySearchRequest{TenantID: "tenant", OwnerID: "owner", SpaceID: assignment.SpaceID, Watermark: 3, AccessScopeHash: assignment.AccessScopeHash, Query: "deployment region", Limit: 10})
	if len(beforeRevoke.Records) != 2 || len(afterRevoke.Records) != 1 {
		t.Fatalf("immutable watermark history changed: before=%+v after=%+v", beforeRevoke, afterRevoke)
	}
	history, err := store.GetProjectMemoryHistory(ctx, "tenant", "owner", assignment.SpaceID, first.MemoryID)
	if err != nil || len(history) != 2 || history[0].Status != MemoryActive || history[1].Status != MemoryRevoked {
		t.Fatalf("immutable memory history missing: history=%+v err=%v", history, err)
	}
	conflictHistory, err := store.ListProjectMemoryConflicts(ctx, "tenant", "owner", assignment.SpaceID, true)
	if err != nil || len(conflictHistory) != 1 || conflictHistory[0].Status != "RESOLVED" || conflictHistory[0].ResolvedEpoch == nil || *conflictHistory[0].ResolvedEpoch != 3 {
		t.Fatalf("conflict resolution history missing: conflicts=%+v err=%v", conflictHistory, err)
	}
}

func TestProjectMemoryRejectsSensitiveCandidateAndIdempotencyDrift(t *testing.T) {
	ctx := context.Background()
	store := NewMemoryStore(QueuePolicy{MaxGlobalRunnable: 1, MaxRunnablePerOwner: 1})
	created, err := store.CreateTaskWithRun(ctx, "tenant", "owner", "task", "task")
	if err != nil {
		t.Fatal(err)
	}
	spaceID := store.runMemoryAssignments[created.Run.RunID].SpaceID
	base := ProposeMemoryCommand{TenantID: "tenant", OwnerID: "owner", SpaceID: spaceID, MemoryType: MemoryRiskNote, Subject: "credential", Predicate: "risk", Value: "redacted", Statement: "authorization: Bearer secret", AuthorityClass: MemoryDerivedProposal, IdempotencyKey: "one"}
	if _, err := store.ProposeProjectMemory(ctx, base); !errors.Is(err, ErrSensitivePayload) {
		t.Fatalf("expected sensitive payload rejection, got %v", err)
	}
	base.Statement = "Credentials must never be stored in memory."
	if _, err := store.ProposeProjectMemory(ctx, base); err != nil {
		t.Fatal(err)
	}
	base.Statement = "A changed statement."
	if _, err := store.ProposeProjectMemory(ctx, base); !errors.Is(err, ErrInvalidIdempotency) {
		t.Fatalf("expected idempotency conflict, got %v", err)
	}
}

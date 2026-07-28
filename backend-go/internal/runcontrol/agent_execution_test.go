package runcontrol

import (
	"context"
	"testing"
	"time"
)

func newLeasedMemoryRun(t *testing.T) (*MemoryStore, AgentRun, LeaseContext) {
	t.Helper()
	store := NewMemoryStore(QueuePolicy{MaxGlobalRunnable: 1, MaxRunnablePerOwner: 1})
	created, err := store.CreateTaskWithRun(context.Background(), "tenant", "alice", "write a PRD", "start")
	if err != nil {
		t.Fatal(err)
	}
	run, err := store.AcquireRun(context.Background(), created.Run.RunID, "worker-a", time.Now().UTC(), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	lease := LeaseContext{RunID: run.RunID, LeaseID: run.LeaseID, WorkerID: run.WorkerID, FencingToken: run.FencingToken, ExpiresAt: run.LeaseExpiresAt}
	return store, run, lease
}

func TestMemoryStoreReturnsRunContextAndResumesCheckpoint(t *testing.T) {
	store, run, lease := newLeasedMemoryRun(t)
	input, err := store.GetRunContext(context.Background(), lease)
	if err != nil {
		t.Fatal(err)
	}
	if input.Run.RunID != run.RunID || input.TaskMessage != "write a PRD" || len(input.Checkpoint) != 0 {
		t.Fatalf("unexpected first context: %+v", input)
	}
	if _, err := store.SaveCheckpoint(context.Background(), lease, 1, []byte(`{"node":"outline"}`)); err != nil {
		t.Fatal(err)
	}
	resumed, err := store.GetRunContext(context.Background(), lease)
	if err != nil {
		t.Fatal(err)
	}
	if resumed.CheckpointSequence != 1 || string(resumed.Checkpoint) != `{"node":"outline"}` {
		t.Fatalf("checkpoint was not resumed: %+v", resumed)
	}
}

func TestMemoryStoreRejectsStaleCheckpointAndAllowsIdenticalReplay(t *testing.T) {
	store, _, lease := newLeasedMemoryRun(t)
	if _, err := store.SaveCheckpoint(context.Background(), lease, 2, []byte("state")); err != nil {
		t.Fatal(err)
	}
	replay, err := store.SaveCheckpoint(context.Background(), lease, 2, []byte("state"))
	if err != nil || replay.Sequence != 2 {
		t.Fatalf("identical checkpoint replay failed: %+v %v", replay, err)
	}
	if _, err := store.SaveCheckpoint(context.Background(), lease, 1, []byte("old")); err != ErrCheckpointConflict {
		t.Fatalf("expected old checkpoint conflict, got %v", err)
	}
	if _, err := store.SaveCheckpoint(context.Background(), lease, 2, []byte("different")); err != ErrCheckpointConflict {
		t.Fatalf("expected same-sequence conflict, got %v", err)
	}
}

func TestMemoryStoreModelAttemptAndEvidenceAreIdempotent(t *testing.T) {
	store, _, lease := newLeasedMemoryRun(t)
	attempt := ModelAttempt{AttemptKey: "model-1", Operation: "outline", RequestHash: "hash-a", Status: "SUCCEEDED"}
	first, err := store.RecordModelAttempt(context.Background(), lease, attempt)
	if err != nil {
		t.Fatal(err)
	}
	replay, err := store.RecordModelAttempt(context.Background(), lease, attempt)
	if err != nil || replay != first {
		t.Fatalf("attempt replay was not idempotent: %q %q %v", first, replay, err)
	}
	attempt.RequestHash = "hash-b"
	if _, err := store.RecordModelAttempt(context.Background(), lease, attempt); err != ErrInvalidIdempotency {
		t.Fatalf("expected attempt conflict, got %v", err)
	}
	items := []EvidenceItem{{SourceType: "repository", SourceID: "repo-1", Locator: "README.md:1", ExcerptHash: "sha", Excerpt: "text"}}
	accepted, err := store.AppendEvidence(context.Background(), lease, items)
	if err != nil || accepted != 1 {
		t.Fatalf("first evidence append failed: %d %v", accepted, err)
	}
	accepted, err = store.AppendEvidence(context.Background(), lease, items)
	if err != nil || accepted != 0 {
		t.Fatalf("duplicate evidence was not ignored: %d %v", accepted, err)
	}
}

func TestMemoryStoreRejectsSensitiveAttemptMetadata(t *testing.T) {
	store, _, lease := newLeasedMemoryRun(t)
	attempt := ModelAttempt{AttemptKey: "model-1", Operation: "outline", RequestHash: "hash-a", ResponseMetadataJSON: `{"authorization":"secret"}`}
	if _, err := store.RecordModelAttempt(context.Background(), lease, attempt); err != ErrSensitivePayload {
		t.Fatalf("expected sensitive payload rejection, got %v", err)
	}
}

func TestMemoryStoreDraftUsesTaskVersionAndReplayKey(t *testing.T) {
	store, run, lease := newLeasedMemoryRun(t)
	first, err := store.SubmitDraft(context.Background(), lease, "draft-1", 1, []byte("draft"))
	if err != nil || first.TaskVersion != 2 {
		t.Fatalf("draft submit failed: %+v %v", first, err)
	}
	replay, err := store.SubmitDraft(context.Background(), lease, "draft-1", 1, []byte("draft"))
	if err != nil || replay.TaskVersion != first.TaskVersion {
		t.Fatalf("draft replay failed: %+v %v", replay, err)
	}
	if _, err := store.SubmitDraft(context.Background(), lease, "draft-2", 1, []byte("other")); err != ErrTaskVersionConflict {
		t.Fatalf("expected task version conflict, got %v", err)
	}
	if task, err := store.GetTask(context.Background(), "tenant", "alice", run.TaskID); err != nil || task.Version != 2 {
		t.Fatalf("task version changed unexpectedly: %+v %v", task, err)
	}
}

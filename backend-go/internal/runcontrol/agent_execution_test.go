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

func TestMemoryStoreRunArtifactIsDurableAndIdempotent(t *testing.T) {
	store, _, lease := newLeasedMemoryRun(t)
	content := []byte(`{"schema_version":"draft-bundle.v1"}`)
	artifact := RunArtifact{
		ArtifactKey:  lease.RunID + ":draft_bundle:1",
		ArtifactType: "DRAFT_BUNDLE",
		Generation:   1,
		RequestHash:  "request-a",
		ContentHash:  stableHash(string(content)),
		Content:      content,
	}
	first, err := store.SaveRunArtifact(context.Background(), lease, artifact)
	if err != nil {
		t.Fatal(err)
	}
	replay, err := store.SaveRunArtifact(context.Background(), lease, artifact)
	if err != nil || replay != first {
		t.Fatalf("artifact replay failed: %+v %+v %v", first, replay, err)
	}
	resumed, err := store.GetRunContext(context.Background(), lease)
	if err != nil {
		t.Fatal(err)
	}
	if len(resumed.ResumeArtifacts) != 1 || string(resumed.ResumeArtifacts[0].Content) != string(content) {
		t.Fatalf("artifact was not included in resume context: %+v", resumed.ResumeArtifacts)
	}
	artifact.Content = []byte(`{"different":true}`)
	artifact.ContentHash = stableHash(string(artifact.Content))
	if _, err := store.SaveRunArtifact(context.Background(), lease, artifact); err != ErrInvalidIdempotency {
		t.Fatalf("expected artifact idempotency conflict, got %v", err)
	}
}

func TestMemoryStoreAdvancesPlannedAttemptAfterProviderResult(t *testing.T) {
	store, run, lease := newLeasedMemoryRun(t)
	planned := ModelAttempt{
		AttemptKey: "model-1", Operation: "draft", Provider: "deepseek",
		RequestHash: "hash-a", Status: "PLANNED",
	}
	attemptID, err := store.RecordModelAttempt(context.Background(), lease, planned)
	if err != nil {
		t.Fatal(err)
	}
	completed := planned
	completed.Status = "SUCCEEDED"
	completed.ResponseMetadataJSON = `{"request_id":"redacted"}`
	replayedID, err := store.RecordModelAttempt(context.Background(), lease, completed)
	if err != nil || replayedID != attemptID {
		t.Fatalf("planned attempt was not advanced in place: %q %q %v", attemptID, replayedID, err)
	}
	items, err := store.ListModelAttempts(context.Background(), "tenant", "alice", run.TaskID, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(items) != 1 || items[0].Status != "SUCCEEDED" {
		t.Fatalf("unexpected attempt projection: %+v", items)
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

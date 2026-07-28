package runcontrol

import (
	"context"
	"testing"
	"time"
)

func TestMemoryStorePlacesSecondOwnerRunInCapacityWait(t *testing.T) {
	store := NewMemoryStore(QueuePolicy{MaxGlobalRunnable: 1, MaxRunnablePerOwner: 1})
	first, err := store.CreateTaskWithRun(context.Background(), "tenant", "alice", "first", "one")
	if err != nil {
		t.Fatal(err)
	}
	second, err := store.CreateTaskWithRun(context.Background(), "tenant", "bob", "second", "two")
	if err != nil {
		t.Fatal(err)
	}
	if first.Run.Status != RunQueued || !first.Run.QueueSlotAcquired {
		t.Fatalf("first run was not admitted: %+v", first.Run)
	}
	if second.Run.Status != RunWaitingCapacity || second.Run.QueueSlotAcquired {
		t.Fatalf("second run was not capacity-blocked: %+v", second.Run)
	}
}

func TestMemoryStoreFencesExpiredWorker(t *testing.T) {
	store := NewMemoryStore(QueuePolicy{MaxGlobalRunnable: 1, MaxRunnablePerOwner: 1})
	created, err := store.CreateTaskWithRun(context.Background(), "tenant", "alice", "first", "one")
	if err != nil {
		t.Fatal(err)
	}
	now := time.Unix(100, 0).UTC()
	first, err := store.AcquireRun(context.Background(), created.Run.RunID, "worker-a", now, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.AcquireRun(context.Background(), created.Run.RunID, "worker-b", now.Add(500*time.Millisecond), time.Second); err != ErrLeaseHeld {
		t.Fatalf("expected live lease to be held, got %v", err)
	}
	second, err := store.AcquireRun(context.Background(), created.Run.RunID, "worker-b", now.Add(2*time.Second), time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if second.FencingToken <= first.FencingToken {
		t.Fatalf("fencing token did not advance: %d -> %d", first.FencingToken, second.FencingToken)
	}
	if _, err := store.CompleteRun(context.Background(), LeaseContext{RunID: first.RunID, LeaseID: first.LeaseID, WorkerID: first.WorkerID, FencingToken: first.FencingToken}, RunSucceeded, now.Add(2*time.Second)); err != ErrLeaseLost {
		t.Fatalf("expected stale lease rejection, got %v", err)
	}
	if _, err := store.CompleteRun(context.Background(), LeaseContext{RunID: second.RunID, LeaseID: second.LeaseID, WorkerID: second.WorkerID, FencingToken: second.FencingToken}, RunSucceeded, now.Add(2*time.Second)); err != nil {
		t.Fatal(err)
	}
}

func TestMemoryStorePromotesWaitingRunAndPublishesOutbox(t *testing.T) {
	store := NewMemoryStore(QueuePolicy{MaxGlobalRunnable: 1, MaxRunnablePerOwner: 1})
	now := time.Now().UTC()
	first, err := store.CreateTaskWithRun(context.Background(), "tenant", "alice", "first", "one")
	if err != nil {
		t.Fatal(err)
	}
	second, err := store.CreateTaskWithRun(context.Background(), "tenant", "bob", "second", "two")
	if err != nil {
		t.Fatal(err)
	}
	lease, err := store.AcquireRun(context.Background(), first.Run.RunID, "worker-a", now, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.CompleteRun(context.Background(), LeaseContext{RunID: lease.RunID, LeaseID: lease.LeaseID, WorkerID: lease.WorkerID, FencingToken: lease.FencingToken}, RunSucceeded, now.Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	promoted, err := store.PromoteWaiting(context.Background(), now.Add(2*time.Second))
	if err != nil {
		t.Fatal(err)
	}
	if len(promoted) != 1 || promoted[0].RunID != second.Run.RunID || promoted[0].Status != RunQueued {
		t.Fatalf("unexpected promotions: %+v", promoted)
	}
	outbox, err := store.ClaimOutbox(context.Background(), 10, now.Add(3*time.Second))
	if err != nil {
		t.Fatal(err)
	}
	if len(outbox) != 2 {
		t.Fatalf("expected two run requests, got %d", len(outbox))
	}
	if err := store.MarkOutboxPublished(context.Background(), outbox[0].MessageID, now.Add(4*time.Second)); err != nil {
		t.Fatal(err)
	}
	if err := store.MarkOutboxFailed(context.Background(), outbox[1].MessageID, now.Add(100*time.Second)); err != nil {
		t.Fatal(err)
	}
	retry, err := store.ClaimOutbox(context.Background(), 10, now.Add(101*time.Second))
	if err != nil {
		t.Fatal(err)
	}
	if len(retry) != 1 || retry[0].MessageID != outbox[1].MessageID {
		t.Fatalf("unexpected retry batch: %+v", retry)
	}
}

func TestMemoryStoreRejectsIdempotencyKeyReuseWithDifferentMessage(t *testing.T) {
	store := NewMemoryStore(QueuePolicy{MaxGlobalRunnable: 30, MaxRunnablePerOwner: 1})
	if _, err := store.CreateTaskWithRun(context.Background(), "tenant", "alice", "first", "same-key"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.CreateTaskWithRun(context.Background(), "tenant", "alice", "second", "same-key"); err != ErrInvalidIdempotency {
		t.Fatalf("expected idempotency conflict, got %v", err)
	}
}

package runcontrol

import (
	"context"
	"testing"
)

func TestMemoryStoreTaskEventsAreOrderedAndCursorScoped(t *testing.T) {
	store := NewMemoryStore(QueuePolicy{MaxGlobalRunnable: 10, MaxRunnablePerOwner: 1})
	created, err := store.CreateTaskWithRun(context.Background(), "tenant", "alice", "message", "start")
	if err != nil {
		t.Fatal(err)
	}
	first, err := store.ListTaskEvents(context.Background(), "tenant", "alice", created.Task.TaskID, 0, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(first) != 1 || first[0].Sequence != 1 || first[0].EventType != "task.created" {
		t.Fatalf("unexpected creation event: %+v", first)
	}

	appended, err := store.AppendTaskEvent(context.Background(), "tenant", "alice", created.Task.TaskID, "run.queued", []byte(`{"run_id":"`+created.Run.RunID+`"}`))
	if err != nil {
		t.Fatal(err)
	}
	if appended.Sequence != 2 {
		t.Fatalf("expected sequence 2, got %d", appended.Sequence)
	}
	items, err := store.ListTaskEvents(context.Background(), "tenant", "alice", created.Task.TaskID, 1, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(items) != 1 || items[0].Sequence != 2 {
		t.Fatalf("cursor did not skip acknowledged event: %+v", items)
	}
	if _, err := store.ListTaskEvents(context.Background(), "tenant", "bob", created.Task.TaskID, 0, 10); err != ErrNotFound {
		t.Fatalf("expected owner isolation, got %v", err)
	}
}

func TestMemoryStoreRejectsInvalidTaskEventPayload(t *testing.T) {
	store := NewMemoryStore(QueuePolicy{MaxGlobalRunnable: 10, MaxRunnablePerOwner: 1})
	created, err := store.CreateTaskWithRun(context.Background(), "tenant", "alice", "message", "start")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.AppendTaskEvent(context.Background(), "tenant", "alice", created.Task.TaskID, "run.failed", []byte("not-json")); err != ErrInvalidPayload {
		t.Fatalf("expected invalid payload, got %v", err)
	}
}

func TestMemoryStoreDispatchIsIdempotentByRunAttempt(t *testing.T) {
	store := NewMemoryStore(QueuePolicy{MaxGlobalRunnable: 10, MaxRunnablePerOwner: 1})
	created, err := store.CreateTaskWithRun(context.Background(), "tenant", "alice", "message", "start")
	if err != nil {
		t.Fatal(err)
	}
	dispatch := AgentDispatch{DispatchID: "dispatch-1", RunID: created.Run.RunID, TenantID: "tenant", OwnerID: "alice", WorkerID: "worker-a", AttemptNo: 1, RequestHash: "hash-1", Status: DispatchStarted}
	first, err := store.CreateDispatch(context.Background(), dispatch)
	if err != nil {
		t.Fatal(err)
	}
	replay, err := store.CreateDispatch(context.Background(), dispatch)
	if err != nil || replay.DispatchID != first.DispatchID {
		t.Fatalf("dispatch replay failed: %+v %v", replay, err)
	}
	dispatch.RequestHash = "hash-2"
	if _, err := store.CreateDispatch(context.Background(), dispatch); err != ErrInvalidIdempotency {
		t.Fatalf("expected dispatch idempotency conflict, got %v", err)
	}
	if err := store.UpdateDispatch(context.Background(), first.DispatchID, DispatchUnknown, "stream reset", nil); err != nil {
		t.Fatal(err)
	}
	unknown, err := store.ListDispatches(context.Background(), DispatchUnknown, 10)
	if err != nil || len(unknown) != 1 || unknown[0].LastError != "stream reset" {
		t.Fatalf("unknown dispatch was not recorded: %+v %v", unknown, err)
	}
}

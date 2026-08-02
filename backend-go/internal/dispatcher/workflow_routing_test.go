package dispatcher

import (
	"context"
	"testing"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/agentpool"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
)

func TestDispatcherLeavesIncompatibleRunQueuedBeforeLease(t *testing.T) {
	store := runcontrol.NewMemoryStore(runcontrol.QueuePolicy{
		MaxGlobalRunnable:      10,
		MaxRunnablePerOwner:    1,
		DefaultWorkflowVersion: runcontrol.WorkflowVersionV4,
	})
	created, err := store.CreateTaskWithRun(context.Background(), "tenant", "owner", "message", "idem")
	if err != nil {
		t.Fatal(err)
	}
	pool, err := agentpool.NewPool([]agentpool.ClientSlot{
		{WorkerID: "legacy", Client: worker{stream: &stream{}}, MaxInflight: 1},
	})
	if err != nil {
		t.Fatal(err)
	}
	dispatcher, err := New(store, pool, time.Minute, 10)
	if err != nil {
		t.Fatal(err)
	}
	report, err := dispatcher.DispatchOnce(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if report.Incompatible != 1 || report.Processed != 0 {
		t.Fatalf("unexpected report: %+v", report)
	}
	run, err := store.GetRun(context.Background(), created.Run.RunID)
	if err != nil {
		t.Fatal(err)
	}
	if run.Status != runcontrol.RunQueued || run.AttemptCount != 0 || run.LeaseID != "" {
		t.Fatalf("incompatible run was leased or mutated: %+v", run)
	}
}

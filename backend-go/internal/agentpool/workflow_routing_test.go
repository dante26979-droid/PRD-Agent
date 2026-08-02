package agentpool

import "testing"

func TestReserveForWorkflowFiltersBeforeCapacity(t *testing.T) {
	legacy := &fakeClient{}
	modern := &fakeClient{}
	pool, err := NewPool([]ClientSlot{
		{WorkerID: "legacy", Client: legacy, MaxInflight: 1},
		{WorkerID: "modern", Client: modern, MaxInflight: 1, SupportedWorkflowVersions: []string{"agent-runtime.v1", "agent-runtime.v4"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	reservation, err := pool.ReserveForWorkflow("agent-runtime.v4")
	if err != nil {
		t.Fatal(err)
	}
	defer reservation.Release()
	if reservation.WorkerID() != "modern" {
		t.Fatalf("selected %q, want modern", reservation.WorkerID())
	}
}

func TestReserveForWorkflowDistinguishesIncompatibleFromSaturated(t *testing.T) {
	pool, err := NewPool([]ClientSlot{{WorkerID: "legacy", Client: &fakeClient{}, MaxInflight: 1}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := pool.ReserveForWorkflow("agent-runtime.v4"); err != ErrNoCompatibleWorker {
		t.Fatalf("error = %v, want ErrNoCompatibleWorker", err)
	}
	first, err := pool.ReserveForWorkflow("agent-runtime.v1")
	if err != nil {
		t.Fatal(err)
	}
	defer first.Release()
	if _, err := pool.ReserveForWorkflow("agent-runtime.v1"); err != ErrPoolSaturated {
		t.Fatalf("error = %v, want ErrPoolSaturated", err)
	}
}

func TestReserveForRunRequiresExecutionLedgerCapability(t *testing.T) {
	legacy := &fakeClient{}
	ledger := &fakeClient{}
	pool, err := NewPool([]ClientSlot{
		{WorkerID: "legacy", Client: legacy, MaxInflight: 1, SupportedWorkflowVersions: []string{"agent-runtime.v4"}},
		{WorkerID: "ledger", Client: ledger, MaxInflight: 1, SupportedWorkflowVersions: []string{"agent-runtime.v4"}, SupportedExecutionLedgerVersions: []string{"run-ledger.v1"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	reservation, err := pool.ReserveForRun("agent-runtime.v4", "run-ledger.v1")
	if err != nil {
		t.Fatal(err)
	}
	defer reservation.Release()
	if reservation.WorkerID() != "ledger" {
		t.Fatalf("ledger run routed to %q", reservation.WorkerID())
	}
}

func TestReserveForRunRejectsLedgerRunWhenOnlyLegacyWorkerExists(t *testing.T) {
	pool, err := NewPool([]ClientSlot{{WorkerID: "legacy", Client: &fakeClient{}, SupportedWorkflowVersions: []string{"agent-runtime.v4"}}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := pool.ReserveForRun("agent-runtime.v4", "run-ledger.v1"); err != ErrNoCompatibleWorker {
		t.Fatalf("expected incompatible worker, got %v", err)
	}
}

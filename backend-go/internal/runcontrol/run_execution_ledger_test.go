package runcontrol

import (
	"context"
	"errors"
	"testing"
	"time"
)

func newLedgerMemoryRun(t *testing.T, budget RunBudget) (*MemoryStore, LeaseContext) {
	t.Helper()
	store := NewMemoryStore(QueuePolicy{
		MaxGlobalRunnable: 1, MaxRunnablePerOwner: 1,
		DefaultWorkflowVersion:        WorkflowVersionV4,
		DefaultExecutionLedgerVersion: ExecutionLedgerVersionV1,
		DefaultRunBudget:              budget,
	})
	created, err := store.CreateTaskWithRun(context.Background(), "tenant", "owner", "ledger", "ledger-start")
	if err != nil {
		t.Fatal(err)
	}
	run, err := store.AcquireRun(context.Background(), created.Run.RunID, "worker", time.Now().UTC(), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	return store, leaseFromRun(run)
}

func TestMemoryLedgerRemoteLifecycleIsIdempotentAndBudgeted(t *testing.T) {
	store, lease := newLedgerMemoryRun(t, RunBudget{MaxModelAttempts: 1, MaxInputTokens: 100, MaxOutputTokens: 50, MaxElapsedMS: 1000})
	entry := LedgerEntry{OperationKey: "model:outline:iteration:1", EntryKind: LedgerEntryModel, Operation: "outline", RequestHash: "sha256:a", Reservation: BudgetDelta{ModelAttempts: 1, InputTokens: 40, OutputTokens: 20}}
	reserved, err := store.ReserveLedgerEntry(context.Background(), lease, entry)
	if err != nil || reserved.Status != LedgerReserved {
		t.Fatalf("reserve = %+v, %v", reserved, err)
	}
	replayed, err := store.ReserveLedgerEntry(context.Background(), lease, entry)
	if err != nil || replayed.EntryID != reserved.EntryID {
		t.Fatalf("reserve replay = %+v, %v", replayed, err)
	}
	conflict := entry
	conflict.RequestHash = "sha256:b"
	if _, err := store.ReserveLedgerEntry(context.Background(), lease, conflict); !errors.Is(err, ErrInvalidIdempotency) {
		t.Fatalf("expected identity conflict, got %v", err)
	}
	if _, err := store.ReserveLedgerEntry(context.Background(), lease, LedgerEntry{OperationKey: "model:outline:iteration:2", EntryKind: LedgerEntryModel, Operation: "outline", RequestHash: "sha256:c", Reservation: BudgetDelta{ModelAttempts: 1}}); !errors.Is(err, ErrBudgetExhausted) {
		t.Fatalf("expected active reservation to exhaust model budget, got %v", err)
	}
	started, err := store.MarkLedgerCallStarted(context.Background(), lease, entry.OperationKey, entry.RequestHash)
	if err != nil || started.Status != LedgerCallStarted {
		t.Fatalf("call started = %+v, %v", started, err)
	}
	artifact := RunArtifact{ArtifactKey: lease.RunID + ":ledger:outcome", ArtifactType: "MODEL_VALIDATED_OUTPUT", RequestHash: entry.RequestHash, ContentHash: stableHash("result"), Content: []byte("result")}
	if _, err := store.SaveRunArtifact(context.Background(), lease, artifact); err != nil {
		t.Fatal(err)
	}
	finish := LedgerFinish{OperationKey: entry.OperationKey, RequestHash: entry.RequestHash, Status: LedgerSucceeded, Consumption: BudgetDelta{ModelAttempts: 1, InputTokens: 35, OutputTokens: 12}, OutputArtifactKey: artifact.ArtifactKey, OutputArtifactHash: artifact.ContentHash}
	completed, err := store.FinishLedgerEntry(context.Background(), lease, finish)
	if err != nil || completed.Status != LedgerSucceeded {
		t.Fatalf("finish = %+v, %v", completed, err)
	}
	if replay, err := store.FinishLedgerEntry(context.Background(), lease, finish); err != nil || replay.EntryID != completed.EntryID {
		t.Fatalf("finish replay = %+v, %v", replay, err)
	}
	input, err := store.GetRunContext(context.Background(), lease)
	if err != nil {
		t.Fatal(err)
	}
	if input.ConsumedBudget.ModelAttempts != 1 || len(input.LedgerEntries) != 1 {
		t.Fatalf("durable context = %+v", input)
	}
}

func TestMemoryLedgerCallStartedCannotBeReplayedOrReReserved(t *testing.T) {
	store, lease := newLedgerMemoryRun(t, RunBudget{MaxToolCalls: 1, MaxElapsedMS: 1000})
	entry := LedgerEntry{OperationKey: "capability:search:1", EntryKind: LedgerEntryCapability, Operation: "search", RequestHash: "sha256:a", Reservation: BudgetDelta{ToolCalls: 1}}
	if _, err := store.ReserveLedgerEntry(context.Background(), lease, entry); err != nil {
		t.Fatal(err)
	}
	if _, err := store.MarkLedgerCallStarted(context.Background(), lease, entry.OperationKey, entry.RequestHash); err != nil {
		t.Fatal(err)
	}
	unknown := LedgerFinish{OperationKey: entry.OperationKey, RequestHash: entry.RequestHash, Status: LedgerOutcomeUnknown, Consumption: BudgetDelta{ToolCalls: 1}, ErrorCategory: "OUTCOME_UNKNOWN"}
	if _, err := store.FinishLedgerEntry(context.Background(), lease, unknown); err != nil {
		t.Fatal(err)
	}
	if _, err := store.MarkLedgerCallStarted(context.Background(), lease, entry.OperationKey, entry.RequestHash); !errors.Is(err, ErrLedgerConflict) {
		t.Fatalf("expected terminal replay rejection, got %v", err)
	}
	if replay, err := store.ReserveLedgerEntry(context.Background(), lease, entry); err != nil || replay.Status != LedgerOutcomeUnknown {
		t.Fatalf("terminal reserve replay = %+v, %v", replay, err)
	}
}

func TestMemoryLedgerConsumesLocalTransitionAtomically(t *testing.T) {
	store, lease := newLedgerMemoryRun(t, RunBudget{MaxIterations: 1, MaxElapsedMS: 1000})
	entry := LedgerEntry{OperationKey: "transition:iteration:1", EntryKind: LedgerEntryLocalTransition, Operation: "iteration", RequestHash: "sha256:i1", Consumption: BudgetDelta{Iterations: 1}}
	created, err := store.ConsumeLocalLedgerEntry(context.Background(), lease, entry)
	if err != nil || created.Status != LedgerSucceeded {
		t.Fatalf("consume = %+v, %v", created, err)
	}
	if _, err := store.ConsumeLocalLedgerEntry(context.Background(), lease, LedgerEntry{OperationKey: "transition:iteration:2", EntryKind: LedgerEntryLocalTransition, Operation: "iteration", RequestHash: "sha256:i2", Consumption: BudgetDelta{Iterations: 1}}); !errors.Is(err, ErrBudgetExhausted) {
		t.Fatalf("expected iteration budget exhaustion, got %v", err)
	}
}

func TestMemoryLedgerPersistsLocalDerivationArtifact(t *testing.T) {
	store, lease := newLedgerMemoryRun(t, RunBudget{MaxElapsedMS: 1000})
	entry := LedgerEntry{
		OperationKey: "context:build:outline:sequence1",
		EntryKind:    LedgerEntryLocalDerivation,
		Operation:    "build_context_pack",
		RequestHash:  "sha256:context",
	}
	if _, err := store.ReserveLedgerEntry(context.Background(), lease, entry); err != nil {
		t.Fatal(err)
	}
	if _, err := store.MarkLedgerCallStarted(context.Background(), lease, entry.OperationKey, entry.RequestHash); err != nil {
		t.Fatal(err)
	}
	artifact := RunArtifact{
		ArtifactKey:  lease.RunID + ":context-pack:1",
		ArtifactType: "CONTEXT_PACK",
		RequestHash:  entry.RequestHash,
		ContentHash:  stableHash("context-pack"),
		Content:      []byte("context-pack"),
	}
	if _, err := store.SaveRunArtifact(context.Background(), lease, artifact); err != nil {
		t.Fatal(err)
	}
	completed, err := store.FinishLedgerEntry(context.Background(), lease, LedgerFinish{
		OperationKey:       entry.OperationKey,
		RequestHash:        entry.RequestHash,
		Status:             LedgerSucceeded,
		OutputArtifactKey:  artifact.ArtifactKey,
		OutputArtifactHash: artifact.ContentHash,
	})
	if err != nil || completed.EntryKind != LedgerEntryLocalDerivation {
		t.Fatalf("derivation finish = %+v, %v", completed, err)
	}
}

func TestMemoryLedgerEnforcesServerElapsedBudgetBeforeRemoteCall(t *testing.T) {
	store, lease := newLedgerMemoryRun(t, RunBudget{MaxModelAttempts: 1, MaxElapsedMS: 10})
	store.mu.Lock()
	budget := store.budgets[lease.RunID]
	budget.startedAt = time.Now().Add(-time.Second)
	store.budgets[lease.RunID] = budget
	store.mu.Unlock()
	_, err := store.ReserveLedgerEntry(context.Background(), lease, LedgerEntry{OperationKey: "model:outline:iteration:1", EntryKind: LedgerEntryModel, Operation: "outline", RequestHash: "sha256:a", Reservation: BudgetDelta{ModelAttempts: 1}})
	if !errors.Is(err, ErrBudgetExhausted) {
		t.Fatalf("expected elapsed budget exhaustion, got %v", err)
	}
}

func TestMemoryLedgerRecordsTokenOverageAndBlocksNextReservation(t *testing.T) {
	store, lease := newLedgerMemoryRun(t, RunBudget{MaxModelAttempts: 2, MaxInputTokens: 10, MaxElapsedMS: 1000})
	entry := LedgerEntry{OperationKey: "model:outline:iteration:1", EntryKind: LedgerEntryModel, Operation: "outline", RequestHash: "sha256:a", Reservation: BudgetDelta{ModelAttempts: 1, InputTokens: 5}}
	if _, err := store.ReserveLedgerEntry(context.Background(), lease, entry); err != nil {
		t.Fatal(err)
	}
	if _, err := store.MarkLedgerCallStarted(context.Background(), lease, entry.OperationKey, entry.RequestHash); err != nil {
		t.Fatal(err)
	}
	if _, err := store.FinishLedgerEntry(context.Background(), lease, LedgerFinish{OperationKey: entry.OperationKey, RequestHash: entry.RequestHash, Status: LedgerFailed, Consumption: BudgetDelta{ModelAttempts: 1, InputTokens: 15}, ErrorCategory: "VALIDATION_FAILED"}); err != nil {
		t.Fatal(err)
	}
	if store.budgets[lease.RunID].overage.InputTokens != 5 {
		t.Fatalf("overage = %+v", store.budgets[lease.RunID].overage)
	}
	_, err := store.ReserveLedgerEntry(context.Background(), lease, LedgerEntry{OperationKey: "model:outline:iteration:2", EntryKind: LedgerEntryModel, Operation: "outline", RequestHash: "sha256:b", Reservation: BudgetDelta{ModelAttempts: 1}})
	if !errors.Is(err, ErrBudgetExhausted) {
		t.Fatalf("expected overage to block next call, got %v", err)
	}
}

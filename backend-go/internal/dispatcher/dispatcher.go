// Package dispatcher converts durable QUEUED Agent Runs into bounded direct
// Go -> Python AgentWorkerService executions.
package dispatcher

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"sync"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/agentpool"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	agentv1 "github.com/dante26979-droid/prd-agent/contracts/gen/go/agent/v1"
)

const contractVersion = "agent-execution.v1"

type Dispatcher struct {
	store          runcontrol.DispatchStore
	pool           *agentpool.Pool
	leaseTTL       time.Duration
	maxBatch       int
	executeTimeout time.Duration
	now            func() time.Time
}

type Report struct {
	Processed int
	Succeeded int
	Failed    int
	Unknown   int
	Saturated int
}

func New(store runcontrol.DispatchStore, pool *agentpool.Pool, leaseTTL time.Duration, maxBatch int) (*Dispatcher, error) {
	if store == nil || pool == nil {
		return nil, fmt.Errorf("dispatch store and agent pool are required")
	}
	if leaseTTL <= 0 {
		leaseTTL = time.Minute
	}
	if maxBatch <= 0 {
		maxBatch = 10
	}
	return &Dispatcher{store: store, pool: pool, leaseTTL: leaseTTL, maxBatch: maxBatch, executeTimeout: 30 * time.Minute, now: func() time.Time { return time.Now().UTC() }}, nil
}

func (d *Dispatcher) SetExecuteTimeout(timeout time.Duration) {
	if timeout > 0 {
		d.executeTimeout = timeout
	}
}

func (d *Dispatcher) DispatchOnce(ctx context.Context) (Report, error) {
	runs, err := d.store.ListQueuedRuns(ctx, d.maxBatch)
	if err != nil {
		return Report{}, err
	}
	report := Report{}
	for _, run := range runs {
		reservation, err := d.pool.Reserve("")
		if errors.Is(err, agentpool.ErrPoolSaturated) {
			report.Saturated++
			break
		}
		if err != nil {
			return report, err
		}
		result, err := d.dispatchReserved(ctx, reservation, run)
		reservation.Release()
		report.Processed++
		switch result {
		case runcontrol.DispatchSucceeded:
			report.Succeeded++
		case runcontrol.DispatchFailed:
			report.Failed++
		case runcontrol.DispatchUnknown:
			report.Unknown++
		}
		if err != nil && !errors.Is(err, runcontrol.ErrLeaseHeld) && !errors.Is(err, runcontrol.ErrInvalidRunStatus) {
			// A single Run failure must not prevent other admitted Runs from
			// being considered on the next maintenance cycle.
			continue
		}
	}
	return report, nil
}

func (d *Dispatcher) CancelRun(ctx context.Context, runID string) error {
	dispatches, err := d.store.ListDispatches(ctx, runcontrol.DispatchRunning, 100)
	if err != nil {
		return err
	}
	for _, dispatch := range dispatches {
		if dispatch.RunID != runID {
			continue
		}
		return d.pool.Cancel(ctx, dispatch.WorkerID, dispatch.DispatchID, dispatch.RunID)
	}
	return runcontrol.ErrNotFound
}

func (d *Dispatcher) CancelStopping(ctx context.Context, limit int) error {
	runs, err := d.store.ListStoppingRuns(ctx, limit)
	if err != nil {
		return err
	}
	dispatches, err := d.store.ListDispatches(ctx, runcontrol.DispatchRunning, limit)
	if err != nil {
		return err
	}
	stopping := make(map[string]struct{}, len(runs))
	for _, run := range runs {
		stopping[run.RunID] = struct{}{}
	}
	for _, dispatch := range dispatches {
		if _, ok := stopping[dispatch.RunID]; !ok {
			continue
		}
		if err := d.pool.Cancel(ctx, dispatch.WorkerID, dispatch.DispatchID, dispatch.RunID); err != nil && !errors.Is(err, agentpool.ErrNoWorker) {
			return err
		}
	}
	return nil
}

func (d *Dispatcher) RecoverUnknown(ctx context.Context, limit int) (Report, error) {
	dispatches, err := d.store.ListDispatches(ctx, runcontrol.DispatchUnknown, limit)
	if err != nil {
		return Report{}, err
	}
	report := Report{}
	for _, previous := range dispatches {
		reservation, err := d.pool.Reserve("")
		if errors.Is(err, agentpool.ErrPoolSaturated) {
			report.Saturated++
			break
		}
		if err != nil {
			return report, err
		}
		run, err := d.store.AcquireRun(ctx, previous.RunID, reservation.WorkerID(), d.now(), d.leaseTTL)
		if err != nil {
			reservation.Release()
			if errors.Is(err, runcontrol.ErrLeaseHeld) || errors.Is(err, runcontrol.ErrInvalidRunStatus) || errors.Is(err, runcontrol.ErrNotFound) {
				continue
			}
			return report, err
		}
		status, dispatchErr := d.dispatchLeased(ctx, reservation, run, run)
		reservation.Release()
		report.Processed++
		switch status {
		case runcontrol.DispatchSucceeded:
			report.Succeeded++
		case runcontrol.DispatchFailed:
			report.Failed++
		case runcontrol.DispatchUnknown:
			report.Unknown++
		}
		if dispatchErr != nil {
			continue
		}
	}
	return report, nil
}

func (d *Dispatcher) dispatchReserved(ctx context.Context, reservation *agentpool.Reservation, run runcontrol.AgentRun) (runcontrol.DispatchStatus, error) {
	leaseRun, err := d.store.AcquireRun(ctx, run.RunID, reservation.WorkerID(), d.now(), d.leaseTTL)
	if err != nil {
		return "", err
	}
	return d.dispatchLeased(ctx, reservation, leaseRun, leaseRun)
}

func (d *Dispatcher) dispatchLeased(ctx context.Context, reservation *agentpool.Reservation, run runcontrol.AgentRun, leaseRun runcontrol.AgentRun) (runcontrol.DispatchStatus, error) {
	lease := leaseFromRun(leaseRun)
	input, err := d.store.GetRunContext(ctx, lease)
	if err != nil {
		return d.markUnknown(ctx, run, "get run context: "+err.Error(), ""), err
	}
	dispatchID := "dispatch-" + run.RunID + "-" + strconv.Itoa(leaseRun.AttemptCount)
	dispatch, err := d.store.CreateDispatch(ctx, runcontrol.AgentDispatch{
		DispatchID:  dispatchID,
		RunID:       run.RunID,
		TenantID:    run.TenantID,
		OwnerID:     run.OwnerID,
		WorkerID:    reservation.WorkerID(),
		AttemptNo:   leaseRun.AttemptCount,
		RequestHash: requestHash(dispatchID, input),
		Status:      runcontrol.DispatchStarted,
		StartedAt:   d.now(),
	})
	if err != nil {
		return d.markUnknown(ctx, run, "create dispatch: "+err.Error(), ""), err
	}
	if err := d.store.UpdateDispatch(ctx, dispatch.DispatchID, runcontrol.DispatchRunning, "", nil); err != nil {
		return d.markUnknown(ctx, run, "start dispatch: "+err.Error(), dispatch.DispatchID), err
	}

	keeper := newLeaseKeeper(d.store, lease, d.leaseTTL, d.now)
	keeper.Start()
	executeCtx, cancel := context.WithTimeout(ctx, d.executeTimeout)
	result, executeErr := reservation.Execute(executeCtx, &agentv1.ExecuteRunRequest{
		Meta:       &agentv1.RequestMeta{ContractVersion: contractVersion, RequestId: dispatch.DispatchID, CorrelationId: run.RunID},
		DispatchId: dispatch.DispatchID,
		RunId:      run.RunID,
		Lease:      leaseToProto(lease),
		Input:      inputToProto(input),
		WorkerId:   reservation.WorkerID(),
	})
	cancel()
	keeper.Stop()
	if executeErr != nil {
		return d.markUnknown(ctx, run, "agent RPC: "+executeErr.Error(), dispatch.DispatchID), executeErr
	}
	if err := keeper.Err(); err != nil {
		return d.markUnknown(ctx, run, "lease lost: "+err.Error(), dispatch.DispatchID), err
	}
	lease, err = keeper.Current()
	if err != nil {
		return d.markUnknown(ctx, run, "lease unavailable: "+err.Error(), dispatch.DispatchID), err
	}
	status, err := d.applyEvents(ctx, dispatch.DispatchID, run, lease, result.Events)
	if err != nil {
		return d.markUnknown(ctx, run, "apply agent events: "+err.Error(), dispatch.DispatchID), err
	}
	finished := d.now()
	if err := d.store.UpdateDispatch(ctx, dispatch.DispatchID, status, "", &finished); err != nil {
		return d.markUnknown(ctx, run, "finish dispatch: "+err.Error(), dispatch.DispatchID), err
	}
	return status, nil
}

func (d *Dispatcher) applyEvents(ctx context.Context, dispatchID string, run runcontrol.AgentRun, lease runcontrol.LeaseContext, events []*agentv1.ExecuteRunResponse) (runcontrol.DispatchStatus, error) {
	if len(events) == 0 {
		return runcontrol.DispatchUnknown, fmt.Errorf("agent returned no events")
	}
	currentLease := lease
	expectedSequence := int64(1)
	terminal := false
	terminalStatus := runcontrol.DispatchUnknown
	for _, event := range events {
		if event == nil || event.DispatchId != dispatchID || event.RunId != run.RunID || event.EventId == "" || event.EventSequence != expectedSequence {
			return runcontrol.DispatchUnknown, fmt.Errorf("invalid agent event sequence")
		}
		payload, _ := json.Marshal(map[string]any{"event_id": event.EventId, "run_id": event.RunId, "event_type": event.EventType, "event_sequence": event.EventSequence})
		if _, err := d.store.AppendTaskEvent(ctx, run.TenantID, run.OwnerID, run.TaskID, "agent."+event.EventType, payload); err != nil {
			return runcontrol.DispatchUnknown, err
		}
		switch event.EventType {
		case "RUN_STARTED":
		case "MODEL_ATTEMPT":
			if event.ModelAttempt == nil {
				return runcontrol.DispatchUnknown, fmt.Errorf("model attempt payload is required")
			}
			_, err := d.store.RecordModelAttempt(ctx, currentLease, runcontrol.ModelAttempt{AttemptKey: event.ModelAttempt.AttemptKey, Operation: event.ModelAttempt.Operation, PromptVersion: event.ModelAttempt.PromptVersion, Provider: event.ModelAttempt.Provider, RequestHash: event.ModelAttempt.RequestHash, Status: event.ModelAttempt.Status, ResponseMetadataJSON: event.ModelAttempt.ResponseMetadataJson, TokenUsageJSON: event.ModelAttempt.TokenUsageJson, ErrorCategory: event.ModelAttempt.ErrorCategory})
			if err != nil {
				return runcontrol.DispatchUnknown, err
			}
		case "EVIDENCE_APPENDED":
			items := make([]runcontrol.EvidenceItem, 0, len(event.EvidenceItems))
			for _, item := range event.EvidenceItems {
				items = append(items, runcontrol.EvidenceItem{SourceType: item.SourceType, SourceID: item.SourceId, Locator: item.Locator, ExcerptHash: item.ExcerptHash, Excerpt: item.Excerpt})
			}
			if _, err := d.store.AppendEvidence(ctx, currentLease, items); err != nil {
				return runcontrol.DispatchUnknown, err
			}
		case "CHECKPOINT_SAVED":
			if _, err := d.store.SaveCheckpoint(ctx, currentLease, event.CheckpointSequence, event.Checkpoint); err != nil {
				return runcontrol.DispatchUnknown, err
			}
		case "DRAFT_SUBMITTED":
			if _, err := d.store.SubmitDraft(ctx, currentLease, event.DraftKey, int(event.ExpectedTaskVersion), event.DraftPatch); err != nil {
				return runcontrol.DispatchUnknown, err
			}
		case "RUN_COMPLETED":
			if terminal {
				return runcontrol.DispatchUnknown, fmt.Errorf("duplicate terminal event")
			}
			if _, err := d.store.CompleteRun(ctx, currentLease, runcontrol.RunSucceeded, d.now()); err != nil {
				return runcontrol.DispatchUnknown, err
			}
			terminal = true
			terminalStatus = runcontrol.DispatchSucceeded
		case "RUN_FAILED":
			if terminal {
				return runcontrol.DispatchUnknown, fmt.Errorf("duplicate terminal event")
			}
			status := runcontrol.RunFailed
			if event.ErrorCategory == "CANCELLED" {
				status = runcontrol.RunStopped
			}
			if _, err := d.store.CompleteRun(ctx, currentLease, status, d.now()); err != nil {
				return runcontrol.DispatchUnknown, err
			}
			terminal = true
			terminalStatus = runcontrol.DispatchFailed
		default:
			return runcontrol.DispatchUnknown, fmt.Errorf("unsupported agent event type %q", event.EventType)
		}
		expectedSequence++
	}
	if !terminal {
		return runcontrol.DispatchUnknown, fmt.Errorf("agent stream ended without terminal event")
	}
	return terminalStatus, nil
}

func (d *Dispatcher) markUnknown(ctx context.Context, run runcontrol.AgentRun, reason, dispatchID string) runcontrol.DispatchStatus {
	if dispatchID != "" {
		_ = d.store.UpdateDispatch(ctx, dispatchID, runcontrol.DispatchUnknown, reason, timePtr(d.now()))
	}
	return runcontrol.DispatchUnknown
}

func requestHash(dispatchID string, input runcontrol.AgentRunInput) string {
	digest := sha256.Sum256([]byte(dispatchID + "\x00" + input.Run.RunID + "\x00" + input.WorkflowVersion + "\x00" + string(input.Checkpoint)))
	return hex.EncodeToString(digest[:])
}

func leaseFromRun(run runcontrol.AgentRun) runcontrol.LeaseContext {
	return runcontrol.LeaseContext{RunID: run.RunID, LeaseID: run.LeaseID, WorkerID: run.WorkerID, FencingToken: run.FencingToken, ExpiresAt: run.LeaseExpiresAt}
}

func leaseToProto(lease runcontrol.LeaseContext) *agentv1.LeaseContext {
	return &agentv1.LeaseContext{RunId: lease.RunID, LeaseId: lease.LeaseID, WorkerId: lease.WorkerID, FencingToken: lease.FencingToken, ExpiresAt: lease.ExpiresAt.UTC().Format(time.RFC3339Nano)}
}

func inputToProto(input runcontrol.AgentRunInput) *agentv1.AgentRunInput {
	return &agentv1.AgentRunInput{RunId: input.Run.RunID, TenantId: input.Run.TenantID, OwnerId: input.Run.OwnerID, TaskId: input.Run.TaskID, TaskMessage: input.TaskMessage, WorkflowVersion: input.WorkflowVersion, Checkpoint: input.Checkpoint, CheckpointSequence: input.CheckpointSequence, TaskVersion: int64(input.TaskVersion), RepositoryBindingId: input.RepositoryBindingID, RepositoryRevision: input.RepositoryRevision}
}

func timePtr(value time.Time) *time.Time { return &value }

type leaseKeeper struct {
	store runcontrol.AgentExecutionStore
	mu    sync.RWMutex
	lease runcontrol.LeaseContext
	ttl   time.Duration
	now   func() time.Time
	stop  chan struct{}
	done  chan struct{}
	lost  error
}

func newLeaseKeeper(store runcontrol.AgentExecutionStore, lease runcontrol.LeaseContext, ttl time.Duration, now func() time.Time) *leaseKeeper {
	return &leaseKeeper{store: store, lease: lease, ttl: ttl, now: now, stop: make(chan struct{}), done: make(chan struct{})}
}

func (k *leaseKeeper) Start() { go k.run() }

func (k *leaseKeeper) Stop() {
	close(k.stop)
	<-k.done
}

func (k *leaseKeeper) run() {
	defer close(k.done)
	interval := k.ttl / 3
	if interval < 10*time.Millisecond {
		interval = 10 * time.Millisecond
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-k.stop:
			return
		case now := <-ticker.C:
			k.mu.RLock()
			lease := k.lease
			k.mu.RUnlock()
			updated, err := k.store.Heartbeat(context.Background(), lease, now.UTC(), k.ttl)
			if err != nil {
				k.mu.Lock()
				k.lost = err
				k.mu.Unlock()
				return
			}
			k.mu.Lock()
			k.lease = updated
			k.mu.Unlock()
		}
	}
}

func (k *leaseKeeper) Current() (runcontrol.LeaseContext, error) {
	k.mu.RLock()
	defer k.mu.RUnlock()
	if k.lost != nil {
		return runcontrol.LeaseContext{}, k.lost
	}
	return k.lease, nil
}

func (k *leaseKeeper) Err() error {
	k.mu.RLock()
	defer k.mu.RUnlock()
	return k.lost
}

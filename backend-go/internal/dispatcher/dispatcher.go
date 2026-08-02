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

const (
	contractVersion             = "agent-execution.v2"
	maxDispatchRecoveryAttempts = 3
)

type Dispatcher struct {
	store          runcontrol.DispatchStore
	pool           *agentpool.Pool
	leaseTTL       time.Duration
	maxBatch       int
	executeTimeout time.Duration
	now            func() time.Time
}

type Report struct {
	Processed    int
	Succeeded    int
	Failed       int
	Unknown      int
	Saturated    int
	Incompatible int
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
		reservation, err := d.pool.ReserveForRun(string(run.WorkflowVersion), string(run.ExecutionLedgerVersion))
		if errors.Is(err, agentpool.ErrNoCompatibleWorker) {
			report.Incompatible++
			continue
		}
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
		current, err := d.store.GetRun(ctx, previous.RunID)
		if err != nil {
			if errors.Is(err, runcontrol.ErrNotFound) {
				finished := d.now()
				if updateErr := d.store.UpdateDispatch(ctx, previous.DispatchID, runcontrol.DispatchQuarantined, "run no longer exists", &finished); updateErr != nil {
					return report, updateErr
				}
				report.Processed++
				report.Failed++
				continue
			}
			return report, err
		}
		if current.Status.Terminal() {
			finished := d.now()
			dispatchStatus := runcontrol.DispatchFailed
			lastError := "terminal run reconciled after final event acknowledgement was lost"
			if current.Status == runcontrol.RunSucceeded {
				dispatchStatus = runcontrol.DispatchSucceeded
				lastError = ""
			}
			if err := d.store.UpdateDispatch(ctx, previous.DispatchID, dispatchStatus, lastError, &finished); err != nil {
				return report, err
			}
			report.Processed++
			if dispatchStatus == runcontrol.DispatchSucceeded {
				report.Succeeded++
			} else {
				report.Failed++
			}
			continue
		}
		reservation, err := d.pool.ReserveForRun(string(current.WorkflowVersion), string(current.ExecutionLedgerVersion))
		if errors.Is(err, agentpool.ErrNoCompatibleWorker) {
			report.Incompatible++
			continue
		}
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
		finished := d.now()
		if previous.AttemptNo >= maxDispatchRecoveryAttempts {
			if _, err := d.store.CompleteRun(ctx, leaseFromRun(run), runcontrol.RunFailed, finished); err != nil {
				reservation.Release()
				return report, err
			}
			if err := d.store.UpdateDispatch(ctx, previous.DispatchID, runcontrol.DispatchQuarantined, "automatic recovery budget exhausted", &finished); err != nil {
				reservation.Release()
				return report, err
			}
			reservation.Release()
			report.Processed++
			report.Failed++
			continue
		}
		hasModelAttempts := false
		if current.ExecutionLedgerVersion == "" {
			hasModelAttempts, err = d.store.HasModelAttempts(ctx, previous.RunID)
			if err != nil {
				reservation.Release()
				return report, err
			}
		}
		if hasModelAttempts {
			if _, err := d.store.CompleteRun(ctx, leaseFromRun(run), runcontrol.RunFailed, finished); err != nil {
				reservation.Release()
				return report, err
			}
			if err := d.store.UpdateDispatch(ctx, previous.DispatchID, runcontrol.DispatchQuarantined, "model attempt result is not safe to replay automatically; create a user retry", &finished); err != nil {
				reservation.Release()
				return report, err
			}
			reservation.Release()
			report.Processed++
			report.Failed++
			continue
		}
		status, dispatchErr := d.dispatchLeased(ctx, reservation, run, run)
		reservation.Release()
		supersededAt := d.now()
		if err := d.store.UpdateDispatch(ctx, previous.DispatchID, runcontrol.DispatchFailed, "superseded by recovery attempt", &supersededAt); err != nil {
			return report, errors.Join(dispatchErr, err)
		}
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
	applier := newEventApplier(d, dispatch.DispatchID, run, lease)
	executeErr := reservation.Stream(executeCtx, &agentv1.ExecuteRunRequest{
		Meta:       &agentv1.RequestMeta{ContractVersion: contractVersion, RequestId: dispatch.DispatchID, CorrelationId: run.RunID},
		DispatchId: dispatch.DispatchID,
		RunId:      run.RunID,
		Lease:      leaseToProto(lease),
		Input:      inputToProto(input),
		WorkerId:   reservation.WorkerID(),
	}, func(event *agentv1.ExecuteRunResponse) error {
		currentLease, err := keeper.Current()
		if err != nil {
			return err
		}
		applier.currentLease = currentLease
		return applier.Apply(executeCtx, event)
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
	applier.currentLease = lease
	status, err := applier.Finish()
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
	applier := newEventApplier(d, dispatchID, run, lease)
	for _, event := range events {
		if err := applier.Apply(ctx, event); err != nil {
			return runcontrol.DispatchUnknown, err
		}
	}
	return applier.Finish()
}

type eventApplier struct {
	dispatcher       *Dispatcher
	dispatchID       string
	run              runcontrol.AgentRun
	currentLease     runcontrol.LeaseContext
	expectedSequence int64
	received         int
	terminal         bool
	terminalStatus   runcontrol.DispatchStatus
}

func newEventApplier(dispatcher *Dispatcher, dispatchID string, run runcontrol.AgentRun, lease runcontrol.LeaseContext) *eventApplier {
	return &eventApplier{
		dispatcher: dispatcher, dispatchID: dispatchID, run: run,
		currentLease: lease, expectedSequence: 1, terminalStatus: runcontrol.DispatchUnknown,
	}
}

func (a *eventApplier) Apply(ctx context.Context, event *agentv1.ExecuteRunResponse) error {
	if event == nil || event.DispatchId != a.dispatchID || event.RunId != a.run.RunID || event.EventId == "" || event.EventSequence != a.expectedSequence {
		return fmt.Errorf("invalid agent event sequence")
	}
	if a.terminal {
		return fmt.Errorf("event received after terminal event")
	}
	switch event.EventType {
	case "RUN_STARTED":
	case "MODEL_ATTEMPT":
		if event.ModelAttempt == nil {
			return fmt.Errorf("model attempt payload is required")
		}
		_, err := a.dispatcher.store.RecordModelAttempt(ctx, a.currentLease, runcontrol.ModelAttempt{AttemptKey: event.ModelAttempt.AttemptKey, Operation: event.ModelAttempt.Operation, PromptVersion: event.ModelAttempt.PromptVersion, Provider: event.ModelAttempt.Provider, RequestHash: event.ModelAttempt.RequestHash, Status: event.ModelAttempt.Status, ResponseMetadataJSON: event.ModelAttempt.ResponseMetadataJson, TokenUsageJSON: event.ModelAttempt.TokenUsageJson, ErrorCategory: event.ModelAttempt.ErrorCategory})
		if err != nil {
			return err
		}
	case "EVIDENCE_APPENDED":
		items := make([]runcontrol.EvidenceItem, 0, len(event.EvidenceItems))
		for _, item := range event.EvidenceItems {
			items = append(items, runcontrol.EvidenceItem{SourceType: item.SourceType, SourceID: item.SourceId, Locator: item.Locator, ExcerptHash: item.ExcerptHash, Excerpt: item.Excerpt})
		}
		if _, err := a.dispatcher.store.AppendEvidence(ctx, a.currentLease, items); err != nil {
			return err
		}
	case "RUN_ARTIFACT_SAVED":
		if event.RunArtifact == nil {
			return fmt.Errorf("run artifact payload is required")
		}
		if _, err := a.dispatcher.store.SaveRunArtifact(ctx, a.currentLease, runcontrol.RunArtifact{
			ArtifactKey: event.RunArtifact.ArtifactKey, ArtifactType: event.RunArtifact.ArtifactType,
			Generation: event.RunArtifact.Generation, RequestHash: event.RunArtifact.RequestHash,
			ContentHash: event.RunArtifact.ContentHash, Content: event.RunArtifact.Content,
		}); err != nil {
			return err
		}
	case "LEDGER_RESERVED":
		entry, err := ledgerEntryFromProto(event.LedgerEvent)
		if err != nil {
			return err
		}
		if _, err := a.dispatcher.store.ReserveLedgerEntry(ctx, a.currentLease, entry); err != nil {
			return err
		}
	case "LEDGER_CALL_STARTED":
		entry, err := ledgerEntryFromProto(event.LedgerEvent)
		if err != nil {
			return err
		}
		if _, err := a.dispatcher.store.MarkLedgerCallStarted(ctx, a.currentLease, entry.OperationKey, entry.RequestHash); err != nil {
			return err
		}
	case "LEDGER_FINISHED":
		entry, err := ledgerEntryFromProto(event.LedgerEvent)
		if err != nil {
			return err
		}
		if entry.EntryKind == runcontrol.LedgerEntryLocalTransition {
			if _, err := a.dispatcher.store.ConsumeLocalLedgerEntry(ctx, a.currentLease, entry); err != nil {
				return err
			}
		} else if _, err := a.dispatcher.store.FinishLedgerEntry(ctx, a.currentLease, runcontrol.LedgerFinish{OperationKey: entry.OperationKey, RequestHash: entry.RequestHash, Status: entry.Status, Consumption: entry.Consumption, OutputArtifactKey: entry.OutputArtifactKey, OutputArtifactHash: entry.OutputArtifactHash, EvidenceRefs: entry.EvidenceRefs, ErrorCategory: entry.ErrorCategory, Retryable: entry.Retryable}); err != nil {
			return err
		}
	case "CHECKPOINT_SAVED":
		if _, err := a.dispatcher.store.SaveCheckpoint(ctx, a.currentLease, event.CheckpointSequence, event.Checkpoint); err != nil {
			return err
		}
	case "DRAFT_SUBMITTED":
		if _, err := a.dispatcher.store.SubmitDraft(ctx, a.currentLease, event.DraftKey, int(event.ExpectedTaskVersion), event.DraftPatch); err != nil {
			return err
		}
	case "RUN_OUTPUT_SUBMITTED":
		output, err := runOutputFromProto(event.RunOutput)
		if err != nil {
			return err
		}
		if _, err := a.dispatcher.store.SubmitRunOutput(ctx, a.currentLease, output); err != nil {
			return err
		}
	case "RUN_COMPLETED":
		if _, err := a.dispatcher.store.CompleteRun(ctx, a.currentLease, runcontrol.RunSucceeded, a.dispatcher.now()); err != nil {
			return err
		}
		a.terminal = true
		a.terminalStatus = runcontrol.DispatchSucceeded
	case "RUN_FAILED":
		status := runcontrol.RunFailed
		if event.ErrorCategory == "CANCELLED" {
			status = runcontrol.RunStopped
		}
		if _, err := a.dispatcher.store.CompleteRun(ctx, a.currentLease, status, a.dispatcher.now()); err != nil {
			return err
		}
		a.terminal = true
		a.terminalStatus = runcontrol.DispatchFailed
	default:
		return fmt.Errorf("unsupported agent event type %q", event.EventType)
	}
	payload, _ := json.Marshal(map[string]any{"event_id": event.EventId, "run_id": event.RunId, "event_type": event.EventType, "event_sequence": event.EventSequence})
	if _, err := a.dispatcher.store.AppendTaskEvent(ctx, a.run.TenantID, a.run.OwnerID, a.run.TaskID, "agent."+event.EventType, payload); err != nil {
		return err
	}
	a.expectedSequence++
	a.received++
	return nil
}

func (a *eventApplier) Finish() (runcontrol.DispatchStatus, error) {
	if a.received == 0 {
		return runcontrol.DispatchUnknown, fmt.Errorf("agent returned no events")
	}
	if !a.terminal {
		return runcontrol.DispatchUnknown, fmt.Errorf("agent stream ended without terminal event")
	}
	return a.terminalStatus, nil
}

func (d *Dispatcher) markUnknown(ctx context.Context, run runcontrol.AgentRun, reason, dispatchID string) runcontrol.DispatchStatus {
	if dispatchID != "" {
		_ = d.store.UpdateDispatch(ctx, dispatchID, runcontrol.DispatchUnknown, reason, timePtr(d.now()))
	}
	return runcontrol.DispatchUnknown
}

func requestHash(dispatchID string, input runcontrol.AgentRunInput) string {
	digest := sha256.Sum256([]byte(dispatchID + "\x00" + input.Run.RunID + "\x00" + input.WorkflowVersion + "\x00" + input.ExecutionLedgerVersion + "\x00" + string(input.RunPurpose) + "\x00" + input.UnitScope.ScopeHash + "\x00" + input.AssignmentHash + "\x00" + string(input.Checkpoint)))
	return hex.EncodeToString(digest[:])
}

func leaseFromRun(run runcontrol.AgentRun) runcontrol.LeaseContext {
	return runcontrol.LeaseContext{RunID: run.RunID, LeaseID: run.LeaseID, WorkerID: run.WorkerID, FencingToken: run.FencingToken, ExpiresAt: run.LeaseExpiresAt}
}

func leaseToProto(lease runcontrol.LeaseContext) *agentv1.LeaseContext {
	return &agentv1.LeaseContext{RunId: lease.RunID, LeaseId: lease.LeaseID, WorkerId: lease.WorkerID, FencingToken: lease.FencingToken, ExpiresAt: lease.ExpiresAt.UTC().Format(time.RFC3339Nano)}
}

func inputToProto(input runcontrol.AgentRunInput) *agentv1.AgentRunInput {
	value := &agentv1.AgentRunInput{RunId: input.Run.RunID, TenantId: input.Run.TenantID, OwnerId: input.Run.OwnerID, TaskId: input.Run.TaskID, TaskMessage: input.TaskMessage, WorkflowVersion: input.WorkflowVersion, Checkpoint: input.Checkpoint, CheckpointSequence: input.CheckpointSequence, TaskVersion: int64(input.TaskVersion), RepositoryBindingId: input.RepositoryBindingID, RepositoryRevision: input.RepositoryRevision, EvaluationMode: string(input.EvaluationMode), AuthoritativeWorkflowVersion: string(input.AuthoritativeWorkflow), ShadowWorkflowVersion: string(input.ShadowWorkflow), CandidatePolicyVersion: input.CandidatePolicyVersion, AssignmentHash: input.AssignmentHash}
	if input.RunPurpose.Valid() {
		value.RunPurpose = runPurposeToProto(input.RunPurpose)
		value.UnitScope = unitScopeToProto(input.UnitScope)
	}
	if input.RepositoryBindingID != "" && input.RepositoryRevision != "" {
		value.AllowedSourceAuthorities = append(value.AllowedSourceAuthorities, &agentv1.SourceAuthority{
			SourceKind:    "github",
			BindingId:     input.RepositoryBindingID,
			SourceId:      input.RepositoryBindingID,
			SourceVersion: input.RepositoryRevision,
		})
	}
	value.ExecutionLedgerVersion = input.ExecutionLedgerVersion
	if input.ExecutionLedgerVersion != "" {
		value.RunBudget = runBudgetToProto(input.RunBudget)
		value.ConsumedBudget = consumedBudgetToProto(input.ConsumedBudget)
		for _, entry := range input.LedgerEntries {
			value.LedgerEntries = append(value.LedgerEntries, ledgerEntryToProto(entry))
		}
	}
	value.ResumeSummary = &agentv1.ResumeStateSummary{CheckpointContentHash: input.ResumeSummary.CheckpointContentHash, TerminalModelAttemptCount: input.ResumeSummary.TerminalModelAttemptCount, EvidenceCount: input.ResumeSummary.EvidenceCount, EvidenceRefs: input.ResumeSummary.EvidenceRefs, ArtifactCount: input.ResumeSummary.ArtifactCount}
	for _, item := range input.ResumeSummary.Artifacts {
		value.ResumeSummary.Artifacts = append(value.ResumeSummary.Artifacts, &agentv1.RunArtifactIdentity{ArtifactKey: item.ArtifactKey, ArtifactType: item.ArtifactType, Generation: item.Generation, RequestHash: item.RequestHash, ContentHash: item.ContentHash})
	}
	for _, item := range input.ResumeEvidence {
		value.ResumeEvidence = append(value.ResumeEvidence, &agentv1.EvidenceItem{SourceType: item.SourceType, SourceId: item.SourceID, Locator: item.Locator, ExcerptHash: item.ExcerptHash, Excerpt: item.Excerpt})
	}
	for _, item := range input.ResumeArtifacts {
		value.ResumeArtifacts = append(value.ResumeArtifacts, &agentv1.RunArtifact{ArtifactKey: item.ArtifactKey, ArtifactType: item.ArtifactType, Generation: item.Generation, RequestHash: item.RequestHash, ContentHash: item.ContentHash, Content: item.Content})
	}
	if input.RevisionScope.BaseDraftID != "" || len(input.RevisionScope.ReopenedUnitKeys) > 0 {
		value.RevisionScope = &agentv1.RevisionScope{BaseDraftId: input.RevisionScope.BaseDraftID, BaseDraftHash: input.RevisionScope.BaseDraftHash, ReopenedUnitKeys: input.RevisionScope.ReopenedUnitKeys, ImmutableUnitKeys: input.RevisionScope.ImmutableUnitKeys, UserFeedback: input.RevisionScope.UserFeedback}
	}
	if input.ResumeDraft != nil {
		value.ResumeDraft = &agentv1.SubmittedDraftReceipt{DraftKey: input.ResumeDraft.DraftKey, ContentHash: input.ResumeDraft.ContentHash, TaskVersion: int64(input.ResumeDraft.TaskVersion), Content: input.ResumeDraft.Content}
	}
	if input.BaseDraft != nil {
		value.BaseDraft = &agentv1.SubmittedDraftReceipt{DraftKey: input.BaseDraft.DraftKey, ContentHash: input.BaseDraft.ContentHash, TaskVersion: int64(input.BaseDraft.TaskVersion), Content: input.BaseDraft.Content}
	}
	if input.SubmittedDraft != nil {
		value.SubmittedDraft = &agentv1.SubmittedDraftReceipt{DraftKey: input.SubmittedDraft.DraftKey, ContentHash: input.SubmittedDraft.ContentHash, TaskVersion: int64(input.SubmittedDraft.TaskVersion), Content: input.SubmittedDraft.Content}
	}
	return value
}

func runPurposeToProto(value runcontrol.RunPurpose) agentv1.RunPurpose {
	switch value {
	case runcontrol.RunPurposePlanOutline:
		return agentv1.RunPurpose_RUN_PURPOSE_PLAN_OUTLINE
	case runcontrol.RunPurposeGenerateUnit:
		return agentv1.RunPurpose_RUN_PURPOSE_GENERATE_UNIT
	case runcontrol.RunPurposeReviseUnit:
		return agentv1.RunPurpose_RUN_PURPOSE_REVISE_UNIT
	case runcontrol.RunPurposeFullReview:
		return agentv1.RunPurpose_RUN_PURPOSE_FULL_REVIEW
	default:
		return agentv1.RunPurpose_RUN_PURPOSE_UNSPECIFIED
	}
}

func runPurposeFromProto(value agentv1.RunPurpose) runcontrol.RunPurpose {
	switch value {
	case agentv1.RunPurpose_RUN_PURPOSE_PLAN_OUTLINE:
		return runcontrol.RunPurposePlanOutline
	case agentv1.RunPurpose_RUN_PURPOSE_GENERATE_UNIT:
		return runcontrol.RunPurposeGenerateUnit
	case agentv1.RunPurpose_RUN_PURPOSE_REVISE_UNIT:
		return runcontrol.RunPurposeReviseUnit
	case agentv1.RunPurpose_RUN_PURPOSE_FULL_REVIEW:
		return runcontrol.RunPurposeFullReview
	default:
		return ""
	}
}

func runOutputKindFromProto(value agentv1.RunOutputKind) runcontrol.RunOutputKind {
	switch value {
	case agentv1.RunOutputKind_RUN_OUTPUT_KIND_OUTLINE_CANDIDATE:
		return runcontrol.RunOutputOutlineCandidate
	case agentv1.RunOutputKind_RUN_OUTPUT_KIND_UNIT_CANDIDATE:
		return runcontrol.RunOutputUnitCandidate
	case agentv1.RunOutputKind_RUN_OUTPUT_KIND_UNIT_PATCH:
		return runcontrol.RunOutputUnitPatch
	case agentv1.RunOutputKind_RUN_OUTPUT_KIND_FULL_REVIEW_REPORT:
		return runcontrol.RunOutputFullReviewReport
	default:
		return ""
	}
}

func unitScopeToProto(value runcontrol.UnitScope) *agentv1.UnitScope {
	result := &agentv1.UnitScope{
		SchemaVersion: value.SchemaVersion, OutlineId: value.OutlineID,
		OutlineVersion: value.OutlineVersion, OutlineHash: value.OutlineHash,
		CurrentUnitKey: value.CurrentUnitKey, CurrentUnitTitle: value.CurrentUnitTitle,
		CurrentUnitOrdinal: int32(value.CurrentUnitOrdinal), SectionNodeKeys: value.SectionNodeKeys,
		DependencyUnitKeys: value.DependencyUnitKeys, ReopenedUnitKeys: value.ReopenedUnitKeys,
		ImmutableUnitKeys: value.ImmutableUnitKeys, RequirementBriefRef: value.RequirementRef,
		RequirementBriefHash: value.RequirementHash, BaseUnitHash: value.BaseUnitHash,
		UserFeedback: value.UserFeedback, ScopeHash: value.ScopeHash,
	}
	for _, item := range value.ConfirmedContext {
		result.ConfirmedContext = append(result.ConfirmedContext, &agentv1.ConfirmedUnitContext{
			UnitKey: item.UnitKey, UnitVersion: item.UnitVersion, ContentHash: item.ContentHash,
			Summary: item.Summary, WorkingDraftRef: item.WorkingDraftRef, Markdown: item.Markdown,
		})
	}
	return result
}

func runOutputFromProto(value *agentv1.RunOutput) (runcontrol.RunOutput, error) {
	if value == nil {
		return runcontrol.RunOutput{}, fmt.Errorf("run output payload is required")
	}
	purpose := runPurposeFromProto(value.RunPurpose)
	kind := runOutputKindFromProto(value.OutputKind)
	if !purpose.Valid() || kind == "" {
		return runcontrol.RunOutput{}, fmt.Errorf("run output purpose or kind is invalid")
	}
	return runcontrol.RunOutput{
		SchemaVersion: value.SchemaVersion, OutputKey: value.OutputKey,
		OutputKind: kind, RunPurpose: purpose, ScopeHash: value.ScopeHash,
		ExpectedTaskVersion: int(value.ExpectedTaskVersion), ContentHash: value.ContentHash,
		Payload: append([]byte(nil), value.Payload...),
	}, nil
}

func budgetDeltaFromProto(value *agentv1.BudgetDelta) runcontrol.BudgetDelta {
	if value == nil {
		return runcontrol.BudgetDelta{}
	}
	return runcontrol.BudgetDelta{ModelAttempts: value.ModelAttempts, ToolCalls: value.ToolCalls, Iterations: value.Iterations, Replans: value.Replans, Supplements: value.Supplements, QualityRepairs: value.QualityRepairs, InputTokens: value.InputTokens, OutputTokens: value.OutputTokens, ElapsedMS: value.ElapsedMs}
}

func budgetDeltaToProto(value runcontrol.BudgetDelta) *agentv1.BudgetDelta {
	return &agentv1.BudgetDelta{ModelAttempts: value.ModelAttempts, ToolCalls: value.ToolCalls, Iterations: value.Iterations, Replans: value.Replans, Supplements: value.Supplements, QualityRepairs: value.QualityRepairs, InputTokens: value.InputTokens, OutputTokens: value.OutputTokens, ElapsedMs: value.ElapsedMS}
}

func runBudgetToProto(value runcontrol.RunBudget) *agentv1.RunBudget {
	if value == (runcontrol.RunBudget{}) {
		return nil
	}
	return &agentv1.RunBudget{MaxModelAttempts: value.MaxModelAttempts, MaxToolCalls: value.MaxToolCalls, MaxIterations: value.MaxIterations, MaxReplans: value.MaxReplans, MaxSupplements: value.MaxSupplements, MaxQualityRepairs: value.MaxQualityRepairs, MaxInputTokens: value.MaxInputTokens, MaxOutputTokens: value.MaxOutputTokens, MaxElapsedMs: value.MaxElapsedMS}
}

func consumedBudgetToProto(value runcontrol.BudgetDelta) *agentv1.ConsumedBudget {
	return &agentv1.ConsumedBudget{ModelAttempts: value.ModelAttempts, ToolCalls: value.ToolCalls, Iterations: value.Iterations, Replans: value.Replans, Supplements: value.Supplements, QualityRepairs: value.QualityRepairs, InputTokens: value.InputTokens, OutputTokens: value.OutputTokens, ElapsedMs: value.ElapsedMS}
}

func ledgerEntryToProto(entry runcontrol.LedgerEntry) *agentv1.RunLedgerEntry {
	return &agentv1.RunLedgerEntry{EntryId: entry.EntryID, OperationKey: entry.OperationKey, EntryKind: string(entry.EntryKind), Operation: entry.Operation, RequestHash: entry.RequestHash, Status: string(entry.Status), Reservation: budgetDeltaToProto(entry.Reservation), Consumption: budgetDeltaToProto(entry.Consumption), OutputArtifactKey: entry.OutputArtifactKey, OutputArtifactHash: entry.OutputArtifactHash, EvidenceRefs: entry.EvidenceRefs, ErrorCategory: entry.ErrorCategory, Retryable: entry.Retryable}
}

func ledgerEntryFromProto(event *agentv1.RunLedgerEvent) (runcontrol.LedgerEntry, error) {
	if event == nil || event.Entry == nil {
		return runcontrol.LedgerEntry{}, fmt.Errorf("ledger event payload is required")
	}
	entry := event.Entry
	if entry.OperationKey == "" || entry.Operation == "" || entry.RequestHash == "" {
		return runcontrol.LedgerEntry{}, fmt.Errorf("ledger event identity is incomplete")
	}
	return runcontrol.LedgerEntry{EntryID: entry.EntryId, OperationKey: entry.OperationKey, EntryKind: runcontrol.LedgerEntryKind(entry.EntryKind), Operation: entry.Operation, RequestHash: entry.RequestHash, Status: runcontrol.LedgerStatus(entry.Status), Reservation: budgetDeltaFromProto(entry.Reservation), Consumption: budgetDeltaFromProto(entry.Consumption), OutputArtifactKey: entry.OutputArtifactKey, OutputArtifactHash: entry.OutputArtifactHash, EvidenceRefs: append([]string(nil), entry.EvidenceRefs...), ErrorCategory: entry.ErrorCategory, Retryable: entry.Retryable}, nil
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

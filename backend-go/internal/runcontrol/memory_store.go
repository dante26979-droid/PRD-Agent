package runcontrol

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/id"
)

var (
	ErrNotFound            = errors.New("resource not found")
	ErrInvalidIdempotency  = errors.New("idempotency key was reused with different input")
	ErrLeaseHeld           = errors.New("run is leased by another worker")
	ErrLeaseLost           = errors.New("run lease is no longer valid")
	ErrInvalidRunStatus    = errors.New("run cannot be acquired in its current status")
	ErrInvalidPayload      = errors.New("invalid agent payload")
	ErrPayloadTooLarge     = errors.New("agent payload is too large")
	ErrCheckpointConflict  = errors.New("checkpoint sequence conflict")
	ErrTaskVersionConflict = errors.New("task version conflict")
	ErrSensitivePayload    = errors.New("sensitive value is not allowed in agent payload")
	ErrCapacityExhausted   = errors.New("agent waiting queue is full")
	ErrBudgetExhausted     = errors.New("run budget is exhausted")
	ErrLedgerConflict      = errors.New("run execution ledger transition conflict")
	ErrOutcomeUnknown      = errors.New("remote call outcome is unknown")
)

const (
	MaxCheckpointBytes   = 1 << 20
	MaxRunArtifactBytes  = 1 << 20
	MaxDraftPatchBytes   = 2 << 20
	MaxEventPayloadBytes = 256 << 10
	DefaultEventPageSize = 100
	MaxEventPageSize     = 1000
)

type memoryIdempotency struct {
	requestHash string
	value       TaskWithRun
}

type memoryOutbox struct {
	message   OutboxMessage
	claimed   bool
	published bool
}

type MemoryStore struct {
	mu                            sync.RWMutex
	policy                        QueuePolicy
	tasks                         map[string]Task
	runs                          map[string]AgentRun
	idempotency                   map[string]memoryIdempotency
	outbox                        map[string]*memoryOutbox
	checkpoints                   map[string]memoryCheckpoint
	attempts                      map[string]memoryAttempt
	evidence                      map[string]EvidenceRecord
	artifacts                     map[string]RunArtifact
	drafts                        map[string]memoryDraft
	events                        map[string][]TaskEvent
	dispatches                    map[string]AgentDispatch
	lastScheduled                 map[string]time.Time
	retryIdempotency              map[string]memoryRetryIdempotency
	publishes                     map[string]memoryPublish
	publishPreviewIdempotency     map[string]string
	publishConfirmIdempotency     map[string]string
	confirmationVersions          map[string]memoryConfirmationVersion
	confirmationDecisions         map[string]memoryConfirmationDecision
	revisionScopes                map[string]RevisionScope
	budgets                       map[string]memoryBudgetState
	ledgerEntries                 map[string]LedgerEntry
	unitScopes                    map[string]UnitScope
	runOutputs                    map[string]RunOutput
	runOutputReceipts             map[string]RunOutputReceipt
	reviewOutlines                map[string]ReviewOutlineVersion
	reviewUnits                   map[string]map[string]ReviewUnit
	reviewTransitions             map[string]memoryReviewTransition
	fullReviewReports             map[string]FullReviewReport
	gateDecisionRecords           map[string]GateDecisionRecord
	rolloutStageState             RolloutStageState
	drainRecords                  map[string]DrainRecord
	rolloutAssignments            map[string]RolloutAssignment
	rolloutOperatorResults        map[string]RolloutOperatorResult
	rolloutCommandHashes          map[string]string
	rolloutGateResults            map[string]RolloutGateOperatorResult
	rolloutStageResults           map[string]RolloutStageOperatorResult
	readinessRecords              map[string]ReadinessRecord
	readinessResults              map[string]ReadinessOperatorResult
	memorySpaces                  map[string]MemorySpace
	taskMemorySpaces              map[string]string
	runMemoryAssignments          map[string]RunMemoryAssignment
	projectMemoryRecords          map[string]ProjectMemoryRecord
	projectMemoryVersions         map[string][]ProjectMemoryVersion
	projectMemoryCandidates       map[string]ProjectMemoryCandidate
	projectMemoryConflicts        map[string]ProjectMemoryConflict
	projectMemoryCommandHashes    map[string]string
	projectMemoryCommandResources map[string]string
}

type memoryCheckpoint struct {
	sequence    int64
	payload     []byte
	contentHash string
}

type memoryDraft struct {
	draftID     string
	runID       string
	draftKey    string
	patchHash   string
	patch       []byte
	taskVersion int
	createdAt   time.Time
}

type memoryAttempt struct {
	attemptID string
	runID     string
	value     ModelAttempt
	createdAt time.Time
}

type memoryBudgetState struct {
	policy    RunBudget
	reserved  BudgetDelta
	consumed  BudgetDelta
	overage   BudgetDelta
	startedAt time.Time
}

type memoryRetryIdempotency struct {
	requestHash string
	run         AgentRun
}

type memoryPublish struct {
	record           PublishRecord
	tenantID         string
	ownerID          string
	draftKey         string
	content          []byte
	expiresAt        time.Time
	availableAt      time.Time
	claimWorker      string
	claimExpires     time.Time
	retentionWorker  string
	retentionExpires time.Time
	payloadPurgedAt  time.Time
}

type memoryConfirmationVersion struct {
	taskID      string
	taskVersion int
	value       ConfirmationUnitVersion
}

type memoryConfirmationDecision struct {
	requestHash string
	result      ConfirmationMutationResult
	decision    string
}

func NewMemoryStore(policy QueuePolicy) *MemoryStore {
	if policy.MaxGlobalRunnable < 1 {
		policy.MaxGlobalRunnable = 30
	}
	if policy.MaxRunnablePerOwner < 1 {
		policy.MaxRunnablePerOwner = 1
	}
	if policy.MaxWaitingRuns < 1 {
		policy.MaxWaitingRuns = 100
	}
	if policy.ConfirmationSecret == "" {
		policy.ConfirmationSecret = "local-publish-confirmation-secret"
	}
	policy.DefaultWorkflowVersion = DefaultWorkflowVersion(policy.DefaultWorkflowVersion)
	if policy.DefaultWorkflowVersion == WorkflowVersionV4 && policy.DefaultExecutionLedgerVersion == "" {
		policy.DefaultExecutionLedgerVersion = ExecutionLedgerVersionV1
	}
	if policy.DefaultExecutionLedgerVersion != "" && policy.DefaultExecutionLedgerVersion != ExecutionLedgerVersionV1 {
		policy.DefaultExecutionLedgerVersion = ""
	}
	if policy.DefaultExecutionLedgerVersion == ExecutionLedgerVersionV1 && policy.DefaultRunBudget == (RunBudget{}) {
		policy.DefaultRunBudget = defaultV4RunBudget()
	}
	return &MemoryStore{
		policy:                        policy,
		tasks:                         make(map[string]Task),
		runs:                          make(map[string]AgentRun),
		idempotency:                   make(map[string]memoryIdempotency),
		outbox:                        make(map[string]*memoryOutbox),
		checkpoints:                   make(map[string]memoryCheckpoint),
		attempts:                      make(map[string]memoryAttempt),
		evidence:                      make(map[string]EvidenceRecord),
		artifacts:                     make(map[string]RunArtifact),
		drafts:                        make(map[string]memoryDraft),
		events:                        make(map[string][]TaskEvent),
		dispatches:                    make(map[string]AgentDispatch),
		lastScheduled:                 make(map[string]time.Time),
		retryIdempotency:              make(map[string]memoryRetryIdempotency),
		publishes:                     make(map[string]memoryPublish),
		publishPreviewIdempotency:     make(map[string]string),
		publishConfirmIdempotency:     make(map[string]string),
		confirmationVersions:          make(map[string]memoryConfirmationVersion),
		confirmationDecisions:         make(map[string]memoryConfirmationDecision),
		revisionScopes:                make(map[string]RevisionScope),
		budgets:                       make(map[string]memoryBudgetState),
		ledgerEntries:                 make(map[string]LedgerEntry),
		unitScopes:                    make(map[string]UnitScope),
		runOutputs:                    make(map[string]RunOutput),
		runOutputReceipts:             make(map[string]RunOutputReceipt),
		reviewOutlines:                make(map[string]ReviewOutlineVersion),
		reviewUnits:                   make(map[string]map[string]ReviewUnit),
		reviewTransitions:             make(map[string]memoryReviewTransition),
		fullReviewReports:             make(map[string]FullReviewReport),
		gateDecisionRecords:           make(map[string]GateDecisionRecord),
		rolloutStageState:             RolloutStageState{Stage: RolloutLocalOnly, Version: 1},
		drainRecords:                  make(map[string]DrainRecord),
		rolloutAssignments:            make(map[string]RolloutAssignment),
		rolloutOperatorResults:        make(map[string]RolloutOperatorResult),
		rolloutCommandHashes:          make(map[string]string),
		rolloutGateResults:            make(map[string]RolloutGateOperatorResult),
		rolloutStageResults:           make(map[string]RolloutStageOperatorResult),
		readinessRecords:              make(map[string]ReadinessRecord),
		readinessResults:              make(map[string]ReadinessOperatorResult),
		memorySpaces:                  make(map[string]MemorySpace),
		taskMemorySpaces:              make(map[string]string),
		runMemoryAssignments:          make(map[string]RunMemoryAssignment),
		projectMemoryRecords:          make(map[string]ProjectMemoryRecord),
		projectMemoryVersions:         make(map[string][]ProjectMemoryVersion),
		projectMemoryCandidates:       make(map[string]ProjectMemoryCandidate),
		projectMemoryConflicts:        make(map[string]ProjectMemoryConflict),
		projectMemoryCommandHashes:    make(map[string]string),
		projectMemoryCommandResources: make(map[string]string),
	}
}

func (s *MemoryStore) Health(context.Context) error { return nil }

func (s *MemoryStore) CreateTaskWithRun(_ context.Context, tenantID, ownerID, message, idempotencyKey string) (TaskWithRun, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	key := tenantID + "\x00" + ownerID + "\x00" + idempotencyKey
	requestHash := StartTaskRequestHash(tenantID, ownerID, message)
	if replay, ok := s.idempotency[key]; ok {
		if replay.requestHash != requestHash {
			return TaskWithRun{}, ErrInvalidIdempotency
		}
		return replay.value, nil
	}

	taskID, err := id.New("task")
	if err != nil {
		return TaskWithRun{}, err
	}
	runID, err := id.New("run")
	if err != nil {
		return TaskWithRun{}, err
	}
	now := time.Now().UTC()
	task := Task{
		TaskID: taskID, TenantID: tenantID, OwnerID: ownerID, Message: message,
		Status: "DRAFT", Version: 1, CreatedAt: now, UpdatedAt: now,
	}
	status := RunQueued
	if s.runnableCountLocked() >= s.policy.MaxGlobalRunnable || s.ownerRunnableCountLocked(tenantID, ownerID) >= s.policy.MaxRunnablePerOwner {
		status = RunWaitingCapacity
		if s.waitingCountLocked() >= s.policy.MaxWaitingRuns {
			return TaskWithRun{}, ErrCapacityExhausted
		}
	}
	workflowVersion := s.policy.DefaultWorkflowVersion
	var rolloutAssignment *RolloutAssignment
	if s.policy.RolloutPolicy != nil {
		assignment, assignmentErr := s.policy.RolloutPolicy.Assign(AssignmentRequest{TenantID: tenantID, OwnerID: ownerID, TaskID: taskID})
		if assignmentErr != nil {
			return TaskWithRun{}, assignmentErr
		}
		workflowVersion = assignment.AuthoritativeWorkflowVersion
		rolloutAssignment = &assignment
	}
	ledgerVersion := ledgerVersionForPolicy(s.policy)
	if workflowVersion == WorkflowVersionV4 && ledgerVersion == "" {
		ledgerVersion = ExecutionLedgerVersionV1
	}
	if workflowVersion == WorkflowVersionV4 && s.policy.DefaultRunBudget == (RunBudget{}) {
		s.policy.DefaultRunBudget = defaultV4RunBudget()
	}
	run := AgentRun{
		RunID: runID, TaskID: taskID, TenantID: tenantID, OwnerID: ownerID,
		WorkflowVersion:        workflowVersion,
		ExecutionLedgerVersion: ledgerVersion,
		Status:                 status, QueueSlotAcquired: status == RunQueued,
		CreatedAt: now, UpdatedAt: now,
	}
	s.tasks[taskID] = task
	s.runs[runID] = run
	space := s.ensureDefaultMemorySpaceLocked(tenantID, ownerID, now)
	s.taskMemorySpaces[taskID] = space.SpaceID
	s.runMemoryAssignments[runID] = buildRunMemoryAssignment(runID, space, now)
	if workflowVersion == WorkflowVersionV4 {
		outlineScope, scopeErr := BuildUnitScope(UnitScope{Purpose: RunPurposePlanOutline})
		if scopeErr != nil {
			return TaskWithRun{}, scopeErr
		}
		s.unitScopes[runID] = outlineScope
	}
	if rolloutAssignment != nil {
		s.rolloutAssignments[runID] = *rolloutAssignment
	}
	s.initializeBudgetLocked(run, now)
	s.events[taskID] = []TaskEvent{{
		EventID: "evt-" + taskID + "-1", TaskID: taskID, TenantID: tenantID, OwnerID: ownerID,
		Sequence: 1, EventType: "task.created", Payload: []byte(`{"task_id":"` + taskID + `","run_id":"` + runID + `","status":"DRAFT"}`), OccurredAt: now,
	}}
	if status == RunQueued {
		s.lastScheduled[scheduleOwnerKey(tenantID, ownerID)] = now
		s.addRunRequestOutboxLocked(run, "START_OR_RESUME", now)
	}
	value := TaskWithRun{Task: task, Run: run}
	s.idempotency[key] = memoryIdempotency{requestHash: requestHash, value: value}
	return value, nil
}

func defaultV4RunBudget() RunBudget {
	return RunBudget{MaxModelAttempts: 8, MaxToolCalls: 8, MaxIterations: 12, MaxReplans: 2, MaxSupplements: 2, MaxQualityRepairs: 2, MaxInputTokens: 120000, MaxOutputTokens: 32000, MaxElapsedMS: 1800000}
}

func (s *MemoryStore) RetryTask(_ context.Context, tenantID, ownerID, taskID, idempotencyKey string, expectedTaskVersion int) (AgentRun, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	task, ok := s.tasks[taskID]
	if !ok || task.TenantID != tenantID || task.OwnerID != ownerID {
		return AgentRun{}, ErrNotFound
	}
	if expectedTaskVersion < 1 || task.Version != expectedTaskVersion {
		return AgentRun{}, ErrTaskVersionConflict
	}
	requestHash := RetryTaskRequestHash(tenantID, ownerID, taskID, expectedTaskVersion)
	idempotencyIdentity := tenantID + "\x00" + ownerID + "\x00" + idempotencyKey
	if existing, ok := s.retryIdempotency[idempotencyIdentity]; ok {
		if existing.requestHash != requestHash {
			return AgentRun{}, ErrInvalidIdempotency
		}
		return existing.run, nil
	}
	for _, run := range s.runs {
		if run.TaskID == taskID && !run.Status.Terminal() {
			return AgentRun{}, ErrInvalidRunStatus
		}
	}
	status := RunQueued
	if s.runnableCountLocked() >= s.policy.MaxGlobalRunnable || s.ownerRunnableCountLocked(tenantID, ownerID) >= s.policy.MaxRunnablePerOwner {
		status = RunWaitingCapacity
		if s.waitingCountLocked() >= s.policy.MaxWaitingRuns {
			return AgentRun{}, ErrCapacityExhausted
		}
	}
	runID, err := id.New("run")
	if err != nil {
		return AgentRun{}, err
	}
	now := time.Now().UTC()
	workflowVersion := s.policy.DefaultWorkflowVersion
	inheritedAssignment, hasInheritedAssignment := s.inheritedRolloutAssignmentLocked(taskID)
	if hasInheritedAssignment {
		workflowVersion = inheritedAssignment.AuthoritativeWorkflowVersion
	}
	run := AgentRun{
		RunID: runID, TaskID: taskID, TenantID: tenantID, OwnerID: ownerID,
		WorkflowVersion:        workflowVersion,
		ExecutionLedgerVersion: ledgerVersionForPolicy(s.policy),
		Status:                 status, QueueSlotAcquired: status == RunQueued, CreatedAt: now, UpdatedAt: now,
	}
	s.runs[runID] = run
	if spaceID := s.taskMemorySpaces[taskID]; spaceID != "" {
		if space, ok := s.memorySpaces[spaceID]; ok {
			s.runMemoryAssignments[runID] = buildRunMemoryAssignment(runID, space, now)
		}
	}
	if inheritedScope, ok := s.inheritedUnitScopeLocked(taskID); ok {
		s.unitScopes[runID] = inheritedScope
	}
	if hasInheritedAssignment {
		s.rolloutAssignments[runID] = inheritedAssignment
	}
	s.initializeBudgetLocked(run, now)
	if status == RunQueued {
		s.lastScheduled[scheduleOwnerKey(tenantID, ownerID)] = now
		s.addRunRequestOutboxLocked(run, "USER_RETRY", now)
	}
	s.retryIdempotency[idempotencyIdentity] = memoryRetryIdempotency{requestHash: requestHash, run: run}
	return run, nil
}

func (s *MemoryStore) inheritedUnitScopeLocked(taskID string) (UnitScope, bool) {
	var selected UnitScope
	var selectedAt time.Time
	found := false
	for runID, scope := range s.unitScopes {
		run, ok := s.runs[runID]
		if !ok || run.TaskID != taskID || (found && !run.CreatedAt.After(selectedAt)) {
			continue
		}
		selected, selectedAt, found = scope, run.CreatedAt, true
	}
	return selected, found
}

func (s *MemoryStore) ListTasks(_ context.Context, tenantID, ownerID string, limit int) ([]Task, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	items := make([]Task, 0, len(s.tasks))
	for _, task := range s.tasks {
		if task.TenantID == tenantID && task.OwnerID == ownerID {
			items = append(items, task)
		}
	}
	sort.Slice(items, func(i, j int) bool { return items[i].UpdatedAt.After(items[j].UpdatedAt) })
	if limit > 0 && len(items) > limit {
		items = items[:limit]
	}
	return items, nil
}

func (s *MemoryStore) GetTask(_ context.Context, tenantID, ownerID, taskID string) (Task, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	task, ok := s.tasks[taskID]
	if !ok || task.TenantID != tenantID || task.OwnerID != ownerID {
		return Task{}, ErrNotFound
	}
	return task, nil
}

func (s *MemoryStore) AppendTaskEvent(_ context.Context, tenantID, ownerID, taskID, eventType string, payload []byte) (TaskEvent, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if task, ok := s.tasks[taskID]; !ok || task.TenantID != tenantID || task.OwnerID != ownerID {
		return TaskEvent{}, ErrNotFound
	}
	if eventType == "" || len(payload) == 0 || len(payload) > MaxEventPayloadBytes || !json.Valid(payload) {
		return TaskEvent{}, ErrInvalidPayload
	}
	return s.appendTaskEventLocked(tenantID, ownerID, taskID, eventType, payload), nil
}

func (s *MemoryStore) ListTaskEvents(_ context.Context, tenantID, ownerID, taskID string, afterSequence int64, limit int) ([]TaskEvent, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if task, ok := s.tasks[taskID]; !ok || task.TenantID != tenantID || task.OwnerID != ownerID {
		return nil, ErrNotFound
	}
	if afterSequence < 0 {
		return nil, ErrInvalidPayload
	}
	if limit <= 0 {
		limit = DefaultEventPageSize
	}
	if limit > MaxEventPageSize {
		limit = MaxEventPageSize
	}
	items := make([]TaskEvent, 0, limit)
	for _, event := range s.events[taskID] {
		if event.Sequence <= afterSequence {
			continue
		}
		item := event
		item.Payload = append([]byte(nil), event.Payload...)
		items = append(items, item)
		if len(items) == limit {
			break
		}
	}
	return items, nil
}

func (s *MemoryStore) ListQueuedRuns(_ context.Context, limit int) ([]AgentRun, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if limit <= 0 {
		limit = 100
	}
	items := make([]AgentRun, 0, limit)
	for _, run := range s.runs {
		if run.Status != RunQueued {
			continue
		}
		items = append(items, run)
	}
	sort.Slice(items, func(i, j int) bool { return items[i].CreatedAt.Before(items[j].CreatedAt) })
	if len(items) > limit {
		items = items[:limit]
	}
	return items, nil
}

func (s *MemoryStore) ListStoppingRuns(_ context.Context, limit int) ([]AgentRun, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if limit <= 0 {
		limit = 100
	}
	items := make([]AgentRun, 0, limit)
	for _, run := range s.runs {
		if run.Status == RunStopping {
			items = append(items, run)
		}
	}
	sort.Slice(items, func(i, j int) bool { return items[i].UpdatedAt.Before(items[j].UpdatedAt) })
	if len(items) > limit {
		items = items[:limit]
	}
	return items, nil
}

func (s *MemoryStore) CreateDispatch(_ context.Context, dispatch AgentDispatch) (AgentDispatch, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if dispatch.DispatchID == "" || dispatch.RunID == "" || dispatch.WorkerID == "" || dispatch.AttemptNo < 1 || dispatch.RequestHash == "" {
		return AgentDispatch{}, ErrInvalidPayload
	}
	run, ok := s.runs[dispatch.RunID]
	if !ok || run.TenantID != dispatch.TenantID || run.OwnerID != dispatch.OwnerID {
		return AgentDispatch{}, ErrNotFound
	}
	for _, existing := range s.dispatches {
		if existing.RunID == dispatch.RunID && existing.AttemptNo == dispatch.AttemptNo {
			if existing.RequestHash != dispatch.RequestHash {
				return AgentDispatch{}, ErrInvalidIdempotency
			}
			return existing, nil
		}
	}
	if dispatch.Status == "" {
		dispatch.Status = DispatchStarted
	}
	if dispatch.StartedAt.IsZero() {
		dispatch.StartedAt = time.Now().UTC()
	}
	s.dispatches[dispatch.DispatchID] = dispatch
	return dispatch, nil
}

func (s *MemoryStore) UpdateDispatch(_ context.Context, dispatchID string, status DispatchStatus, lastError string, finishedAt *time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	dispatch, ok := s.dispatches[dispatchID]
	if !ok {
		return ErrNotFound
	}
	dispatch.Status = status
	dispatch.LastError = lastError
	if finishedAt != nil {
		value := *finishedAt
		dispatch.FinishedAt = &value
	}
	s.dispatches[dispatchID] = dispatch
	return nil
}

func (s *MemoryStore) ListDispatches(_ context.Context, status DispatchStatus, limit int) ([]AgentDispatch, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if limit <= 0 {
		limit = 100
	}
	items := make([]AgentDispatch, 0, limit)
	for _, dispatch := range s.dispatches {
		if status != "" && dispatch.Status != status {
			continue
		}
		items = append(items, dispatch)
	}
	sort.Slice(items, func(i, j int) bool { return items[i].StartedAt.Before(items[j].StartedAt) })
	if len(items) > limit {
		items = items[:limit]
	}
	return items, nil
}

func (s *MemoryStore) appendTaskEventLocked(tenantID, ownerID, taskID, eventType string, payload []byte) TaskEvent {
	sequence := int64(len(s.events[taskID]) + 1)
	event := TaskEvent{
		EventID: "evt-" + taskID + "-" + fmt.Sprint(sequence), TaskID: taskID,
		TenantID: tenantID, OwnerID: ownerID, Sequence: sequence, EventType: eventType,
		Payload: append([]byte(nil), payload...), OccurredAt: time.Now().UTC(),
	}
	s.events[taskID] = append(s.events[taskID], event)
	return event
}

func (s *MemoryStore) ListRuns(_ context.Context, tenantID, ownerID, taskID string) ([]AgentRun, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if task, ok := s.tasks[taskID]; !ok || task.TenantID != tenantID || task.OwnerID != ownerID {
		return nil, ErrNotFound
	}
	items := make([]AgentRun, 0)
	for _, run := range s.runs {
		if run.TaskID == taskID && run.TenantID == tenantID && run.OwnerID == ownerID {
			items = append(items, run)
		}
	}
	sort.Slice(items, func(i, j int) bool { return items[i].CreatedAt.After(items[j].CreatedAt) })
	return items, nil
}

func (s *MemoryStore) StopRun(_ context.Context, tenantID, ownerID, taskID, runID string) (AgentRun, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	run, ok := s.runs[runID]
	if !ok || run.TaskID != taskID || run.TenantID != tenantID || run.OwnerID != ownerID {
		return AgentRun{}, ErrNotFound
	}
	if !run.Status.Terminal() {
		run.Status = RunStopping
		run.UpdatedAt = time.Now().UTC()
		s.runs[runID] = run
	}
	return run, nil
}

func (s *MemoryStore) AcquireRun(_ context.Context, runID, workerID string, now time.Time, ttl time.Duration) (AgentRun, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	run, ok := s.runs[runID]
	if !ok {
		return AgentRun{}, ErrNotFound
	}
	if run.Status != RunQueued && !(run.Status == RunRunning && !run.LeaseExpiresAt.After(now)) {
		if run.Status == RunRunning && run.LeaseExpiresAt.After(now) {
			return AgentRun{}, ErrLeaseHeld
		}
		return AgentRun{}, ErrInvalidRunStatus
	}
	if ttl <= 0 {
		ttl = 30 * time.Second
	}
	run.Status = RunRunning
	run.WorkerID = workerID
	leaseID, err := id.New("lease")
	if err != nil {
		return AgentRun{}, err
	}
	run.LeaseID = leaseID
	run.FencingToken++
	run.LeaseExpiresAt = now.Add(ttl)
	run.AttemptCount++
	run.UpdatedAt = now
	s.runs[runID] = run
	return run, nil
}

func (s *MemoryStore) Heartbeat(_ context.Context, lease LeaseContext, now time.Time, ttl time.Duration) (LeaseContext, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	run, ok := s.runs[lease.RunID]
	if !ok {
		return LeaseContext{}, ErrNotFound
	}
	if !validLease(run, lease, now) {
		return LeaseContext{}, ErrLeaseLost
	}
	if ttl <= 0 {
		ttl = 30 * time.Second
	}
	run.LeaseExpiresAt = now.Add(ttl)
	run.UpdatedAt = now
	s.runs[lease.RunID] = run
	return leaseFromRun(run), nil
}

func (s *MemoryStore) CompleteRun(_ context.Context, lease LeaseContext, status RunStatus, now time.Time) (AgentRun, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	run, ok := s.runs[lease.RunID]
	if !ok {
		return AgentRun{}, ErrNotFound
	}
	if !validLease(run, lease, now) {
		return AgentRun{}, ErrLeaseLost
	}
	if !status.Terminal() {
		return AgentRun{}, ErrInvalidRunStatus
	}
	run.Status = status
	run.QueueSlotAcquired = false
	run.LeaseID = ""
	run.WorkerID = ""
	run.LeaseExpiresAt = time.Time{}
	run.UpdatedAt = now
	s.runs[lease.RunID] = run
	return run, nil
}

func (s *MemoryStore) GetRunContext(_ context.Context, lease LeaseContext) (AgentRunInput, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	run, ok := s.runs[lease.RunID]
	if !ok {
		return AgentRunInput{}, ErrNotFound
	}
	if !validLease(run, lease, time.Now().UTC()) {
		return AgentRunInput{}, ErrLeaseLost
	}
	task, ok := s.tasks[run.TaskID]
	if !ok {
		return AgentRunInput{}, ErrNotFound
	}
	checkpoint := s.checkpoints[run.RunID]
	evidence := make([]EvidenceItem, 0)
	for _, item := range s.evidence {
		if item.RunID == run.RunID {
			evidence = append(evidence, EvidenceItem{SourceType: item.SourceType, SourceID: item.SourceID, Locator: item.Locator, ExcerptHash: item.ExcerptHash, Excerpt: item.Excerpt})
		}
	}
	sort.Slice(evidence, func(i, j int) bool {
		return evidence[i].SourceType+"\x00"+evidence[i].SourceID+"\x00"+evidence[i].Locator <
			evidence[j].SourceType+"\x00"+evidence[j].SourceID+"\x00"+evidence[j].Locator
	})
	artifacts := make([]RunArtifact, 0)
	for key, item := range s.artifacts {
		if strings.HasPrefix(key, run.RunID+"\x00") {
			copy := item
			copy.Content = append([]byte(nil), item.Content...)
			artifacts = append(artifacts, copy)
		}
	}
	sort.Slice(artifacts, func(i, j int) bool { return artifacts[i].ArtifactKey < artifacts[j].ArtifactKey })
	summary := ResumeStateSummary{
		CheckpointContentHash: checkpoint.contentHash,
		EvidenceCount:         int64(len(evidence)),
		ArtifactCount:         int64(len(artifacts)),
	}
	for _, item := range evidence {
		summary.EvidenceRefs = append(summary.EvidenceRefs, EvidenceReference(item))
	}
	for _, item := range artifacts {
		summary.Artifacts = append(summary.Artifacts, RunArtifactIdentity{ArtifactKey: item.ArtifactKey, ArtifactType: item.ArtifactType, Generation: item.Generation, RequestHash: item.RequestHash, ContentHash: item.ContentHash})
	}
	for _, attempt := range s.attempts {
		if attempt.runID == run.RunID && attempt.value.Status != "PLANNED" {
			summary.TerminalModelAttemptCount++
		}
	}
	var submittedDraft *SubmittedDraftReceipt
	for _, draft := range s.drafts {
		if draft.runID == run.RunID && (submittedDraft == nil || draft.taskVersion > submittedDraft.TaskVersion) {
			submittedDraft = &SubmittedDraftReceipt{DraftKey: draft.draftKey, ContentHash: draft.patchHash, TaskVersion: draft.taskVersion, Content: append([]byte(nil), draft.patch...)}
		}
	}
	scope := s.revisionScopes[run.RunID]
	scope.ReopenedUnitKeys = append([]string(nil), scope.ReopenedUnitKeys...)
	scope.ImmutableUnitKeys = append([]string(nil), scope.ImmutableUnitKeys...)
	var baseDraft *SubmittedDraftReceipt
	if scope.BaseDraftID != "" {
		base := s.draftByIDLocked(scope.BaseDraftID)
		baseDraft = &SubmittedDraftReceipt{DraftKey: base.draftKey, ContentHash: base.patchHash, TaskVersion: base.taskVersion, Content: append([]byte(nil), base.patch...)}
	}
	legacyDraft := submittedDraft
	if baseDraft != nil {
		legacyDraft = baseDraft
	}
	baseTaskVersion := task.Version
	if run.WorkflowVersion == WorkflowVersionV4 && submittedDraft != nil {
		baseTaskVersion = submittedDraft.TaskVersion - 1
	}
	budget := s.budgets[run.RunID]
	ledgerEntries := make([]LedgerEntry, 0)
	for _, entry := range s.ledgerEntries {
		if entry.RunID == run.RunID {
			entry.EvidenceRefs = append([]string(nil), entry.EvidenceRefs...)
			ledgerEntries = append(ledgerEntries, entry)
		}
	}
	sort.Slice(ledgerEntries, func(i, j int) bool { return ledgerEntries[i].CreatedAt.Before(ledgerEntries[j].CreatedAt) })
	unitScope := s.unitScopes[lease.RunID]
	input := AgentRunInput{Run: run, TaskMessage: task.Message, WorkflowVersion: string(run.WorkflowVersion), Checkpoint: append([]byte(nil), checkpoint.payload...), CheckpointSequence: checkpoint.sequence, TaskVersion: baseTaskVersion, ResumeEvidence: evidence, ResumeArtifacts: artifacts, RevisionScope: scope, ResumeDraft: legacyDraft, BaseDraft: baseDraft, SubmittedDraft: submittedDraft, ResumeSummary: summary, ExecutionLedgerVersion: string(run.ExecutionLedgerVersion), RunBudget: budget.policy, ConsumedBudget: budget.consumed, LedgerEntries: ledgerEntries, RunPurpose: unitScope.Purpose, UnitScope: unitScope}
	if assignment, ok := s.rolloutAssignments[lease.RunID]; ok {
		input.EvaluationMode = assignment.EvaluationMode
		input.AuthoritativeWorkflow = assignment.AuthoritativeWorkflowVersion
		input.ShadowWorkflow = assignment.ShadowWorkflowVersion
		input.CandidatePolicyVersion = assignment.PolicyVersion
		input.AssignmentHash = assignment.AssignmentHash
	}
	if assignment, ok := s.runMemoryAssignments[lease.RunID]; ok {
		input.MemorySpaceID = assignment.SpaceID
		input.MemoryWatermark = assignment.MemoryWatermark
		input.MemoryPolicyVersion = assignment.PolicyVersion
		input.MemoryAccessScopeHash = assignment.AccessScopeHash
		input.MemoryAssignmentHash = assignment.AssignmentHash
	}
	return input, nil
}

func (s *MemoryStore) GetRun(_ context.Context, runID string) (AgentRun, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	run, ok := s.runs[runID]
	if !ok {
		return AgentRun{}, ErrNotFound
	}
	return run, nil
}

func (s *MemoryStore) RecordModelAttempt(_ context.Context, lease LeaseContext, attempt ModelAttempt) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.validateLeaseLocked(lease, time.Now().UTC()); err != nil {
		return "", err
	}
	if attempt.AttemptKey == "" || attempt.Operation == "" || attempt.RequestHash == "" {
		return "", fmt.Errorf("%w: attempt_key, operation and request_hash are required", ErrInvalidPayload)
	}
	metadata := strings.ToLower(attempt.ResponseMetadataJSON + "\x00" + attempt.TokenUsageJSON)
	for _, secretMarker := range []string{"authorization", "api_key", "access_token", "jwt"} {
		if strings.Contains(metadata, secretMarker) {
			return "", ErrSensitivePayload
		}
	}
	key := lease.RunID + "\x00" + attempt.AttemptKey
	if existing, ok := s.attempts[key]; ok {
		if existing.value.RequestHash != attempt.RequestHash {
			return "", ErrInvalidIdempotency
		}
		if existing.value.Status != "PLANNED" && attempt.Status != existing.value.Status {
			if attempt.Status == "PLANNED" {
				return existing.attemptID, nil
			}
			return "", ErrInvalidIdempotency
		}
		existing.value = attempt
		s.attempts[key] = existing
		return existing.attemptID, nil
	}
	attemptID := "attempt-" + stableHash(key)
	s.attempts[key] = memoryAttempt{
		attemptID: attemptID, runID: lease.RunID, value: attempt, createdAt: time.Now().UTC(),
	}
	return attemptID, nil
}

func (s *MemoryStore) HasModelAttempts(_ context.Context, runID string) (bool, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if _, ok := s.runs[runID]; !ok {
		return false, ErrNotFound
	}
	for _, attempt := range s.attempts {
		if attempt.runID == runID {
			return true, nil
		}
	}
	return false, nil
}

func (s *MemoryStore) ReserveLedgerEntry(_ context.Context, lease LeaseContext, entry LedgerEntry) (LedgerEntry, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.validateLedgerMutationLocked(lease, entry); err != nil {
		return LedgerEntry{}, err
	}
	if assignment, ok := s.rolloutAssignments[lease.RunID]; ok && assignment.EvaluationMode == EvaluationShadow && (entry.EntryKind == LedgerEntryModel || entry.EntryKind == LedgerEntryCapability) {
		return LedgerEntry{}, fmt.Errorf("%w: SHADOW_REMOTE_EFFECT_FORBIDDEN", errNoRemoteEffects)
	}
	key := ledgerMapKey(lease.RunID, entry.OperationKey)
	if existing, ok := s.ledgerEntries[key]; ok {
		if !sameLedgerIdentity(existing, entry) {
			return LedgerEntry{}, ErrInvalidIdempotency
		}
		return cloneLedgerEntry(existing), nil
	}
	if entry.EntryKind == LedgerEntryLocalTransition {
		return LedgerEntry{}, fmt.Errorf("%w: local transition must use ConsumeLocalLedgerEntry", ErrLedgerConflict)
	}
	budget := s.budgets[lease.RunID]
	if elapsedBudgetExhausted(budget, time.Now().UTC()) {
		return LedgerEntry{}, ErrBudgetExhausted
	}
	if !budgetAllows(budget.policy, addBudget(budget.reserved, budget.consumed), entry.Reservation) {
		return LedgerEntry{}, ErrBudgetExhausted
	}
	now := time.Now().UTC()
	entry.EntryID = "ledger-" + stableHash(key)
	entry.RunID = lease.RunID
	entry.Status = LedgerReserved
	entry.Consumption = BudgetDelta{}
	entry.CreatedAt, entry.UpdatedAt = now, now
	entry.EvidenceRefs = append([]string(nil), entry.EvidenceRefs...)
	budget.reserved = addBudget(budget.reserved, entry.Reservation)
	s.budgets[lease.RunID] = budget
	s.ledgerEntries[key] = entry
	return cloneLedgerEntry(entry), nil
}

func (s *MemoryStore) MarkLedgerCallStarted(_ context.Context, lease LeaseContext, operationKey, requestHash string) (LedgerEntry, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.validateLeaseLocked(lease, time.Now().UTC()); err != nil {
		return LedgerEntry{}, err
	}
	key := ledgerMapKey(lease.RunID, operationKey)
	entry, ok := s.ledgerEntries[key]
	if !ok {
		return LedgerEntry{}, ErrNotFound
	}
	if entry.RequestHash != requestHash {
		return LedgerEntry{}, ErrInvalidIdempotency
	}
	if entry.Status == LedgerCallStarted {
		return cloneLedgerEntry(entry), nil
	}
	if entry.Status != LedgerReserved {
		return LedgerEntry{}, ErrLedgerConflict
	}
	now := time.Now().UTC()
	entry.Status, entry.CallStartedAt, entry.UpdatedAt = LedgerCallStarted, &now, now
	s.ledgerEntries[key] = entry
	return cloneLedgerEntry(entry), nil
}

func (s *MemoryStore) FinishLedgerEntry(_ context.Context, lease LeaseContext, result LedgerFinish) (LedgerEntry, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.validateLeaseLocked(lease, time.Now().UTC()); err != nil {
		return LedgerEntry{}, err
	}
	key := ledgerMapKey(lease.RunID, result.OperationKey)
	entry, ok := s.ledgerEntries[key]
	if !ok {
		return LedgerEntry{}, ErrNotFound
	}
	if entry.RequestHash != result.RequestHash {
		return LedgerEntry{}, ErrInvalidIdempotency
	}
	if entry.Status == LedgerSucceeded || entry.Status == LedgerFailed || entry.Status == LedgerOutcomeUnknown {
		if !sameLedgerFinish(entry, result) {
			return LedgerEntry{}, ErrLedgerConflict
		}
		return cloneLedgerEntry(entry), nil
	}
	if entry.Status != LedgerCallStarted || (result.Status != LedgerSucceeded && result.Status != LedgerFailed && result.Status != LedgerOutcomeUnknown) {
		return LedgerEntry{}, ErrLedgerConflict
	}
	if result.Status == LedgerSucceeded && result.OutputArtifactKey == "" {
		return LedgerEntry{}, fmt.Errorf("%w: successful remote entry requires an outcome artifact", ErrLedgerConflict)
	}
	if result.Status == LedgerSucceeded && result.OutputArtifactKey != "" {
		artifact, ok := s.artifacts[lease.RunID+"\x00"+result.OutputArtifactKey]
		if !ok || artifact.ContentHash != result.OutputArtifactHash || artifact.RequestHash != result.RequestHash {
			return LedgerEntry{}, fmt.Errorf("%w: referenced outcome artifact is not durable", ErrLedgerConflict)
		}
	}
	if result.Status == LedgerSucceeded {
		for _, ref := range result.EvidenceRefs {
			if !s.hasEvidenceRefLocked(lease.RunID, ref) {
				return LedgerEntry{}, fmt.Errorf("%w: referenced evidence is not durable", ErrLedgerConflict)
			}
		}
	}
	budget := s.budgets[lease.RunID]
	budget.reserved = subtractBudgetFloor(budget.reserved, entry.Reservation)
	budget.consumed = addBudget(budget.consumed, result.Consumption)
	budget.overage = budgetOverage(budget.policy, budget.consumed)
	s.budgets[lease.RunID] = budget
	now := time.Now().UTC()
	entry.Status = result.Status
	entry.Consumption = result.Consumption
	entry.OutputArtifactKey = result.OutputArtifactKey
	entry.OutputArtifactHash = result.OutputArtifactHash
	entry.EvidenceRefs = append([]string(nil), result.EvidenceRefs...)
	entry.ErrorCategory, entry.Retryable = result.ErrorCategory, result.Retryable
	entry.CompletedAt, entry.UpdatedAt = &now, now
	s.ledgerEntries[key] = entry
	return cloneLedgerEntry(entry), nil
}

func (s *MemoryStore) ConsumeLocalLedgerEntry(_ context.Context, lease LeaseContext, entry LedgerEntry) (LedgerEntry, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.validateLedgerMutationLocked(lease, entry); err != nil {
		return LedgerEntry{}, err
	}
	if entry.EntryKind != LedgerEntryLocalTransition {
		return LedgerEntry{}, ErrLedgerConflict
	}
	key := ledgerMapKey(lease.RunID, entry.OperationKey)
	if existing, ok := s.ledgerEntries[key]; ok {
		if !sameLedgerIdentity(existing, entry) {
			return LedgerEntry{}, ErrInvalidIdempotency
		}
		return cloneLedgerEntry(existing), nil
	}
	budget := s.budgets[lease.RunID]
	if elapsedBudgetExhausted(budget, time.Now().UTC()) {
		return LedgerEntry{}, ErrBudgetExhausted
	}
	if !budgetAllows(budget.policy, budget.consumed, entry.Consumption) {
		return LedgerEntry{}, ErrBudgetExhausted
	}
	now := time.Now().UTC()
	entry.EntryID, entry.RunID, entry.Status = "ledger-"+stableHash(key), lease.RunID, LedgerSucceeded
	entry.Reservation = BudgetDelta{}
	entry.CreatedAt, entry.CompletedAt, entry.UpdatedAt = now, &now, now
	budget.consumed = addBudget(budget.consumed, entry.Consumption)
	s.budgets[lease.RunID] = budget
	s.ledgerEntries[key] = entry
	return cloneLedgerEntry(entry), nil
}

func (s *MemoryStore) AppendEvidence(_ context.Context, lease LeaseContext, items []EvidenceItem) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.validateLeaseLocked(lease, time.Now().UTC()); err != nil {
		return 0, err
	}
	accepted := 0
	for _, item := range items {
		if item.SourceType == "" || item.SourceID == "" || item.Locator == "" || item.ExcerptHash == "" {
			return 0, ErrInvalidPayload
		}
		key := lease.RunID + "\x00" + item.SourceType + "\x00" + item.SourceID + "\x00" + item.Locator + "\x00" + item.ExcerptHash
		if _, exists := s.evidence[key]; exists {
			continue
		}
		s.evidence[key] = EvidenceRecord{
			EvidenceID: "evidence-" + stableHash(key), RunID: lease.RunID,
			SourceType: item.SourceType, SourceID: item.SourceID, Locator: item.Locator,
			ExcerptHash: item.ExcerptHash, Excerpt: item.Excerpt, CreatedAt: time.Now().UTC(),
		}
		accepted++
	}
	return accepted, nil
}

func (s *MemoryStore) SaveRunArtifact(_ context.Context, lease LeaseContext, artifact RunArtifact) (RunArtifactReceipt, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.validateLeaseLocked(lease, time.Now().UTC()); err != nil {
		return RunArtifactReceipt{}, err
	}
	if artifact.ArtifactKey == "" || artifact.ArtifactType == "" || artifact.Generation < 0 || artifact.RequestHash == "" || len(artifact.Content) == 0 {
		return RunArtifactReceipt{}, ErrInvalidPayload
	}
	if len(artifact.Content) > MaxRunArtifactBytes {
		return RunArtifactReceipt{}, ErrPayloadTooLarge
	}
	hash := stableHash(string(artifact.Content))
	if artifact.ContentHash != "" && artifact.ContentHash != hash {
		return RunArtifactReceipt{}, ErrInvalidPayload
	}
	if artifact.ArtifactType == "SHADOW_EVALUATION" {
		assignment, ok := s.rolloutAssignments[lease.RunID]
		if !ok {
			return RunArtifactReceipt{}, ErrInvalidPayload
		}
		trace, err := ParseShadowEvaluationArtifact(lease.RunID, artifact, assignment)
		if err != nil {
			return RunArtifactReceipt{}, err
		}
		run := s.runs[lease.RunID]
		persisted := PersistedAuthoritativeResult{RunID: lease.RunID, TaskID: run.TaskID}
		if trace.OutputKey != "" {
			output, ok := s.runOutputs[lease.RunID+"\x00"+trace.OutputKey]
			if !ok {
				return RunArtifactReceipt{}, fmt.Errorf("%w: shadow output was not persisted", ErrInvalidPayload)
			}
			persisted.OutputKey, persisted.OutputKind, persisted.OutputContentHash = output.OutputKey, output.OutputKind, output.ContentHash
		} else {
			for _, draft := range s.drafts {
				if draft.runID == lease.RunID && draft.draftKey == trace.DraftKey {
					persisted.DraftKey, persisted.DraftContentHash = draft.draftKey, draft.patchHash
					break
				}
			}
		}
		if err := ValidatePersistedAuthoritativeTrace(trace, persisted); err != nil {
			return RunArtifactReceipt{}, err
		}
	}
	artifact.ContentHash = hash
	key := lease.RunID + "\x00" + artifact.ArtifactKey
	if existing, ok := s.artifacts[key]; ok {
		if existing.RequestHash != artifact.RequestHash || existing.ContentHash != artifact.ContentHash {
			return RunArtifactReceipt{}, ErrInvalidIdempotency
		}
		return RunArtifactReceipt{ArtifactKey: existing.ArtifactKey, ContentHash: existing.ContentHash}, nil
	}
	artifact.Content = append([]byte(nil), artifact.Content...)
	s.artifacts[key] = artifact
	return RunArtifactReceipt{ArtifactKey: artifact.ArtifactKey, ContentHash: artifact.ContentHash}, nil
}

func (s *MemoryStore) SaveCheckpoint(_ context.Context, lease LeaseContext, sequence int64, checkpoint []byte) (CheckpointReceipt, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.validateLeaseLocked(lease, time.Now().UTC()); err != nil {
		return CheckpointReceipt{}, err
	}
	if len(checkpoint) > MaxCheckpointBytes {
		return CheckpointReceipt{}, ErrPayloadTooLarge
	}
	current := s.checkpoints[lease.RunID]
	hash := sha256.Sum256(checkpoint)
	contentHash := hex.EncodeToString(hash[:])
	if sequence < current.sequence {
		return CheckpointReceipt{}, ErrCheckpointConflict
	}
	if sequence == current.sequence && current.sequence != 0 {
		if current.contentHash != contentHash {
			return CheckpointReceipt{}, ErrCheckpointConflict
		}
		return CheckpointReceipt{Sequence: sequence, ContentHash: contentHash}, nil
	}
	s.checkpoints[lease.RunID] = memoryCheckpoint{sequence: sequence, payload: append([]byte(nil), checkpoint...), contentHash: contentHash}
	return CheckpointReceipt{Sequence: sequence, ContentHash: contentHash}, nil
}

func (s *MemoryStore) SetRunUnitScope(_ context.Context, tenantID, ownerID, runID string, expectedTaskVersion int, scope UnitScope) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	run, ok := s.runs[runID]
	if !ok || run.TenantID != tenantID || run.OwnerID != ownerID {
		return ErrNotFound
	}
	task := s.tasks[run.TaskID]
	if task.Version != expectedTaskVersion {
		return ErrTaskVersionConflict
	}
	if run.WorkflowVersion != WorkflowVersionV4 {
		return ErrInvalidPayload
	}
	built, err := BuildUnitScope(scope)
	if err != nil {
		return err
	}
	if existing, exists := s.unitScopes[runID]; exists {
		if existing.ScopeHash != built.ScopeHash {
			return ErrInvalidIdempotency
		}
		return nil
	}
	s.unitScopes[runID] = built
	return nil
}

func (s *MemoryStore) SubmitRunOutput(_ context.Context, lease LeaseContext, output RunOutput) (RunOutputReceipt, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.validateLeaseLocked(lease, time.Now().UTC()); err != nil {
		return RunOutputReceipt{}, err
	}
	run := s.runs[lease.RunID]
	task := s.tasks[run.TaskID]
	scope, ok := s.unitScopes[lease.RunID]
	if !ok {
		return RunOutputReceipt{}, ErrInvalidPayload
	}
	identity := lease.RunID + "\x00" + output.OutputKey
	if existing, exists := s.runOutputs[identity]; exists {
		if bareSHA256(existing.ContentHash) != bareSHA256(output.ContentHash) {
			return RunOutputReceipt{}, ErrInvalidIdempotency
		}
		return s.runOutputReceipts[identity], nil
	}
	if err := ValidateRunOutput(scope, output, task.Version); err != nil {
		return RunOutputReceipt{}, err
	}
	var outlineCandidate *OutlineCandidate
	var unitCandidate *struct {
		UnitKey     string   `json:"unit_key"`
		Title       string   `json:"title"`
		Ordinal     int      `json:"ordinal"`
		NodeKeys    []string `json:"node_keys"`
		Markdown    string   `json:"markdown"`
		ContentHash string   `json:"content_hash"`
		ClaimIDs    []string `json:"claim_ids"`
		UnknownIDs  []string `json:"unknown_ids"`
	}
	var fullReviewCandidate *struct {
		OutlineHash string            `json:"outline_hash"`
		UnitHashes  map[string]string `json:"unit_hashes"`
		Outcome     string            `json:"outcome"`
	}
	var unitPatch *struct {
		UnitKey             string   `json:"unit_key"`
		BaseContentHash     string   `json:"base_content_hash"`
		ReplacementMarkdown string   `json:"replacement_markdown"`
		ContentHash         string   `json:"content_hash"`
		PreservedUnknownIDs []string `json:"preserved_unknown_ids"`
		UsedFactIDs         []string `json:"used_fact_ids"`
	}
	if output.RunPurpose == RunPurposePlanOutline {
		candidate, err := ParseOutlineCandidate(output.Payload)
		if err != nil {
			return RunOutputReceipt{}, err
		}
		outlineCandidate = &candidate
	}
	if output.RunPurpose == RunPurposeGenerateUnit {
		candidate := new(struct {
			UnitKey     string   `json:"unit_key"`
			Title       string   `json:"title"`
			Ordinal     int      `json:"ordinal"`
			NodeKeys    []string `json:"node_keys"`
			Markdown    string   `json:"markdown"`
			ContentHash string   `json:"content_hash"`
			ClaimIDs    []string `json:"claim_ids"`
			UnknownIDs  []string `json:"unknown_ids"`
		})
		if err := json.Unmarshal(output.Payload, candidate); err != nil {
			return RunOutputReceipt{}, ErrInvalidPayload
		}
		unitCandidate = candidate
	}
	if output.RunPurpose == RunPurposeFullReview {
		candidate := new(struct {
			OutlineHash string            `json:"outline_hash"`
			UnitHashes  map[string]string `json:"unit_hashes"`
			Outcome     string            `json:"outcome"`
		})
		if err := json.Unmarshal(output.Payload, candidate); err != nil || (candidate.Outcome != "PASSED" && candidate.Outcome != "NEEDS_REVISION") {
			return RunOutputReceipt{}, ErrInvalidPayload
		}
		fullReviewCandidate = candidate
	}
	if output.RunPurpose == RunPurposeReviseUnit {
		candidate := new(struct {
			UnitKey             string   `json:"unit_key"`
			BaseContentHash     string   `json:"base_content_hash"`
			ReplacementMarkdown string   `json:"replacement_markdown"`
			ContentHash         string   `json:"content_hash"`
			PreservedUnknownIDs []string `json:"preserved_unknown_ids"`
			UsedFactIDs         []string `json:"used_fact_ids"`
		})
		if err := json.Unmarshal(output.Payload, candidate); err != nil {
			return RunOutputReceipt{}, ErrInvalidPayload
		}
		if candidate.ContentHash != "" && bareSHA256(candidate.ContentHash) != hashText(strings.TrimSpace(candidate.ReplacementMarkdown)) {
			return RunOutputReceipt{}, ErrInvalidPayload
		}
		unitPatch = candidate
	}
	copyOutput := output
	copyOutput.Payload = append([]byte(nil), output.Payload...)
	copyOutput.ContentHash = bareSHA256(output.ContentHash)
	s.runOutputs[identity] = copyOutput
	if output.RunPurpose == RunPurposePlanOutline {
		candidate := *outlineCandidate
		now := time.Now().UTC()
		outlineID := "outline-" + stableHash(task.TaskID)
		outline := ReviewOutlineVersion{
			OutlineID: outlineID, OutlineVersionID: "outline-version-" + stableHash(identity),
			TaskID: task.TaskID, Version: 1, Status: OutlineDraft, Candidate: candidate,
			ContentHash: candidate.ContentHash, SourceRunID: run.RunID, CreatedAt: now,
		}
		s.reviewOutlines[task.TaskID] = outline
		task.Version++
		task.Status = string(ReviewOutlineReview)
		task.UpdatedAt = now
		s.tasks[task.TaskID] = task
		payload, _ := json.Marshal(map[string]any{"outline_version_id": outline.OutlineVersionID, "task_version": task.Version})
		s.appendTaskEventLocked(task.TenantID, task.OwnerID, task.TaskID, "review.outline_materialized", payload)
	}
	if output.RunPurpose == RunPurposeGenerateUnit {
		now := time.Now().UTC()
		outline, ok := s.reviewOutlines[task.TaskID]
		unit, unitOK := s.reviewUnits[task.TaskID][scope.CurrentUnitKey]
		if !ok || outline.Status != OutlineLocked || !unitOK || unit.Status != UnitPending {
			return RunOutputReceipt{}, ErrInvalidRunStatus
		}
		versionID := "review-unit-version-" + stableHash(identity)
		value := ConfirmationUnitVersion{
			UnitID: unit.UnitID, UnitVersionID: versionID, OutlineVersionID: outline.OutlineVersionID,
			SourceRunID: run.RunID, UnitVersionNo: 1, UnitKey: unit.UnitKey, Title: unit.Title,
			Ordinal: unit.Ordinal, Markdown: unitCandidate.Markdown,
			ContentHash: bareSHA256(unitCandidate.ContentHash), ClaimIDs: append([]string(nil), unitCandidate.ClaimIDs...),
			UnknownIDs: append([]string(nil), unitCandidate.UnknownIDs...), DependsOn: append([]string(nil), unit.DependsOn...),
			ConfirmationStatus: string(UnitReviewing), CreatedAt: now,
		}
		s.confirmationVersions[versionID] = memoryConfirmationVersion{taskID: task.TaskID, taskVersion: task.Version + 1, value: value}
		unit.Status = UnitReviewing
		s.reviewUnits[task.TaskID][unit.UnitKey] = unit
		task.Version++
		task.Status = string(ReviewUnitReview)
		task.UpdatedAt = now
		s.tasks[task.TaskID] = task
		payload, _ := json.Marshal(map[string]any{"unit_version_id": versionID, "unit_key": unit.UnitKey, "task_version": task.Version})
		s.appendTaskEventLocked(task.TenantID, task.OwnerID, task.TaskID, "review.unit_materialized", payload)
	}
	if output.RunPurpose == RunPurposeFullReview {
		now := time.Now().UTC()
		outline, ok := s.reviewOutlines[task.TaskID]
		if !ok || outline.Status != OutlineLocked || task.Status != string(ReviewFullReviewRunning) {
			return RunOutputReceipt{}, ErrInvalidRunStatus
		}
		current := s.confirmedUnitContextLocked(task.TaskID)
		if !sameConfirmedHashSet(current, scope.ConfirmedContext) {
			return RunOutputReceipt{}, fmt.Errorf("%w: full review scope is stale", ErrTaskVersionConflict)
		}
		unitHashPayload, _ := canonicalJSON(fullReviewCandidate.UnitHashes)
		report := FullReviewReport{
			ReportID: "full-review-" + stableHash(identity), TaskID: task.TaskID,
			OutlineVersionID: outline.OutlineVersionID, SourceRunID: run.RunID,
			ContentHash: copyOutput.ContentHash, Disposition: fullReviewCandidate.Outcome,
			UnitHashes: cloneStringMap(fullReviewCandidate.UnitHashes), UnitHashSetHash: hashBytesHex(unitHashPayload),
			Payload: append([]byte(nil), copyOutput.Payload...), CreatedAt: now,
		}
		s.fullReviewReports[task.TaskID] = report
		task.Version++
		if report.Disposition == "PASSED" {
			task.Status = string(ReviewReviewable)
		} else {
			task.Status = string(ReviewFullReview)
		}
		task.UpdatedAt = now
		s.tasks[task.TaskID] = task
		payload, _ := json.Marshal(map[string]any{"report_id": report.ReportID, "disposition": report.Disposition, "task_version": task.Version})
		s.appendTaskEventLocked(task.TenantID, task.OwnerID, task.TaskID, "review.full_review_materialized", payload)
	}
	if output.RunPurpose == RunPurposeReviseUnit {
		now := time.Now().UTC()
		outline, ok := s.reviewOutlines[task.TaskID]
		unit, unitOK := s.reviewUnits[task.TaskID][scope.CurrentUnitKey]
		if !ok || outline.Status != OutlineLocked || !unitOK || unit.Status != UnitReopened {
			return RunOutputReceipt{}, ErrInvalidRunStatus
		}
		var base ConfirmationUnitVersion
		for _, item := range s.confirmationVersions {
			if item.taskID == task.TaskID && item.value.UnitKey == unit.UnitKey && item.value.UnitVersionNo > base.UnitVersionNo {
				base = item.value
			}
		}
		if base.UnitVersionNo < 1 || bareSHA256(base.ContentHash) != bareSHA256(unitPatch.BaseContentHash) {
			return RunOutputReceipt{}, ErrTaskVersionConflict
		}
		versionID := "review-unit-version-" + stableHash(identity)
		value := ConfirmationUnitVersion{
			UnitID: unit.UnitID, UnitVersionID: versionID, OutlineVersionID: outline.OutlineVersionID,
			SourceRunID: run.RunID, UnitVersionNo: base.UnitVersionNo + 1, UnitKey: unit.UnitKey, Title: unit.Title,
			Ordinal: unit.Ordinal, Markdown: strings.TrimSpace(unitPatch.ReplacementMarkdown),
			ContentHash: hashText(strings.TrimSpace(unitPatch.ReplacementMarkdown)),
			UnknownIDs:  append([]string(nil), unitPatch.PreservedUnknownIDs...), DependsOn: append([]string(nil), unit.DependsOn...),
			ConfirmationStatus: string(UnitReviewing), CreatedAt: now,
		}
		s.confirmationVersions[versionID] = memoryConfirmationVersion{taskID: task.TaskID, taskVersion: task.Version + 1, value: value}
		unit.Status = UnitReviewing
		s.reviewUnits[task.TaskID][unit.UnitKey] = unit
		task.Version++
		task.Status = string(ReviewUnitReview)
		task.UpdatedAt = now
		s.tasks[task.TaskID] = task
		payload, _ := json.Marshal(map[string]any{"unit_version_id": versionID, "unit_key": unit.UnitKey, "unit_version_no": value.UnitVersionNo, "task_version": task.Version})
		s.appendTaskEventLocked(task.TenantID, task.OwnerID, task.TaskID, "review.unit_revision_materialized", payload)
	}
	receipt := RunOutputReceipt{OutputKey: output.OutputKey, ContentHash: copyOutput.ContentHash, TaskVersion: task.Version}
	s.runOutputReceipts[identity] = receipt
	return receipt, nil
}

func (s *MemoryStore) SubmitDraft(_ context.Context, lease LeaseContext, draftKey string, expectedTaskVersion int, patch []byte) (DraftReceipt, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.validateLeaseLocked(lease, time.Now().UTC()); err != nil {
		return DraftReceipt{}, err
	}
	run := s.runs[lease.RunID]
	task := s.tasks[run.TaskID]
	if draftKey == "" || len(patch) > MaxDraftPatchBytes {
		return DraftReceipt{}, ErrInvalidPayload
	}
	patchHash := stableHash(string(patch))
	draftIdentity := task.TaskID + "\x00" + draftKey
	if existing, ok := s.drafts[draftIdentity]; ok {
		if existing.patchHash != patchHash {
			return DraftReceipt{}, ErrInvalidIdempotency
		}
		return DraftReceipt{TaskVersion: existing.taskVersion}, nil
	}
	if task.Version != expectedTaskVersion {
		return DraftReceipt{}, ErrTaskVersionConflict
	}
	candidates, _, err := ParseConfirmationCandidates(patch, task.TaskID, lease.RunID)
	if err != nil {
		return DraftReceipt{}, err
	}
	now := time.Now().UTC()
	draftID := "draft-" + stableHash(draftIdentity)
	task.Version++
	task.UpdatedAt = now
	s.tasks[task.TaskID] = task
	s.drafts[draftIdentity] = memoryDraft{
		draftID: draftID, runID: lease.RunID,
		draftKey: draftKey, patchHash: patchHash, patch: append([]byte(nil), patch...),
		taskVersion: task.Version, createdAt: now,
	}
	for _, candidate := range candidates {
		unitID := "unit-" + stableHash(task.TaskID+"\x00"+candidate.UnitKey)
		versionID := "unit-version-" + stableHash(draftID+"\x00"+unitID)
		status := "PENDING"
		scope := s.revisionScopes[lease.RunID]
		if stringInSlice(candidate.UnitKey, scope.ImmutableUnitKeys) {
			for _, prior := range s.confirmationVersions {
				if prior.value.DraftID == scope.BaseDraftID &&
					prior.value.UnitKey == candidate.UnitKey &&
					prior.value.ContentHash == candidate.ContentHash &&
					prior.value.ConfirmationStatus == "CONFIRMED" {
					status = "CONFIRMED"
					break
				}
			}
		}
		s.confirmationVersions[versionID] = memoryConfirmationVersion{
			taskID: task.TaskID, taskVersion: task.Version,
			value: ConfirmationUnitVersion{
				UnitID: unitID, UnitVersionID: versionID, DraftID: draftID,
				UnitKey: candidate.UnitKey, Title: candidate.Title, Ordinal: candidate.Ordinal,
				Markdown: candidate.Markdown, ContentHash: candidate.ContentHash,
				ClaimIDs:           append([]string(nil), candidate.ClaimIDs...),
				UnknownIDs:         append([]string(nil), candidate.UnknownIDs...),
				DependsOn:          append([]string(nil), candidate.DependsOn...),
				ConfirmationStatus: status, CreatedAt: now,
			},
		}
	}
	return DraftReceipt{TaskVersion: task.Version}, nil
}

func stringInSlice(value string, items []string) bool {
	for _, item := range items {
		if item == value {
			return true
		}
	}
	return false
}

func (s *MemoryStore) GetLatestDraft(_ context.Context, tenantID, ownerID, taskID string) (WorkingDraft, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	task, ok := s.tasks[taskID]
	if !ok || task.TenantID != tenantID || task.OwnerID != ownerID {
		return WorkingDraft{}, ErrNotFound
	}
	var latest memoryDraft
	found := false
	for identity, draft := range s.drafts {
		if !strings.HasPrefix(identity, taskID+"\x00") {
			continue
		}
		if !found || draft.taskVersion > latest.taskVersion {
			latest = draft
			found = true
		}
	}
	if !found {
		return WorkingDraft{}, ErrNotFound
	}
	return WorkingDraft{
		DraftID: latest.draftID, TaskID: taskID, RunID: latest.runID,
		DraftKey: latest.draftKey, Content: append([]byte(nil), latest.patch...),
		ContentHash: latest.patchHash, TaskVersion: latest.taskVersion, CreatedAt: latest.createdAt,
	}, nil
}

func (s *MemoryStore) ListEvidence(_ context.Context, tenantID, ownerID, taskID string, limit int) ([]EvidenceRecord, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	task, ok := s.tasks[taskID]
	if !ok || task.TenantID != tenantID || task.OwnerID != ownerID {
		return nil, ErrNotFound
	}
	runIDs := s.taskRunIDsLocked(taskID)
	items := make([]EvidenceRecord, 0)
	for _, item := range s.evidence {
		if runIDs[item.RunID] {
			items = append(items, item)
		}
	}
	sort.Slice(items, func(i, j int) bool { return items[i].CreatedAt.Before(items[j].CreatedAt) })
	if limit > 0 && len(items) > limit {
		items = items[:limit]
	}
	return items, nil
}

func (s *MemoryStore) ListModelAttempts(_ context.Context, tenantID, ownerID, taskID string, limit int) ([]ModelAttemptRecord, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	task, ok := s.tasks[taskID]
	if !ok || task.TenantID != tenantID || task.OwnerID != ownerID {
		return nil, ErrNotFound
	}
	runIDs := s.taskRunIDsLocked(taskID)
	items := make([]ModelAttemptRecord, 0)
	for _, item := range s.attempts {
		if !runIDs[item.runID] {
			continue
		}
		items = append(items, ModelAttemptRecord{
			AttemptID: item.attemptID, RunID: item.runID, AttemptKey: item.value.AttemptKey,
			Operation: item.value.Operation, PromptVersion: item.value.PromptVersion,
			Provider: item.value.Provider, RequestHash: item.value.RequestHash,
			Status: item.value.Status, ErrorCategory: item.value.ErrorCategory, CreatedAt: item.createdAt,
		})
	}
	sort.Slice(items, func(i, j int) bool { return items[i].CreatedAt.Before(items[j].CreatedAt) })
	if limit > 0 && len(items) > limit {
		items = items[:limit]
	}
	return items, nil
}

func (s *MemoryStore) CreatePublishPreview(_ context.Context, tenantID, ownerID, taskID, idempotencyKey string, expectedTaskVersion int, now time.Time) (PublishPreviewResult, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	task, ok := s.tasks[taskID]
	if !ok || task.TenantID != tenantID || task.OwnerID != ownerID {
		return PublishPreviewResult{}, ErrNotFound
	}
	if expectedTaskVersion < 1 || task.Version != expectedTaskVersion {
		return PublishPreviewResult{}, ErrTaskVersionConflict
	}
	requestHash := stableHash(strings.Join([]string{taskID, fmt.Sprint(expectedTaskVersion)}, "\x00"))
	idempotencyIdentity := tenantID + "\x00" + ownerID + "\x00" + idempotencyKey
	if publishID, ok := s.publishPreviewIdempotency[idempotencyIdentity]; ok {
		value := s.publishes[publishID]
		existingRequestHash := stableHash(strings.Join([]string{value.record.TaskID, fmt.Sprint(value.record.TaskVersion)}, "\x00"))
		if existingRequestHash != requestHash {
			return PublishPreviewResult{}, ErrInvalidIdempotency
		}
		return s.publishPreviewResultLocked(value), nil
	}
	var publishContent []byte
	var publishHash, publishSource string
	var publishVersion int
	if _, isV4 := s.reviewOutlines[taskID]; isV4 {
		document, err := s.buildV4PublishDocumentLocked(task)
		if err != nil {
			return PublishPreviewResult{}, err
		}
		publishContent = append([]byte(nil), document.Content...)
		publishHash = document.ContentHash
		publishSource = document.SourceVersionID
		publishVersion = int(s.reviewOutlines[taskID].Version)
	} else {
		draft, ok := s.latestDraftLocked(taskID)
		if !ok {
			return PublishPreviewResult{}, ErrNotFound
		}
		for _, version := range s.confirmationVersions {
			if version.value.DraftID == draft.draftID && version.value.ConfirmationStatus != "CONFIRMED" {
				return PublishPreviewResult{}, ErrInvalidRunStatus
			}
		}
		publishContent = append([]byte(nil), draft.patch...)
		publishHash = draft.patchHash
		publishSource = draft.draftKey
		publishVersion = draft.taskVersion
	}
	for _, existing := range s.publishes {
		if existing.record.TaskID == taskID && (existing.record.Status == PublishPending || existing.record.Status == PublishRunning || existing.record.Status == PublishReconciling) {
			return PublishPreviewResult{}, ErrInvalidRunStatus
		}
	}
	publishID, err := id.New("publish")
	if err != nil {
		return PublishPreviewResult{}, err
	}
	expiresAt := now.Add(10 * time.Minute)
	value := memoryPublish{
		record: PublishRecord{
			PublishID: publishID, TaskID: taskID, Status: PublishPreview,
			TaskVersion: task.Version, DraftVersion: publishVersion, ContentHash: publishHash,
			CreatedAt: now, UpdatedAt: now,
		},
		tenantID: tenantID, ownerID: ownerID, draftKey: publishSource,
		content: publishContent, expiresAt: expiresAt, availableAt: now,
	}
	s.publishes[publishID] = value
	s.publishPreviewIdempotency[idempotencyIdentity] = publishID
	payload, _ := json.Marshal(map[string]any{"publish_id": publishID, "content_hash": publishHash})
	s.appendTaskEventLocked(tenantID, ownerID, taskID, "publish.previewed", payload)
	return s.publishPreviewResultLocked(value), nil
}

func (s *MemoryStore) ConfirmPublish(_ context.Context, tenantID, ownerID, taskID, publishID, confirmationToken, idempotencyKey string, expectedTaskVersion int, now time.Time) (PublishRecord, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	requestHash := stableHash(strings.Join([]string{taskID, publishID, confirmationToken, fmt.Sprint(expectedTaskVersion)}, "\x00"))
	idempotencyIdentity := tenantID + "\x00" + ownerID + "\x00" + idempotencyKey
	if existingHash, ok := s.publishConfirmIdempotency[idempotencyIdentity]; ok {
		if existingHash != requestHash {
			return PublishRecord{}, ErrInvalidIdempotency
		}
		value, ok := s.publishes[publishID]
		if !ok || value.tenantID != tenantID || value.ownerID != ownerID || value.record.TaskID != taskID {
			return PublishRecord{}, ErrNotFound
		}
		return value.record, nil
	}
	task, ok := s.tasks[taskID]
	value, publishFound := s.publishes[publishID]
	if !ok || !publishFound || task.TenantID != tenantID || task.OwnerID != ownerID ||
		value.tenantID != tenantID || value.ownerID != ownerID || value.record.TaskID != taskID {
		return PublishRecord{}, ErrNotFound
	}
	if task.Version != expectedTaskVersion || value.record.TaskVersion != expectedTaskVersion {
		return PublishRecord{}, ErrTaskVersionConflict
	}
	if value.record.Status != PublishPreview || !now.Before(value.expiresAt) {
		return PublishRecord{}, ErrInvalidRunStatus
	}
	expected := s.publishConfirmationTokenLocked(value)
	if !hmac.Equal([]byte(expected), []byte(confirmationToken)) {
		return PublishRecord{}, ErrInvalidPayload
	}
	value.record.Status = PublishPending
	value.record.UpdatedAt = now
	value.availableAt = now
	s.publishes[publishID] = value
	s.publishConfirmIdempotency[idempotencyIdentity] = requestHash
	task.Status = "PUBLISHING"
	task.UpdatedAt = now
	s.tasks[taskID] = task
	payload, _ := json.Marshal(map[string]any{"publish_id": publishID, "status": PublishPending})
	s.appendTaskEventLocked(tenantID, ownerID, taskID, "publish.pending", payload)
	return value.record, nil
}

func (s *MemoryStore) ListPublishes(_ context.Context, tenantID, ownerID, taskID string, limit int) ([]PublishRecord, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	task, ok := s.tasks[taskID]
	if !ok || task.TenantID != tenantID || task.OwnerID != ownerID {
		return nil, ErrNotFound
	}
	items := make([]PublishRecord, 0)
	for _, value := range s.publishes {
		if value.record.TaskID == taskID && value.tenantID == tenantID && value.ownerID == ownerID {
			items = append(items, value.record)
		}
	}
	sort.Slice(items, func(i, j int) bool { return items[i].CreatedAt.After(items[j].CreatedAt) })
	if limit > 0 && len(items) > limit {
		items = items[:limit]
	}
	return items, nil
}

func (s *MemoryStore) ClaimPendingPublishes(_ context.Context, workerID string, limit int, now time.Time, ttl time.Duration) ([]PublishJob, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if limit < 1 {
		limit = 1
	}
	if ttl <= 0 {
		ttl = time.Minute
	}
	items := make([]PublishJob, 0, limit)
	for publishID, value := range s.publishes {
		claimable := value.record.Status == PublishPending || value.record.Status == PublishRetryable || value.record.Status == PublishReconciling
		if !claimable || value.availableAt.After(now) || (value.claimWorker != "" && value.claimExpires.After(now)) {
			continue
		}
		reconcileOnly := value.record.Status == PublishReconciling
		value.record.Status = PublishRunning
		value.record.UpdatedAt = now
		value.claimWorker = workerID
		value.claimExpires = now.Add(ttl)
		s.publishes[publishID] = value
		items = append(items, PublishJob{
			Record: value.record, TenantID: value.tenantID, OwnerID: value.ownerID,
			Content: append([]byte(nil), value.content...), ReconcileOnly: reconcileOnly,
		})
		if len(items) >= limit {
			break
		}
	}
	return items, nil
}

func (s *MemoryStore) CompletePublish(_ context.Context, workerID, publishID string, result PublishResult, now time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	value, ok := s.publishes[publishID]
	if !ok {
		return ErrNotFound
	}
	if value.claimWorker != workerID || !value.claimExpires.After(now) || value.record.Status != PublishRunning {
		return ErrLeaseLost
	}
	value.record.Status = result.Status
	value.record.SafeURL = result.SafeURL
	value.record.ProviderRevision = result.ProviderRevision
	value.record.ErrorCode = result.ErrorCode
	value.record.Retryable = result.Retryable
	value.record.UpdatedAt = now
	value.claimWorker = ""
	value.claimExpires = time.Time{}
	if result.Status == PublishRetryable {
		value.availableAt = now.Add(30 * time.Second)
	}
	if result.Status == PublishReconciling {
		value.availableAt = now.Add(10 * time.Second)
	}
	s.publishes[publishID] = value
	task := s.tasks[value.record.TaskID]
	if result.Status == PublishSucceeded {
		task.Status = "PUBLISHED"
		task.UpdatedAt = now
		s.tasks[task.TaskID] = task
	} else if result.Status == PublishManualReview {
		task.Status = "REVIEWABLE"
		task.UpdatedAt = now
		s.tasks[task.TaskID] = task
	}
	payload, _ := json.Marshal(map[string]any{"publish_id": publishID, "status": result.Status, "error_code": result.ErrorCode})
	s.appendTaskEventLocked(value.tenantID, value.ownerID, value.record.TaskID, "publish."+strings.ToLower(string(result.Status)), payload)
	return nil
}

func (s *MemoryStore) latestDraftLocked(taskID string) (memoryDraft, bool) {
	var latest memoryDraft
	found := false
	for identity, draft := range s.drafts {
		if strings.HasPrefix(identity, taskID+"\x00") && (!found || draft.taskVersion > latest.taskVersion) {
			latest = draft
			found = true
		}
	}
	return latest, found
}

func (s *MemoryStore) publishPreviewResultLocked(value memoryPublish) PublishPreviewResult {
	return PublishPreviewResult{
		PublishID: value.record.PublishID, ConfirmationToken: s.publishConfirmationTokenLocked(value),
		TaskVersion: value.record.TaskVersion, DraftVersion: value.record.DraftVersion,
		ContentHash: value.record.ContentHash, Target: "FEISHU_FIXED_WIKI_DOCX", ExpiresAt: value.expiresAt,
	}
}

func (s *MemoryStore) publishConfirmationTokenLocked(value memoryPublish) string {
	mac := hmac.New(sha256.New, []byte(s.policy.ConfirmationSecret))
	_, _ = mac.Write([]byte(strings.Join([]string{
		value.record.PublishID, value.record.TaskID, fmt.Sprint(value.record.TaskVersion),
		fmt.Sprint(value.record.DraftVersion), value.record.ContentHash, value.expiresAt.UTC().Format(time.RFC3339Nano),
	}, "\x00")))
	return base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

func (s *MemoryStore) validateLeaseLocked(lease LeaseContext, now time.Time) error {
	run, ok := s.runs[lease.RunID]
	if !ok {
		return ErrNotFound
	}
	if !validLease(run, lease, now) {
		return ErrLeaseLost
	}
	return nil
}

func ledgerVersionForPolicy(policy QueuePolicy) ExecutionLedgerVersion {
	if policy.DefaultWorkflowVersion == WorkflowVersionV4 {
		return policy.DefaultExecutionLedgerVersion
	}
	return ""
}

func (s *MemoryStore) initializeBudgetLocked(run AgentRun, now time.Time) {
	if run.ExecutionLedgerVersion == ExecutionLedgerVersionV1 {
		s.budgets[run.RunID] = memoryBudgetState{policy: s.policy.DefaultRunBudget, startedAt: now}
	}
}

func (s *MemoryStore) validateLedgerMutationLocked(lease LeaseContext, entry LedgerEntry) error {
	if err := s.validateLeaseLocked(lease, time.Now().UTC()); err != nil {
		return err
	}
	run := s.runs[lease.RunID]
	if run.ExecutionLedgerVersion != ExecutionLedgerVersionV1 {
		return fmt.Errorf("%w: run does not use %s", ErrLedgerConflict, ExecutionLedgerVersionV1)
	}
	if entry.OperationKey == "" || entry.Operation == "" || entry.RequestHash == "" || !validLedgerKind(entry.EntryKind) || !nonNegativeBudget(entry.Reservation) || !nonNegativeBudget(entry.Consumption) {
		return ErrInvalidPayload
	}
	return nil
}

func (s *MemoryStore) hasEvidenceRefLocked(runID, ref string) bool {
	for _, item := range s.evidence {
		if item.RunID == runID && EvidenceReference(EvidenceItem{SourceType: item.SourceType, SourceID: item.SourceID, Locator: item.Locator, ExcerptHash: item.ExcerptHash, Excerpt: item.Excerpt}) == ref {
			return true
		}
	}
	return false
}

func ledgerMapKey(runID, operationKey string) string { return runID + "\x00" + operationKey }

func validLedgerKind(kind LedgerEntryKind) bool {
	return kind == LedgerEntryModel || kind == LedgerEntryCapability || kind == LedgerEntryLocalTransition || kind == LedgerEntryLocalDerivation
}

func sameLedgerIdentity(left, right LedgerEntry) bool {
	return left.OperationKey == right.OperationKey && left.EntryKind == right.EntryKind && left.Operation == right.Operation && left.RequestHash == right.RequestHash
}

func sameLedgerFinish(entry LedgerEntry, result LedgerFinish) bool {
	return entry.Status == result.Status && entry.OutputArtifactKey == result.OutputArtifactKey && entry.OutputArtifactHash == result.OutputArtifactHash && entry.ErrorCategory == result.ErrorCategory && entry.Retryable == result.Retryable && entry.Consumption == result.Consumption && strings.Join(entry.EvidenceRefs, "\x00") == strings.Join(result.EvidenceRefs, "\x00")
}

func cloneLedgerEntry(entry LedgerEntry) LedgerEntry {
	entry.EvidenceRefs = append([]string(nil), entry.EvidenceRefs...)
	return entry
}

func addBudget(left, right BudgetDelta) BudgetDelta {
	return BudgetDelta{ModelAttempts: left.ModelAttempts + right.ModelAttempts, ToolCalls: left.ToolCalls + right.ToolCalls, Iterations: left.Iterations + right.Iterations, Replans: left.Replans + right.Replans, Supplements: left.Supplements + right.Supplements, QualityRepairs: left.QualityRepairs + right.QualityRepairs, InputTokens: left.InputTokens + right.InputTokens, OutputTokens: left.OutputTokens + right.OutputTokens, ElapsedMS: left.ElapsedMS + right.ElapsedMS}
}

func subtractBudgetFloor(left, right BudgetDelta) BudgetDelta {
	value := BudgetDelta{ModelAttempts: left.ModelAttempts - right.ModelAttempts, ToolCalls: left.ToolCalls - right.ToolCalls, Iterations: left.Iterations - right.Iterations, Replans: left.Replans - right.Replans, Supplements: left.Supplements - right.Supplements, QualityRepairs: left.QualityRepairs - right.QualityRepairs, InputTokens: left.InputTokens - right.InputTokens, OutputTokens: left.OutputTokens - right.OutputTokens, ElapsedMS: left.ElapsedMS - right.ElapsedMS}
	fields := []*int64{&value.ModelAttempts, &value.ToolCalls, &value.Iterations, &value.Replans, &value.Supplements, &value.QualityRepairs, &value.InputTokens, &value.OutputTokens, &value.ElapsedMS}
	for _, field := range fields {
		if *field < 0 {
			*field = 0
		}
	}
	return value
}

func nonNegativeBudget(value BudgetDelta) bool {
	return value.ModelAttempts >= 0 && value.ToolCalls >= 0 && value.Iterations >= 0 && value.Replans >= 0 && value.Supplements >= 0 && value.QualityRepairs >= 0 && value.InputTokens >= 0 && value.OutputTokens >= 0 && value.ElapsedMS >= 0
}

func budgetAllows(limit RunBudget, used, requested BudgetDelta) bool {
	next := addBudget(used, requested)
	return nonNegativeBudget(requested) && next.ModelAttempts <= limit.MaxModelAttempts && next.ToolCalls <= limit.MaxToolCalls && next.Iterations <= limit.MaxIterations && next.Replans <= limit.MaxReplans && next.Supplements <= limit.MaxSupplements && next.QualityRepairs <= limit.MaxQualityRepairs && next.InputTokens <= limit.MaxInputTokens && next.OutputTokens <= limit.MaxOutputTokens && next.ElapsedMS <= limit.MaxElapsedMS
}

func budgetOverage(limit RunBudget, consumed BudgetDelta) BudgetDelta {
	return BudgetDelta{ModelAttempts: max64(0, consumed.ModelAttempts-limit.MaxModelAttempts), ToolCalls: max64(0, consumed.ToolCalls-limit.MaxToolCalls), Iterations: max64(0, consumed.Iterations-limit.MaxIterations), Replans: max64(0, consumed.Replans-limit.MaxReplans), Supplements: max64(0, consumed.Supplements-limit.MaxSupplements), QualityRepairs: max64(0, consumed.QualityRepairs-limit.MaxQualityRepairs), InputTokens: max64(0, consumed.InputTokens-limit.MaxInputTokens), OutputTokens: max64(0, consumed.OutputTokens-limit.MaxOutputTokens), ElapsedMS: max64(0, consumed.ElapsedMS-limit.MaxElapsedMS)}
}

func elapsedBudgetExhausted(budget memoryBudgetState, now time.Time) bool {
	return now.Sub(budget.startedAt).Milliseconds() > budget.policy.MaxElapsedMS
}

func max64(left, right int64) int64 {
	if left > right {
		return left
	}
	return right
}

func (s *MemoryStore) taskRunIDsLocked(taskID string) map[string]bool {
	runIDs := make(map[string]bool)
	for _, run := range s.runs {
		if run.TaskID == taskID {
			runIDs[run.RunID] = true
		}
	}
	return runIDs
}

func (s *MemoryStore) PromoteWaiting(_ context.Context, now time.Time) ([]AgentRun, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	waiting := make([]AgentRun, 0)
	for _, run := range s.runs {
		if run.Status == RunWaitingCapacity {
			waiting = append(waiting, run)
		}
	}
	sort.Slice(waiting, func(i, j int) bool {
		left := s.lastScheduled[scheduleOwnerKey(waiting[i].TenantID, waiting[i].OwnerID)]
		right := s.lastScheduled[scheduleOwnerKey(waiting[j].TenantID, waiting[j].OwnerID)]
		if !left.Equal(right) {
			return left.Before(right)
		}
		if !waiting[i].CreatedAt.Equal(waiting[j].CreatedAt) {
			return waiting[i].CreatedAt.Before(waiting[j].CreatedAt)
		}
		return waiting[i].RunID < waiting[j].RunID
	})
	promoted := make([]AgentRun, 0)
	for _, candidate := range waiting {
		if s.runnableCountLocked() >= s.policy.MaxGlobalRunnable || s.ownerRunnableCountLocked(candidate.TenantID, candidate.OwnerID) >= s.policy.MaxRunnablePerOwner {
			continue
		}
		run := s.runs[candidate.RunID]
		run.Status = RunQueued
		run.QueueSlotAcquired = true
		run.UpdatedAt = now
		s.runs[run.RunID] = run
		s.lastScheduled[scheduleOwnerKey(run.TenantID, run.OwnerID)] = now
		s.addRunRequestOutboxLocked(run, "CAPACITY_AVAILABLE", now)
		promoted = append(promoted, run)
	}
	return promoted, nil
}

func (s *MemoryStore) ClaimOutbox(_ context.Context, limit int, now time.Time) ([]OutboxMessage, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if limit <= 0 {
		limit = 100
	}
	items := make([]OutboxMessage, 0, limit)
	for _, entry := range s.outbox {
		if len(items) >= limit || entry.published || entry.claimed || entry.message.AvailableAt.After(now) {
			continue
		}
		entry.claimed = true
		entry.message.Attempts++
		items = append(items, entry.message)
	}
	sort.Slice(items, func(i, j int) bool { return items[i].AvailableAt.Before(items[j].AvailableAt) })
	return items, nil
}

func (s *MemoryStore) MarkOutboxPublished(_ context.Context, messageID string, _ time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	entry, ok := s.outbox[messageID]
	if !ok {
		return ErrNotFound
	}
	entry.published = true
	entry.claimed = false
	return nil
}

func (s *MemoryStore) MarkOutboxFailed(_ context.Context, messageID string, nextAttemptAt time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	entry, ok := s.outbox[messageID]
	if !ok {
		return ErrNotFound
	}
	entry.claimed = false
	entry.message.AvailableAt = nextAttemptAt
	return nil
}

func validLease(run AgentRun, lease LeaseContext, now time.Time) bool {
	return (run.Status == RunRunning || run.Status == RunStopping) && run.LeaseID == lease.LeaseID && run.WorkerID == lease.WorkerID && run.FencingToken == lease.FencingToken && run.LeaseExpiresAt.After(now)
}

func stableHash(value string) string {
	digest := sha256.Sum256([]byte(value))
	return hex.EncodeToString(digest[:])
}

func leaseFromRun(run AgentRun) LeaseContext {
	return LeaseContext{RunID: run.RunID, LeaseID: run.LeaseID, WorkerID: run.WorkerID, FencingToken: run.FencingToken, ExpiresAt: run.LeaseExpiresAt}
}

func (s *MemoryStore) runnableCountLocked() int {
	count := 0
	for _, run := range s.runs {
		if run.QueueSlotAcquired && (run.Status == RunQueued || run.Status == RunRunning) {
			count++
		}
	}
	return count
}

func (s *MemoryStore) ownerRunnableCountLocked(tenantID, ownerID string) int {
	count := 0
	for _, run := range s.runs {
		if run.TenantID == tenantID && run.OwnerID == ownerID && run.QueueSlotAcquired && (run.Status == RunQueued || run.Status == RunRunning) {
			count++
		}
	}
	return count
}

func (s *MemoryStore) waitingCountLocked() int {
	count := 0
	for _, run := range s.runs {
		if run.Status == RunWaitingCapacity {
			count++
		}
	}
	return count
}

func scheduleOwnerKey(tenantID, ownerID string) string {
	return tenantID + "\x00" + ownerID
}

func (s *MemoryStore) addRunRequestOutboxLocked(run AgentRun, reason string, now time.Time) {
	messageID := "outbox-" + run.RunID
	s.outbox[messageID] = &memoryOutbox{message: OutboxMessage{
		MessageID: messageID, AggregateID: run.RunID, EventType: "agent.run.requested",
		Payload:     []byte(`{"run_id":"` + run.RunID + `","task_id":"` + run.TaskID + `","reason":"` + reason + `"}`),
		AvailableAt: now,
	}}
}

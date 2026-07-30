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
	mu                        sync.RWMutex
	policy                    QueuePolicy
	tasks                     map[string]Task
	runs                      map[string]AgentRun
	idempotency               map[string]memoryIdempotency
	outbox                    map[string]*memoryOutbox
	checkpoints               map[string]memoryCheckpoint
	attempts                  map[string]memoryAttempt
	evidence                  map[string]EvidenceRecord
	artifacts                 map[string]RunArtifact
	drafts                    map[string]memoryDraft
	events                    map[string][]TaskEvent
	dispatches                map[string]AgentDispatch
	lastScheduled             map[string]time.Time
	retryIdempotency          map[string]memoryRetryIdempotency
	publishes                 map[string]memoryPublish
	publishPreviewIdempotency map[string]string
	publishConfirmIdempotency map[string]string
	confirmationVersions      map[string]memoryConfirmationVersion
	confirmationDecisions     map[string]memoryConfirmationDecision
	revisionScopes            map[string]RevisionScope
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

type memoryRetryIdempotency struct {
	requestHash string
	run         AgentRun
}

type memoryPublish struct {
	record       PublishRecord
	tenantID     string
	ownerID      string
	draftKey     string
	content      []byte
	expiresAt    time.Time
	availableAt  time.Time
	claimWorker  string
	claimExpires time.Time
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
	return &MemoryStore{
		policy:                    policy,
		tasks:                     make(map[string]Task),
		runs:                      make(map[string]AgentRun),
		idempotency:               make(map[string]memoryIdempotency),
		outbox:                    make(map[string]*memoryOutbox),
		checkpoints:               make(map[string]memoryCheckpoint),
		attempts:                  make(map[string]memoryAttempt),
		evidence:                  make(map[string]EvidenceRecord),
		artifacts:                 make(map[string]RunArtifact),
		drafts:                    make(map[string]memoryDraft),
		events:                    make(map[string][]TaskEvent),
		dispatches:                make(map[string]AgentDispatch),
		lastScheduled:             make(map[string]time.Time),
		retryIdempotency:          make(map[string]memoryRetryIdempotency),
		publishes:                 make(map[string]memoryPublish),
		publishPreviewIdempotency: make(map[string]string),
		publishConfirmIdempotency: make(map[string]string),
		confirmationVersions:      make(map[string]memoryConfirmationVersion),
		confirmationDecisions:     make(map[string]memoryConfirmationDecision),
		revisionScopes:            make(map[string]RevisionScope),
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
	run := AgentRun{
		RunID: runID, TaskID: taskID, TenantID: tenantID, OwnerID: ownerID,
		Status: status, QueueSlotAcquired: status == RunQueued,
		CreatedAt: now, UpdatedAt: now,
	}
	s.tasks[taskID] = task
	s.runs[runID] = run
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
	run := AgentRun{
		RunID: runID, TaskID: taskID, TenantID: tenantID, OwnerID: ownerID,
		Status: status, QueueSlotAcquired: status == RunQueued, CreatedAt: now, UpdatedAt: now,
	}
	s.runs[runID] = run
	if status == RunQueued {
		s.lastScheduled[scheduleOwnerKey(tenantID, ownerID)] = now
		s.addRunRequestOutboxLocked(run, "USER_RETRY", now)
	}
	s.retryIdempotency[idempotencyIdentity] = memoryRetryIdempotency{requestHash: requestHash, run: run}
	return run, nil
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
	var resumeDraft *SubmittedDraftReceipt
	for _, draft := range s.drafts {
		if draft.runID == run.RunID && (resumeDraft == nil || draft.taskVersion > resumeDraft.TaskVersion) {
			resumeDraft = &SubmittedDraftReceipt{DraftKey: draft.draftKey, ContentHash: draft.patchHash, TaskVersion: draft.taskVersion, Content: append([]byte(nil), draft.patch...)}
		}
	}
	scope := s.revisionScopes[run.RunID]
	scope.ReopenedUnitKeys = append([]string(nil), scope.ReopenedUnitKeys...)
	scope.ImmutableUnitKeys = append([]string(nil), scope.ImmutableUnitKeys...)
	if scope.BaseDraftID != "" {
		base := s.draftByIDLocked(scope.BaseDraftID)
		resumeDraft = &SubmittedDraftReceipt{DraftKey: base.draftKey, ContentHash: base.patchHash, TaskVersion: base.taskVersion, Content: append([]byte(nil), base.patch...)}
	}
	return AgentRunInput{Run: run, TaskMessage: task.Message, WorkflowVersion: "agent-runtime.v1", Checkpoint: append([]byte(nil), checkpoint.payload...), CheckpointSequence: checkpoint.sequence, TaskVersion: task.Version, ResumeEvidence: evidence, ResumeArtifacts: artifacts, RevisionScope: scope, ResumeDraft: resumeDraft}, nil
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
	draft, ok := s.latestDraftLocked(taskID)
	if !ok {
		return PublishPreviewResult{}, ErrNotFound
	}
	for _, version := range s.confirmationVersions {
		if version.value.DraftID == draft.draftID {
			if version.value.ConfirmationStatus != "CONFIRMED" {
				return PublishPreviewResult{}, ErrInvalidRunStatus
			}
		}
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
			TaskVersion: task.Version, DraftVersion: draft.taskVersion, ContentHash: draft.patchHash,
			CreatedAt: now, UpdatedAt: now,
		},
		tenantID: tenantID, ownerID: ownerID, draftKey: draft.draftKey,
		content: append([]byte(nil), draft.patch...), expiresAt: expiresAt, availableAt: now,
	}
	s.publishes[publishID] = value
	s.publishPreviewIdempotency[idempotencyIdentity] = publishID
	payload, _ := json.Marshal(map[string]any{"publish_id": publishID, "content_hash": draft.patchHash})
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

package runcontrol

import (
	"context"
	"crypto/sha256"
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
)

const (
	MaxCheckpointBytes   = 1 << 20
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
	mu          sync.RWMutex
	policy      QueuePolicy
	tasks       map[string]Task
	runs        map[string]AgentRun
	idempotency map[string]memoryIdempotency
	outbox      map[string]*memoryOutbox
	checkpoints map[string]memoryCheckpoint
	attempts    map[string]string
	evidence    map[string]EvidenceItem
	drafts      map[string]memoryDraft
	events      map[string][]TaskEvent
	dispatches  map[string]AgentDispatch
}

type memoryCheckpoint struct {
	sequence    int64
	payload     []byte
	contentHash string
}

type memoryDraft struct {
	draftKey    string
	patchHash   string
	taskVersion int
}

func NewMemoryStore(policy QueuePolicy) *MemoryStore {
	return &MemoryStore{
		policy:      policy,
		tasks:       make(map[string]Task),
		runs:        make(map[string]AgentRun),
		idempotency: make(map[string]memoryIdempotency),
		outbox:      make(map[string]*memoryOutbox),
		checkpoints: make(map[string]memoryCheckpoint),
		attempts:    make(map[string]string),
		evidence:    make(map[string]EvidenceItem),
		drafts:      make(map[string]memoryDraft),
		events:      make(map[string][]TaskEvent),
		dispatches:  make(map[string]AgentDispatch),
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
	s.outbox["outbox-"+runID] = &memoryOutbox{message: OutboxMessage{
		MessageID: "outbox-" + runID, AggregateID: runID, EventType: "agent.run.requested",
		Payload: []byte(`{"run_id":"` + runID + `","task_id":"` + taskID + `","reason":"START_OR_RESUME"}`), AvailableAt: now,
	}}
	value := TaskWithRun{Task: task, Run: run}
	s.idempotency[key] = memoryIdempotency{requestHash: requestHash, value: value}
	return value, nil
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
	return AgentRunInput{Run: run, TaskMessage: task.Message, WorkflowVersion: "agent-runtime.v1", Checkpoint: append([]byte(nil), checkpoint.payload...), CheckpointSequence: checkpoint.sequence, TaskVersion: task.Version}, nil
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
		if existing != attempt.RequestHash {
			return "", ErrInvalidIdempotency
		}
		return "attempt-" + stableHash(key), nil
	}
	s.attempts[key] = attempt.RequestHash
	return "attempt-" + stableHash(key), nil
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
		s.evidence[key] = item
		accepted++
	}
	return accepted, nil
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
	task.Version++
	task.UpdatedAt = time.Now().UTC()
	s.tasks[task.TaskID] = task
	s.drafts[draftIdentity] = memoryDraft{draftKey: draftKey, patchHash: patchHash, taskVersion: task.Version}
	return DraftReceipt{TaskVersion: task.Version}, nil
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

func (s *MemoryStore) PromoteWaiting(_ context.Context, now time.Time) ([]AgentRun, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	waiting := make([]AgentRun, 0)
	for _, run := range s.runs {
		if run.Status == RunWaitingCapacity {
			waiting = append(waiting, run)
		}
	}
	sort.Slice(waiting, func(i, j int) bool { return waiting[i].CreatedAt.Before(waiting[j].CreatedAt) })
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

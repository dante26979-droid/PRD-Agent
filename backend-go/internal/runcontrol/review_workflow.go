package runcontrol

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/id"
)

// ReviewTaskStatus is the authoritative v4 review projection owned by the Go
// control plane. Legacy tasks keep their existing status values.
type ReviewTaskStatus string

const (
	ReviewDraft             ReviewTaskStatus = "DRAFT"
	ReviewOutlineReview     ReviewTaskStatus = "OUTLINE_REVIEW"
	ReviewUnitGenerating    ReviewTaskStatus = "UNIT_GENERATING"
	ReviewUnitReview        ReviewTaskStatus = "UNIT_REVIEW"
	ReviewFullReviewRunning ReviewTaskStatus = "FULL_REVIEW_RUNNING"
	ReviewFullReview        ReviewTaskStatus = "FULL_REVIEW"
	ReviewReviewable        ReviewTaskStatus = "REVIEWABLE"
)

type OutlineStatus string

const (
	OutlineDraft  OutlineStatus = "DRAFT"
	OutlineLocked OutlineStatus = "LOCKED"
)

// ReviewOutlineVersion is immutable after Status becomes LOCKED.
type ReviewOutlineVersion struct {
	OutlineID        string           `json:"outline_id"`
	OutlineVersionID string           `json:"outline_version_id"`
	TaskID           string           `json:"task_id"`
	Version          int64            `json:"version"`
	Status           OutlineStatus    `json:"status"`
	Candidate        OutlineCandidate `json:"candidate"`
	ContentHash      string           `json:"content_hash"`
	SourceRunID      string           `json:"source_run_id"`
	CreatedAt        time.Time        `json:"created_at"`
	LockedAt         *time.Time       `json:"locked_at,omitempty"`
}

type ReviewUnitStatus string

const (
	UnitPending   ReviewUnitStatus = "PENDING"
	UnitReviewing ReviewUnitStatus = "REVIEWING"
	UnitConfirmed ReviewUnitStatus = "CONFIRMED"
	UnitReopened  ReviewUnitStatus = "REOPENED"
)

type ReviewUnit struct {
	UnitID           string           `json:"unit_id"`
	TaskID           string           `json:"task_id"`
	OutlineVersionID string           `json:"outline_version_id"`
	UnitKey          string           `json:"unit_key"`
	Title            string           `json:"title"`
	Ordinal          int              `json:"ordinal"`
	NodeKeys         []string         `json:"node_keys"`
	DependsOn        []string         `json:"depends_on"`
	Status           ReviewUnitStatus `json:"status"`
}

type FullReviewReport struct {
	ReportID         string            `json:"report_id"`
	TaskID           string            `json:"task_id"`
	OutlineVersionID string            `json:"outline_version_id"`
	SourceRunID      string            `json:"source_run_id"`
	ContentHash      string            `json:"content_hash"`
	Disposition      string            `json:"disposition"`
	UnitHashes       map[string]string `json:"unit_hashes"`
	UnitHashSetHash  string            `json:"unit_hash_set_hash"`
	Payload          json.RawMessage   `json:"payload,omitempty"`
	CreatedAt        time.Time         `json:"created_at"`
}

type PublishDocument struct {
	WorkflowVersion WorkflowVersion `json:"workflow_version"`
	SourceVersionID string          `json:"source_version_id"`
	Content         []byte          `json:"content"`
	ContentHash     string          `json:"content_hash"`
	TaskVersion     int             `json:"task_version"`
}

type ReviewTransition struct {
	TaskVersion      int       `json:"task_version"`
	TaskStatus       string    `json:"task_status"`
	MaterializedKind string    `json:"materialized_kind,omitempty"`
	MaterializedID   string    `json:"materialized_id,omitempty"`
	CreatedRun       *AgentRun `json:"created_run,omitempty"`
	EventSequence    int64     `json:"event_sequence"`
}

type ConfirmOutlineCommand struct {
	TenantID            string
	OwnerID             string
	TaskID              string
	OutlineVersionID    string
	IdempotencyKey      string
	ExpectedTaskVersion int
}

type ReviewWorkflow interface {
	GetReviewOutline(ctx context.Context, tenantID, ownerID, taskID string) (ReviewOutlineVersion, error)
	ConfirmOutline(ctx context.Context, command ConfirmOutlineCommand) (ReviewTransition, error)
	ListReviewUnits(ctx context.Context, tenantID, ownerID, taskID string) ([]ReviewUnit, error)
}

type memoryReviewTransition struct {
	RequestHash string
	Transition  ReviewTransition
}

func (s *MemoryStore) GetReviewOutline(_ context.Context, tenantID, ownerID, taskID string) (ReviewOutlineVersion, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	task, ok := s.tasks[taskID]
	if !ok || task.TenantID != tenantID || task.OwnerID != ownerID {
		return ReviewOutlineVersion{}, ErrNotFound
	}
	outline, ok := s.reviewOutlines[taskID]
	if !ok {
		return ReviewOutlineVersion{}, ErrNotFound
	}
	return cloneReviewOutline(outline), nil
}

func (s *MemoryStore) ListReviewUnits(_ context.Context, tenantID, ownerID, taskID string) ([]ReviewUnit, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	task, ok := s.tasks[taskID]
	if !ok || task.TenantID != tenantID || task.OwnerID != ownerID {
		return nil, ErrNotFound
	}
	items := make([]ReviewUnit, 0, len(s.reviewUnits[taskID]))
	for _, value := range s.reviewUnits[taskID] {
		value.NodeKeys = append([]string(nil), value.NodeKeys...)
		value.DependsOn = append([]string(nil), value.DependsOn...)
		items = append(items, value)
	}
	sort.Slice(items, func(i, j int) bool {
		if items[i].Ordinal == items[j].Ordinal {
			return items[i].UnitKey < items[j].UnitKey
		}
		return items[i].Ordinal < items[j].Ordinal
	})
	return items, nil
}

func (s *MemoryStore) ConfirmOutline(_ context.Context, command ConfirmOutlineCommand) (ReviewTransition, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	identity := command.TenantID + "\x00" + command.OwnerID + "\x00" + command.IdempotencyKey
	requestHash := stableHash(strings.Join([]string{
		"CONFIRM_OUTLINE", command.TaskID, command.OutlineVersionID, strconv.Itoa(command.ExpectedTaskVersion),
	}, "\x00"))
	if replay, ok := s.reviewTransitions[identity]; ok {
		if replay.RequestHash != requestHash {
			return ReviewTransition{}, ErrInvalidIdempotency
		}
		return cloneReviewTransition(replay.Transition), nil
	}
	task, ok := s.tasks[command.TaskID]
	if !ok || task.TenantID != command.TenantID || task.OwnerID != command.OwnerID {
		return ReviewTransition{}, ErrNotFound
	}
	if command.IdempotencyKey == "" || task.Version != command.ExpectedTaskVersion {
		return ReviewTransition{}, ErrTaskVersionConflict
	}
	outline, ok := s.reviewOutlines[command.TaskID]
	if !ok || outline.OutlineVersionID != command.OutlineVersionID {
		return ReviewTransition{}, ErrNotFound
	}
	if outline.Status != OutlineDraft || task.Status != string(ReviewOutlineReview) {
		return ReviewTransition{}, ErrInvalidRunStatus
	}
	for _, run := range s.runs {
		if run.TaskID == task.TaskID && !run.Status.Terminal() {
			return ReviewTransition{}, ErrInvalidRunStatus
		}
	}
	now := time.Now().UTC()
	outline.Status = OutlineLocked
	outline.LockedAt = &now
	s.reviewOutlines[task.TaskID] = outline
	units := make(map[string]ReviewUnit, len(outline.Candidate.Units))
	for _, planned := range outline.Candidate.Units {
		units[planned.UnitKey] = ReviewUnit{
			UnitID: "review-unit-" + stableHash(task.TaskID+"\x00"+planned.UnitKey), TaskID: task.TaskID,
			OutlineVersionID: outline.OutlineVersionID, UnitKey: planned.UnitKey, Title: planned.Title,
			Ordinal: planned.Ordinal, NodeKeys: append([]string(nil), planned.NodeKeys...),
			DependsOn: append([]string(nil), planned.DependsOn...), Status: UnitPending,
		}
	}
	s.reviewUnits[task.TaskID] = units
	next, ok, err := nextReviewUnit(units)
	if err != nil || !ok {
		if err != nil {
			return ReviewTransition{}, err
		}
		return ReviewTransition{}, fmt.Errorf("%w: locked outline has no dependency-ready unit", ErrInvalidPayload)
	}
	task.Version++
	task.Status = string(ReviewUnitGenerating)
	task.UpdatedAt = now
	createdRun, err := s.createReviewRunLocked(task, outline, next, RunPurposeGenerateUnit, now)
	if err != nil {
		return ReviewTransition{}, err
	}
	s.tasks[task.TaskID] = task
	payload, _ := json.Marshal(map[string]any{"outline_version_id": outline.OutlineVersionID, "run_id": createdRun.RunID, "task_version": task.Version})
	event := s.appendTaskEventLocked(task.TenantID, task.OwnerID, task.TaskID, "review.outline_confirmed", payload)
	transition := ReviewTransition{
		TaskVersion: task.Version, TaskStatus: task.Status, MaterializedKind: "LOCKED_OUTLINE",
		MaterializedID: outline.OutlineVersionID, CreatedRun: &createdRun, EventSequence: event.Sequence,
	}
	s.reviewTransitions[identity] = memoryReviewTransition{RequestHash: requestHash, Transition: cloneReviewTransition(transition)}
	return transition, nil
}

func nextReviewUnit(units map[string]ReviewUnit) (ReviewUnit, bool, error) {
	planned := make([]PlannedConfirmationUnit, 0, len(units))
	for _, unit := range units {
		status := string(unit.Status)
		planned = append(planned, PlannedConfirmationUnit{UnitKey: unit.UnitKey, Ordinal: unit.Ordinal, DependsOn: unit.DependsOn, ConfirmationStatus: status})
	}
	next, ok, err := NextDependencyReadyUnit(planned)
	if err != nil || !ok {
		return ReviewUnit{}, ok, err
	}
	return units[next.UnitKey], true, nil
}

func (s *MemoryStore) createReviewRunLocked(task Task, outline ReviewOutlineVersion, unit ReviewUnit, purpose RunPurpose, now time.Time) (AgentRun, error) {
	status := RunQueued
	if s.runnableCountLocked() >= s.policy.MaxGlobalRunnable || s.ownerRunnableCountLocked(task.TenantID, task.OwnerID) >= s.policy.MaxRunnablePerOwner {
		status = RunWaitingCapacity
		if s.waitingCountLocked() >= s.policy.MaxWaitingRuns {
			return AgentRun{}, ErrCapacityExhausted
		}
	}
	runID, err := id.New("run")
	if err != nil {
		return AgentRun{}, err
	}
	run := AgentRun{
		RunID: runID, TaskID: task.TaskID, TenantID: task.TenantID, OwnerID: task.OwnerID,
		WorkflowVersion: WorkflowVersionV4, ExecutionLedgerVersion: ExecutionLedgerVersionV1,
		Status: status, QueueSlotAcquired: status == RunQueued, CreatedAt: now, UpdatedAt: now,
	}
	required := make([]string, 0)
	for _, node := range outline.Candidate.Nodes {
		if stringInSlice(node.NodeKey, unit.NodeKeys) {
			required = append(required, node.RequiredContent...)
		}
	}
	confirmed := s.confirmedUnitContextLocked(task.TaskID)
	scope, err := BuildUnitScope(UnitScope{
		Purpose: purpose, OutlineID: outline.OutlineID, OutlineVersion: outline.Version,
		OutlineHash: outline.ContentHash, CurrentUnitKey: unit.UnitKey, CurrentUnitTitle: unit.Title,
		CurrentUnitOrdinal: unit.Ordinal, SectionNodeKeys: unit.NodeKeys, DependencyUnitKeys: unit.DependsOn,
		ConfirmedContext: confirmed, ImmutableUnitKeys: confirmedUnitKeys(confirmed),
		RequirementRef:  BuildExecutionInputReference(task.Message, uniqueStrings(required, false), outline.Candidate),
		RequirementHash: hashText(task.Message),
	})
	if err != nil {
		return AgentRun{}, err
	}
	s.runs[runID] = run
	s.unitScopes[runID] = scope
	if assignment, ok := s.inheritedRolloutAssignmentLocked(task.TaskID); ok {
		s.rolloutAssignments[runID] = assignment
	}
	s.initializeBudgetLocked(run, now)
	if status == RunQueued {
		s.lastScheduled[scheduleOwnerKey(task.TenantID, task.OwnerID)] = now
		s.addRunRequestOutboxLocked(run, "REVIEW_TRANSITION", now)
	}
	return run, nil
}

func (s *MemoryStore) createFullReviewRunLocked(task Task, outline ReviewOutlineVersion, now time.Time) (AgentRun, error) {
	status := RunQueued
	if s.runnableCountLocked() >= s.policy.MaxGlobalRunnable || s.ownerRunnableCountLocked(task.TenantID, task.OwnerID) >= s.policy.MaxRunnablePerOwner {
		status = RunWaitingCapacity
		if s.waitingCountLocked() >= s.policy.MaxWaitingRuns {
			return AgentRun{}, ErrCapacityExhausted
		}
	}
	runID, err := id.New("run")
	if err != nil {
		return AgentRun{}, err
	}
	confirmed := s.confirmedUnitContextLocked(task.TaskID)
	scope, err := BuildUnitScope(UnitScope{
		Purpose: RunPurposeFullReview, OutlineID: outline.OutlineID, OutlineVersion: outline.Version,
		OutlineHash: outline.ContentHash, ConfirmedContext: confirmed,
		ImmutableUnitKeys: confirmedUnitKeys(confirmed),
		RequirementRef:    BuildExecutionInputReference(task.Message, nil, outline.Candidate), RequirementHash: hashText(task.Message),
	})
	if err != nil {
		return AgentRun{}, err
	}
	run := AgentRun{
		RunID: runID, TaskID: task.TaskID, TenantID: task.TenantID, OwnerID: task.OwnerID,
		WorkflowVersion: WorkflowVersionV4, ExecutionLedgerVersion: ExecutionLedgerVersionV1,
		Status: status, QueueSlotAcquired: status == RunQueued, CreatedAt: now, UpdatedAt: now,
	}
	s.runs[runID] = run
	s.unitScopes[runID] = scope
	if assignment, ok := s.inheritedRolloutAssignmentLocked(task.TaskID); ok {
		s.rolloutAssignments[runID] = assignment
	}
	s.initializeBudgetLocked(run, now)
	if status == RunQueued {
		s.lastScheduled[scheduleOwnerKey(task.TenantID, task.OwnerID)] = now
		s.addRunRequestOutboxLocked(run, "FULL_REVIEW_READY", now)
	}
	return run, nil
}

func (s *MemoryStore) createRevisionReviewRunLocked(task Task, outline ReviewOutlineVersion, unit ReviewUnit, base ConfirmationUnitVersion, feedback string, now time.Time) (AgentRun, error) {
	status := RunQueued
	if s.runnableCountLocked() >= s.policy.MaxGlobalRunnable || s.ownerRunnableCountLocked(task.TenantID, task.OwnerID) >= s.policy.MaxRunnablePerOwner {
		status = RunWaitingCapacity
		if s.waitingCountLocked() >= s.policy.MaxWaitingRuns {
			return AgentRun{}, ErrCapacityExhausted
		}
	}
	runID, err := id.New("run")
	if err != nil {
		return AgentRun{}, err
	}
	confirmed := s.confirmedUnitContextLocked(task.TaskID)
	confirmed = append(confirmed, ConfirmedUnitContext{
		UnitKey: base.UnitKey, UnitVersion: base.UnitVersionNo, ContentHash: base.ContentHash,
		Summary: base.Title, Markdown: base.Markdown,
	})
	sort.Slice(confirmed, func(i, j int) bool { return confirmed[i].UnitKey < confirmed[j].UnitKey })
	required := make([]string, 0)
	for _, node := range outline.Candidate.Nodes {
		if stringInSlice(node.NodeKey, unit.NodeKeys) {
			required = append(required, node.RequiredContent...)
		}
	}
	immutable := make([]string, 0)
	for _, item := range confirmed {
		if item.UnitKey != unit.UnitKey {
			immutable = append(immutable, item.UnitKey)
		}
	}
	scope, err := BuildUnitScope(UnitScope{
		Purpose: RunPurposeReviseUnit, OutlineID: outline.OutlineID, OutlineVersion: outline.Version,
		OutlineHash: outline.ContentHash, CurrentUnitKey: unit.UnitKey, CurrentUnitTitle: unit.Title,
		CurrentUnitOrdinal: unit.Ordinal, SectionNodeKeys: unit.NodeKeys, DependencyUnitKeys: unit.DependsOn,
		ConfirmedContext: confirmed, ReopenedUnitKeys: []string{unit.UnitKey}, ImmutableUnitKeys: immutable,
		RequirementRef:  BuildExecutionInputReference(task.Message, uniqueStrings(required, false), outline.Candidate),
		RequirementHash: hashText(task.Message),
		BaseUnitHash:    base.ContentHash, UserFeedback: feedback,
	})
	if err != nil {
		return AgentRun{}, err
	}
	run := AgentRun{
		RunID: runID, TaskID: task.TaskID, TenantID: task.TenantID, OwnerID: task.OwnerID,
		WorkflowVersion: WorkflowVersionV4, ExecutionLedgerVersion: ExecutionLedgerVersionV1,
		Status: status, QueueSlotAcquired: status == RunQueued, CreatedAt: now, UpdatedAt: now,
	}
	s.runs[runID] = run
	s.unitScopes[runID] = scope
	if assignment, ok := s.inheritedRolloutAssignmentLocked(task.TaskID); ok {
		s.rolloutAssignments[runID] = assignment
	}
	s.initializeBudgetLocked(run, now)
	if status == RunQueued {
		s.lastScheduled[scheduleOwnerKey(task.TenantID, task.OwnerID)] = now
		s.addRunRequestOutboxLocked(run, "UNIT_REOPENED", now)
	}
	return run, nil
}

func (s *MemoryStore) confirmedUnitContextLocked(taskID string) []ConfirmedUnitContext {
	latest := make(map[string]ConfirmationUnitVersion)
	for _, item := range s.confirmationVersions {
		if item.taskID != taskID || item.value.ConfirmationStatus != string(UnitConfirmed) {
			continue
		}
		current, ok := latest[item.value.UnitKey]
		if !ok || item.value.UnitVersionNo > current.UnitVersionNo {
			latest[item.value.UnitKey] = item.value
		}
	}
	items := make([]ConfirmedUnitContext, 0, len(latest))
	for _, item := range latest {
		items = append(items, ConfirmedUnitContext{
			UnitKey: item.UnitKey, UnitVersion: item.UnitVersionNo, ContentHash: item.ContentHash,
			Summary: item.Title, Markdown: item.Markdown,
		})
	}
	sort.Slice(items, func(i, j int) bool {
		left, right := s.reviewUnits[taskID][items[i].UnitKey], s.reviewUnits[taskID][items[j].UnitKey]
		if left.Ordinal == right.Ordinal {
			return items[i].UnitKey < items[j].UnitKey
		}
		return left.Ordinal < right.Ordinal
	})
	return items
}

func confirmedUnitKeys(items []ConfirmedUnitContext) []string {
	keys := make([]string, 0, len(items))
	for _, item := range items {
		keys = append(keys, item.UnitKey)
	}
	return keys
}

func sameConfirmedHashSet(left, right []ConfirmedUnitContext) bool {
	if len(left) != len(right) {
		return false
	}
	hashes := make(map[string]string, len(left))
	for _, item := range left {
		hashes[item.UnitKey] = bareSHA256(item.ContentHash)
	}
	for _, item := range right {
		if hashes[item.UnitKey] != bareSHA256(item.ContentHash) {
			return false
		}
	}
	return true
}

func cloneStringMap(value map[string]string) map[string]string {
	result := make(map[string]string, len(value))
	for key, item := range value {
		result[key] = item
	}
	return result
}

func BuildExecutionInputReference(requirement string, required []string, outline OutlineCandidate) string {
	payload, _ := json.Marshal(map[string]any{
		"requirement_brief": map[string]any{
			"text": requirement, "required_content": nonNilStrings(required),
		},
		"locked_outline": outline,
	})
	return "execution-input-json:" + string(payload)
}

func (s *MemoryStore) buildV4PublishDocumentLocked(task Task) (PublishDocument, error) {
	if task.Status != string(ReviewReviewable) {
		return PublishDocument{}, ErrInvalidRunStatus
	}
	outline, ok := s.reviewOutlines[task.TaskID]
	if !ok || outline.Status != OutlineLocked {
		return PublishDocument{}, ErrInvalidRunStatus
	}
	report, ok := s.fullReviewReports[task.TaskID]
	if !ok || report.Disposition != "PASSED" || report.OutlineVersionID != outline.OutlineVersionID {
		return PublishDocument{}, ErrInvalidRunStatus
	}
	latest := make(map[string]ConfirmationUnitVersion)
	for _, item := range s.confirmationVersions {
		if item.taskID != task.TaskID || item.value.OutlineVersionID != outline.OutlineVersionID {
			continue
		}
		current, exists := latest[item.value.UnitKey]
		if !exists || item.value.UnitVersionNo > current.UnitVersionNo {
			latest[item.value.UnitKey] = item.value
		}
	}
	parts := []string{"# " + strings.TrimSpace(outline.Candidate.Title)}
	for _, planned := range outline.Candidate.Units {
		unit, exists := latest[planned.UnitKey]
		if !exists || unit.ConfirmationStatus != string(UnitConfirmed) || bareSHA256(report.UnitHashes[planned.UnitKey]) != bareSHA256(unit.ContentHash) {
			return PublishDocument{}, ErrInvalidRunStatus
		}
		parts = append(parts, strings.TrimSpace(unit.Markdown))
	}
	if len(latest) != len(outline.Candidate.Units) || len(report.UnitHashes) != len(latest) {
		return PublishDocument{}, ErrInvalidRunStatus
	}
	content := []byte(strings.Join(parts, "\n\n"))
	return PublishDocument{
		WorkflowVersion: WorkflowVersionV4,
		SourceVersionID: outline.OutlineVersionID + ":" + report.ReportID,
		Content:         content, ContentHash: hashBytesHex(content), TaskVersion: task.Version,
	}, nil
}

var _ ReviewWorkflow = (*MemoryStore)(nil)

func cloneReviewTransition(value ReviewTransition) ReviewTransition {
	if value.CreatedRun != nil {
		run := *value.CreatedRun
		value.CreatedRun = &run
	}
	return value
}

func cloneReviewOutline(value ReviewOutlineVersion) ReviewOutlineVersion {
	value.Candidate.Nodes = append([]OutlineNode(nil), value.Candidate.Nodes...)
	value.Candidate.Units = append([]OutlineUnit(nil), value.Candidate.Units...)
	for index := range value.Candidate.Nodes {
		value.Candidate.Nodes[index].Questions = append([]string(nil), value.Candidate.Nodes[index].Questions...)
		value.Candidate.Nodes[index].RequiredContent = append([]string(nil), value.Candidate.Nodes[index].RequiredContent...)
	}
	for index := range value.Candidate.Units {
		value.Candidate.Units[index].NodeKeys = append([]string(nil), value.Candidate.Units[index].NodeKeys...)
		value.Candidate.Units[index].DependsOn = append([]string(nil), value.Candidate.Units[index].DependsOn...)
	}
	if value.LockedAt != nil {
		locked := *value.LockedAt
		value.LockedAt = &locked
	}
	return value
}

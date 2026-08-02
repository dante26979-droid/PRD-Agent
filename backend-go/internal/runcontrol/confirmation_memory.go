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

func (s *MemoryStore) ListConfirmationUnits(_ context.Context, tenantID, ownerID, taskID string) ([]ConfirmationUnitVersion, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	task, ok := s.tasks[taskID]
	if !ok || task.TenantID != tenantID || task.OwnerID != ownerID {
		return nil, ErrNotFound
	}
	draftID := s.latestDraftIDLocked(taskID)
	_, isV4 := s.reviewOutlines[taskID]
	items := make([]ConfirmationUnitVersion, 0)
	latest := make(map[string]ConfirmationUnitVersion)
	for _, version := range s.confirmationVersions {
		if version.taskID != taskID {
			continue
		}
		if isV4 {
			current, ok := latest[version.value.UnitKey]
			if !ok || version.value.UnitVersionNo > current.UnitVersionNo {
				latest[version.value.UnitKey] = version.value
			}
		} else if version.value.DraftID == draftID {
			items = append(items, cloneConfirmationUnit(version.value))
		}
	}
	if isV4 {
		for _, version := range latest {
			items = append(items, cloneConfirmationUnit(version))
		}
	}
	sort.Slice(items, func(i, j int) bool { return items[i].Ordinal < items[j].Ordinal })
	return items, nil
}

func (s *MemoryStore) ConfirmConfirmationUnit(_ context.Context, tenantID, ownerID, taskID, unitVersionID, idempotencyKey string, expectedTaskVersion int) (ConfirmationMutationResult, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	requestHash := stableHash(strings.Join([]string{"CONFIRM", taskID, unitVersionID, strconv.Itoa(expectedTaskVersion)}, "\x00"))
	if replay, ok := s.confirmationDecisions[tenantID+"\x00"+ownerID+"\x00"+idempotencyKey]; ok {
		if replay.requestHash != requestHash {
			return ConfirmationMutationResult{}, ErrInvalidIdempotency
		}
		return replay.result, nil
	}
	if _, isV4 := s.reviewOutlines[taskID]; isV4 {
		return s.confirmReviewUnitLocked(tenantID, ownerID, taskID, unitVersionID, idempotencyKey, requestHash, expectedTaskVersion)
	}
	task, version, err := s.validateConfirmationMutationLocked(tenantID, ownerID, taskID, unitVersionID, expectedTaskVersion)
	if err != nil {
		return ConfirmationMutationResult{}, err
	}
	for _, dependency := range version.value.DependsOn {
		dependencyVersion := s.currentConfirmationByKeyLocked(taskID, version.value.DraftID, dependency)
		if dependencyVersion == nil || dependencyVersion.value.ConfirmationStatus != "CONFIRMED" {
			return ConfirmationMutationResult{}, ErrInvalidRunStatus
		}
	}
	now := time.Now().UTC()
	task.Version++
	version.value.ConfirmationStatus = "CONFIRMED"
	s.confirmationVersions[unitVersionID] = version
	if s.allCurrentUnitsConfirmedLocked(taskID, version.value.DraftID) {
		task.Status = "REVIEWABLE"
	}
	task.UpdatedAt = now
	s.tasks[taskID] = task
	decisionID, err := id.New("decision")
	if err != nil {
		return ConfirmationMutationResult{}, err
	}
	result := ConfirmationMutationResult{
		DecisionID:  decisionID,
		TaskVersion: task.Version,
		Unit:        cloneConfirmationUnit(version.value),
	}
	s.confirmationDecisions[tenantID+"\x00"+ownerID+"\x00"+idempotencyKey] = memoryConfirmationDecision{
		requestHash: requestHash, result: result, decision: "CONFIRMED",
	}
	payload, _ := json.Marshal(map[string]any{"decision_id": decisionID, "unit_version_id": unitVersionID, "decision": "CONFIRMED", "task_version": task.Version})
	s.appendTaskEventLocked(tenantID, ownerID, taskID, "confirmation_unit.confirmed", payload)
	return result, nil
}

func (s *MemoryStore) confirmReviewUnitLocked(tenantID, ownerID, taskID, unitVersionID, idempotencyKey, requestHash string, expectedTaskVersion int) (ConfirmationMutationResult, error) {
	task, ok := s.tasks[taskID]
	if !ok || task.TenantID != tenantID || task.OwnerID != ownerID {
		return ConfirmationMutationResult{}, ErrNotFound
	}
	if task.Version != expectedTaskVersion {
		return ConfirmationMutationResult{}, ErrTaskVersionConflict
	}
	version, ok := s.confirmationVersions[unitVersionID]
	if !ok || version.taskID != taskID || version.value.OutlineVersionID == "" {
		return ConfirmationMutationResult{}, ErrNotFound
	}
	unit, ok := s.reviewUnits[taskID][version.value.UnitKey]
	if !ok || unit.Status != UnitReviewing || version.value.ConfirmationStatus != string(UnitReviewing) {
		return ConfirmationMutationResult{}, ErrInvalidRunStatus
	}
	for _, dependency := range unit.DependsOn {
		if s.reviewUnits[taskID][dependency].Status != UnitConfirmed {
			return ConfirmationMutationResult{}, ErrInvalidRunStatus
		}
	}
	for _, run := range s.runs {
		if run.TaskID == taskID && !run.Status.Terminal() {
			return ConfirmationMutationResult{}, ErrInvalidRunStatus
		}
	}
	now := time.Now().UTC()
	version.value.ConfirmationStatus = string(UnitConfirmed)
	s.confirmationVersions[unitVersionID] = version
	unit.Status = UnitConfirmed
	s.reviewUnits[taskID][unit.UnitKey] = unit
	task.Version++
	outline := s.reviewOutlines[taskID]
	var createdRun *AgentRun
	next, hasNext, err := nextReviewUnit(s.reviewUnits[taskID])
	if err != nil {
		return ConfirmationMutationResult{}, err
	}
	if hasNext {
		task.Status = string(ReviewUnitGenerating)
		run, createErr := s.createReviewRunLocked(task, outline, next, RunPurposeGenerateUnit, now)
		if createErr != nil {
			return ConfirmationMutationResult{}, createErr
		}
		createdRun = &run
	} else {
		for _, candidate := range s.reviewUnits[taskID] {
			if candidate.Status != UnitConfirmed {
				return ConfirmationMutationResult{}, fmt.Errorf("%w: no dependency-ready unit while review remains incomplete", ErrInvalidRunStatus)
			}
		}
		task.Status = string(ReviewFullReviewRunning)
		run, createErr := s.createFullReviewRunLocked(task, outline, now)
		if createErr != nil {
			return ConfirmationMutationResult{}, createErr
		}
		createdRun = &run
	}
	task.UpdatedAt = now
	s.tasks[taskID] = task
	decisionID, err := id.New("decision")
	if err != nil {
		return ConfirmationMutationResult{}, err
	}
	result := ConfirmationMutationResult{DecisionID: decisionID, TaskVersion: task.Version, Unit: cloneConfirmationUnit(version.value), Run: createdRun}
	identity := tenantID + "\x00" + ownerID + "\x00" + idempotencyKey
	s.confirmationDecisions[identity] = memoryConfirmationDecision{requestHash: requestHash, result: result, decision: "CONFIRMED"}
	payload, _ := json.Marshal(map[string]any{"decision_id": decisionID, "unit_version_id": unitVersionID, "decision": "CONFIRMED", "task_version": task.Version, "next_run_id": createdRun.RunID})
	s.appendTaskEventLocked(tenantID, ownerID, taskID, "review.unit_confirmed", payload)
	return result, nil
}

func (s *MemoryStore) ReopenConfirmationUnit(_ context.Context, tenantID, ownerID, taskID, unitVersionID, feedback, idempotencyKey string, expectedTaskVersion int) (ConfirmationMutationResult, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	feedback = strings.TrimSpace(feedback)
	if feedback == "" {
		return ConfirmationMutationResult{}, ErrInvalidPayload
	}
	requestHash := stableHash(strings.Join([]string{"REOPEN", taskID, unitVersionID, feedback, strconv.Itoa(expectedTaskVersion)}, "\x00"))
	identity := tenantID + "\x00" + ownerID + "\x00" + idempotencyKey
	if replay, ok := s.confirmationDecisions[identity]; ok {
		if replay.requestHash != requestHash {
			return ConfirmationMutationResult{}, ErrInvalidIdempotency
		}
		return replay.result, nil
	}
	if _, isV4 := s.reviewOutlines[taskID]; isV4 {
		return s.reopenReviewUnitLocked(tenantID, ownerID, taskID, unitVersionID, feedback, idempotencyKey, requestHash, expectedTaskVersion)
	}
	task, version, err := s.validateConfirmationMutationLocked(tenantID, ownerID, taskID, unitVersionID, expectedTaskVersion)
	if err != nil {
		return ConfirmationMutationResult{}, err
	}
	if version.value.ConfirmationStatus != "CONFIRMED" {
		return ConfirmationMutationResult{}, ErrInvalidRunStatus
	}
	for _, candidate := range s.confirmationVersions {
		if candidate.value.DraftID != version.value.DraftID || candidate.value.ConfirmationStatus != "CONFIRMED" {
			continue
		}
		for _, dependency := range candidate.value.DependsOn {
			if dependency == version.value.UnitKey {
				return ConfirmationMutationResult{}, ErrInvalidRunStatus
			}
		}
	}
	for _, run := range s.runs {
		if run.TaskID == taskID && !run.Status.Terminal() {
			return ConfirmationMutationResult{}, ErrInvalidRunStatus
		}
	}
	status := RunQueued
	admitted := true
	if s.runnableCountLocked() >= s.policy.MaxGlobalRunnable || s.ownerRunnableCountLocked(tenantID, ownerID) >= s.policy.MaxRunnablePerOwner {
		status = RunWaitingCapacity
		admitted = false
		if s.waitingCountLocked() >= s.policy.MaxWaitingRuns {
			return ConfirmationMutationResult{}, ErrCapacityExhausted
		}
	}
	runID, err := id.New("run")
	if err != nil {
		return ConfirmationMutationResult{}, err
	}
	decisionID, err := id.New("decision")
	if err != nil {
		return ConfirmationMutationResult{}, err
	}
	now := time.Now().UTC()
	task.Version++
	task.Status = "DRAFT"
	task.UpdatedAt = now
	s.tasks[taskID] = task
	version.value.ConfirmationStatus = "REOPENED"
	s.confirmationVersions[unitVersionID] = version
	workflowVersion := s.policy.DefaultWorkflowVersion
	inheritedAssignment, hasInheritedAssignment := s.inheritedRolloutAssignmentLocked(taskID)
	if hasInheritedAssignment {
		workflowVersion = inheritedAssignment.AuthoritativeWorkflowVersion
	}
	run := AgentRun{
		RunID: runID, TaskID: taskID, TenantID: tenantID, OwnerID: ownerID,
		WorkflowVersion:        workflowVersion,
		ExecutionLedgerVersion: ledgerVersionForPolicy(s.policy),
		Status:                 status, QueueSlotAcquired: admitted, CreatedAt: now, UpdatedAt: now,
	}
	s.runs[runID] = run
	if hasInheritedAssignment {
		s.rolloutAssignments[runID] = inheritedAssignment
	}
	s.initializeBudgetLocked(run, now)
	immutable := make([]string, 0)
	for _, candidate := range s.confirmationVersions {
		if candidate.value.DraftID == version.value.DraftID && candidate.value.ConfirmationStatus == "CONFIRMED" {
			immutable = append(immutable, candidate.value.UnitKey)
		}
	}
	sort.Strings(immutable)
	baseDraft := s.draftByIDLocked(version.value.DraftID)
	s.revisionScopes[runID] = RevisionScope{
		BaseDraftID: version.value.DraftID, BaseDraftHash: baseDraft.patchHash,
		ReopenedUnitKeys:  []string{version.value.UnitKey},
		ImmutableUnitKeys: immutable, UserFeedback: feedback,
	}
	if admitted {
		s.lastScheduled[scheduleOwnerKey(tenantID, ownerID)] = now
		s.addRunRequestOutboxLocked(run, "CONFIRMATION_REOPENED", now)
	}
	result := ConfirmationMutationResult{
		DecisionID: decisionID, TaskVersion: task.Version,
		Unit: cloneConfirmationUnit(version.value), Run: &run,
	}
	s.confirmationDecisions[identity] = memoryConfirmationDecision{
		requestHash: requestHash, result: result, decision: "REOPENED",
	}
	payload, _ := json.Marshal(map[string]any{"decision_id": decisionID, "unit_version_id": unitVersionID, "decision": "REOPENED", "run_id": runID, "task_version": task.Version})
	s.appendTaskEventLocked(tenantID, ownerID, taskID, "confirmation_unit.reopened", payload)
	return result, nil
}

func (s *MemoryStore) reopenReviewUnitLocked(tenantID, ownerID, taskID, unitVersionID, feedback, idempotencyKey, requestHash string, expectedTaskVersion int) (ConfirmationMutationResult, error) {
	task, ok := s.tasks[taskID]
	if !ok || task.TenantID != tenantID || task.OwnerID != ownerID {
		return ConfirmationMutationResult{}, ErrNotFound
	}
	if task.Version != expectedTaskVersion {
		return ConfirmationMutationResult{}, ErrTaskVersionConflict
	}
	version, ok := s.confirmationVersions[unitVersionID]
	if !ok || version.taskID != taskID || version.value.OutlineVersionID == "" {
		return ConfirmationMutationResult{}, ErrNotFound
	}
	unit, ok := s.reviewUnits[taskID][version.value.UnitKey]
	if !ok || unit.Status != UnitConfirmed || version.value.ConfirmationStatus != string(UnitConfirmed) {
		return ConfirmationMutationResult{}, ErrInvalidRunStatus
	}
	for _, candidate := range s.reviewUnits[taskID] {
		if candidate.Status != UnitConfirmed {
			continue
		}
		for _, dependency := range candidate.DependsOn {
			if dependency == unit.UnitKey {
				return ConfirmationMutationResult{}, ErrInvalidRunStatus
			}
		}
	}
	for _, run := range s.runs {
		if run.TaskID == taskID && !run.Status.Terminal() {
			return ConfirmationMutationResult{}, ErrInvalidRunStatus
		}
	}
	now := time.Now().UTC()
	version.value.ConfirmationStatus = string(UnitReopened)
	s.confirmationVersions[unitVersionID] = version
	unit.Status = UnitReopened
	s.reviewUnits[taskID][unit.UnitKey] = unit
	delete(s.fullReviewReports, taskID)
	task.Version++
	task.Status = string(ReviewUnitGenerating)
	task.UpdatedAt = now
	run, err := s.createRevisionReviewRunLocked(task, s.reviewOutlines[taskID], unit, version.value, feedback, now)
	if err != nil {
		return ConfirmationMutationResult{}, err
	}
	s.tasks[taskID] = task
	decisionID, err := id.New("decision")
	if err != nil {
		return ConfirmationMutationResult{}, err
	}
	result := ConfirmationMutationResult{DecisionID: decisionID, TaskVersion: task.Version, Unit: cloneConfirmationUnit(version.value), Run: &run}
	identity := tenantID + "\x00" + ownerID + "\x00" + idempotencyKey
	s.confirmationDecisions[identity] = memoryConfirmationDecision{requestHash: requestHash, result: result, decision: "REOPENED"}
	payload, _ := json.Marshal(map[string]any{"decision_id": decisionID, "unit_version_id": unitVersionID, "decision": "REOPENED", "run_id": run.RunID, "task_version": task.Version})
	s.appendTaskEventLocked(tenantID, ownerID, taskID, "review.unit_reopened", payload)
	return result, nil
}

func (s *MemoryStore) validateConfirmationMutationLocked(tenantID, ownerID, taskID, unitVersionID string, expectedTaskVersion int) (Task, memoryConfirmationVersion, error) {
	task, ok := s.tasks[taskID]
	if !ok || task.TenantID != tenantID || task.OwnerID != ownerID {
		return Task{}, memoryConfirmationVersion{}, ErrNotFound
	}
	if task.Version != expectedTaskVersion {
		return Task{}, memoryConfirmationVersion{}, ErrTaskVersionConflict
	}
	version, ok := s.confirmationVersions[unitVersionID]
	if !ok || version.taskID != taskID || version.value.DraftID != s.latestDraftIDLocked(taskID) {
		return Task{}, memoryConfirmationVersion{}, ErrNotFound
	}
	return task, version, nil
}

func (s *MemoryStore) latestDraftIDLocked(taskID string) string {
	var latest memoryDraft
	for identity, draft := range s.drafts {
		if strings.HasPrefix(identity, taskID+"\x00") && draft.taskVersion > latest.taskVersion {
			latest = draft
		}
	}
	return latest.draftID
}

func (s *MemoryStore) draftByIDLocked(draftID string) memoryDraft {
	for _, draft := range s.drafts {
		if draft.draftID == draftID {
			return draft
		}
	}
	return memoryDraft{}
}

func (s *MemoryStore) currentConfirmationByKeyLocked(taskID, draftID, unitKey string) *memoryConfirmationVersion {
	for _, version := range s.confirmationVersions {
		if version.taskID == taskID && version.value.DraftID == draftID && version.value.UnitKey == unitKey {
			copy := version
			return &copy
		}
	}
	return nil
}

func (s *MemoryStore) allCurrentUnitsConfirmedLocked(taskID, draftID string) bool {
	count := 0
	for _, version := range s.confirmationVersions {
		if version.taskID != taskID || version.value.DraftID != draftID {
			continue
		}
		count++
		if version.value.ConfirmationStatus != "CONFIRMED" {
			return false
		}
	}
	return count > 0
}

func cloneConfirmationUnit(value ConfirmationUnitVersion) ConfirmationUnitVersion {
	value.ClaimIDs = append([]string(nil), value.ClaimIDs...)
	value.UnknownIDs = append([]string(nil), value.UnknownIDs...)
	value.DependsOn = append([]string(nil), value.DependsOn...)
	return value
}

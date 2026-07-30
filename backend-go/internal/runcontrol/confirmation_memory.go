package runcontrol

import (
	"context"
	"encoding/json"
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
	items := make([]ConfirmationUnitVersion, 0)
	for _, version := range s.confirmationVersions {
		if version.taskID == taskID && version.value.DraftID == draftID {
			items = append(items, cloneConfirmationUnit(version.value))
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
	run := AgentRun{
		RunID: runID, TaskID: taskID, TenantID: tenantID, OwnerID: ownerID,
		Status: status, QueueSlotAcquired: admitted, CreatedAt: now, UpdatedAt: now,
	}
	s.runs[runID] = run
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

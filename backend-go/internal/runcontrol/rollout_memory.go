package runcontrol

import (
	"context"
	"time"
)

func (s *MemoryStore) SaveRolloutAssignment(_ context.Context, runID string, assignment RolloutAssignment) error {
	assignment = assignment.WithHash()
	s.mu.Lock()
	defer s.mu.Unlock()
	run, ok := s.runs[runID]
	if !ok {
		return ErrNotFound
	}
	if assignment.PolicyVersion == "" || !assignment.AuthoritativeWorkflowVersion.Supported() {
		return ErrInvalidPayload
	}
	if existing, exists := s.rolloutAssignments[runID]; exists {
		if existing != assignment {
			return ErrInvalidIdempotency
		}
		return nil
	}
	if run.WorkflowVersion != assignment.AuthoritativeWorkflowVersion {
		return ErrInvalidPayload
	}
	s.rolloutAssignments[runID] = assignment
	return nil
}

func (s *MemoryStore) GetRolloutAssignment(_ context.Context, runID string) (RolloutAssignment, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	assignment, ok := s.rolloutAssignments[runID]
	if !ok {
		return RolloutAssignment{}, ErrNotFound
	}
	return assignment, nil
}

func (s *MemoryStore) inheritedRolloutAssignmentLocked(taskID string) (RolloutAssignment, bool) {
	var selected RolloutAssignment
	var selectedAt time.Time
	found := false
	for runID, assignment := range s.rolloutAssignments {
		run, ok := s.runs[runID]
		if !ok || run.TaskID != taskID || (found && !run.CreatedAt.After(selectedAt)) {
			continue
		}
		selected, selectedAt, found = assignment, run.CreatedAt, true
	}
	return selected, found
}

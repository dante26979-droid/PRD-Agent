package runcontrol

import (
	"context"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/id"
)

func (s *MemoryStore) ApplyGateCommand(_ context.Context, command RolloutGateOperatorCommand) (RolloutGateOperatorResult, error) {
	requestHash, err := command.RequestHash()
	if err != nil {
		return RolloutGateOperatorResult{}, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if existingHash, ok := s.rolloutCommandHashes[command.CommandID]; ok {
		if existingHash != requestHash {
			return RolloutGateOperatorResult{}, ErrInvalidIdempotency
		}
		existing, ok := s.rolloutGateResults[command.CommandID]
		if !ok {
			return RolloutGateOperatorResult{}, ErrInvalidIdempotency
		}
		existing.Replayed = true
		return existing, nil
	}
	state := s.rolloutStageState
	if state.PolicyVersion == "" && s.policy.RolloutPolicy != nil {
		state.PolicyVersion = s.policy.RolloutPolicy.PolicyVersion
	}
	if state.Version != command.ExpectedVersion {
		return RolloutGateOperatorResult{}, ErrTaskVersionConflict
	}
	decision := EvaluateRolloutGate(command.Manifest, command.Report)
	decisionID, err := id.New("rollout-gate")
	if err != nil {
		return RolloutGateOperatorResult{}, err
	}
	if command.Now.IsZero() {
		command.Now = time.Now().UTC()
	}
	record := GateDecisionRecord{
		DecisionID: decisionID, Candidate: command.Report.Candidate, Baseline: command.Report.Baseline,
		DatasetHash: command.Report.DatasetHash, ConfigHash: command.Report.ConfigHash,
		ReportHash: command.Report.ReportHash, ManifestHash: command.ManifestHash,
		EvidenceHash: command.EvidenceHash, Decision: decision, CreatedAt: command.Now,
	}
	result := RolloutGateOperatorResult{CommandID: command.CommandID, RequestHash: requestHash, Applied: command.Apply, Record: record, State: state}
	if command.Apply {
		s.gateDecisionRecords[record.DecisionID] = record
		s.rolloutCommandHashes[command.CommandID] = requestHash
		s.rolloutGateResults[command.CommandID] = result
	}
	return result, nil
}

func (s *MemoryStore) ApplyStageCommand(_ context.Context, command RolloutStageOperatorCommand) (RolloutStageOperatorResult, error) {
	requestHash, err := command.RequestHash()
	if err != nil {
		return RolloutStageOperatorResult{}, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if existingHash, ok := s.rolloutCommandHashes[command.CommandID]; ok {
		if existingHash != requestHash {
			return RolloutStageOperatorResult{}, ErrInvalidIdempotency
		}
		existing, ok := s.rolloutStageResults[command.CommandID]
		if !ok {
			return RolloutStageOperatorResult{}, ErrInvalidIdempotency
		}
		existing.Replayed = true
		return existing, nil
	}
	current := s.rolloutStageState
	if current.PolicyVersion == "" && s.policy.RolloutPolicy != nil {
		current.PolicyVersion = s.policy.RolloutPolicy.PolicyVersion
	}
	if current.Version != command.ExpectedVersion {
		return RolloutStageOperatorResult{}, ErrTaskVersionConflict
	}
	decision, ok := s.gateDecisionRecords[command.GateDecisionID]
	if !ok {
		return RolloutStageOperatorResult{}, ErrNotFound
	}
	if current.Paused || !decision.Decision.Passed || decision.EvidenceHash != command.EvidenceHash || !ValidRolloutTransition(current.Stage, command.Target) {
		return RolloutStageOperatorResult{}, ErrInvalidRunStatus
	}
	if s.policy.RolloutPolicy == nil || s.policy.RolloutPolicy.PolicyVersion != command.PolicyVersion {
		return RolloutStageOperatorResult{}, ErrNotFound
	}
	if command.Target == RolloutLegacyRetired {
		drain, ok := s.drainRecords[command.DrainID]
		if !ok {
			return RolloutStageOperatorResult{}, ErrNotFound
		}
		if drain.Status != DrainCompleted || !drain.Inventory.Empty() {
			return RolloutStageOperatorResult{}, ErrInvalidRunStatus
		}
	}
	if command.Now.IsZero() {
		command.Now = time.Now().UTC()
	}
	next := current
	next.Stage, next.PolicyVersion, next.LastGateDecisionID = command.Target, command.PolicyVersion, command.GateDecisionID
	next.EvidenceHash, next.Version, next.UpdatedAt = command.EvidenceHash, current.Version+1, command.Now
	result := RolloutStageOperatorResult{CommandID: command.CommandID, RequestHash: requestHash, Applied: command.Apply, State: next}
	if command.Apply {
		s.rolloutStageState = next
		s.rolloutCommandHashes[command.CommandID] = requestHash
		s.rolloutStageResults[command.CommandID] = result
	}
	return result, nil
}

func (s *MemoryStore) ApplyReadinessCommand(_ context.Context, command ReadinessOperatorCommand) (ReadinessOperatorResult, error) {
	requestHash, err := command.RequestHash()
	if err != nil {
		return ReadinessOperatorResult{}, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if existingHash, ok := s.rolloutCommandHashes[command.CommandID]; ok {
		if existingHash != requestHash {
			return ReadinessOperatorResult{}, ErrInvalidIdempotency
		}
		existing, ok := s.readinessResults[command.CommandID]
		if !ok {
			return ReadinessOperatorResult{}, ErrInvalidIdempotency
		}
		existing.Replayed = true
		return existing, nil
	}
	state := s.rolloutStageState
	if state.PolicyVersion == "" && s.policy.RolloutPolicy != nil {
		state.PolicyVersion = s.policy.RolloutPolicy.PolicyVersion
	}
	if state.Version != command.ExpectedVersion {
		return ReadinessOperatorResult{}, ErrTaskVersionConflict
	}
	var gate *GateDecisionRecord
	if value, ok := s.gateDecisionRecords[command.Input.GateDecisionID]; ok {
		copy := value
		gate = &copy
	}
	activePolicy := ""
	if s.policy.RolloutPolicy != nil {
		activePolicy = s.policy.RolloutPolicy.PolicyVersion
	}
	record, err := BuildReadinessRecord(command.Input, state, activePolicy, gate, command.Now)
	if err != nil {
		return ReadinessOperatorResult{}, err
	}
	result := ReadinessOperatorResult{CommandID: command.CommandID, RequestHash: requestHash, Applied: command.Apply, Record: record}
	if command.Apply {
		s.readinessRecords[record.ReadinessID] = record
		s.rolloutCommandHashes[command.CommandID] = requestHash
		s.readinessResults[command.CommandID] = result
	}
	return result, nil
}

func (s *MemoryStore) VerifyReadiness(_ context.Context, readinessID string) (ReadinessVerification, error) {
	if readinessID == "" {
		return ReadinessVerification{}, ErrInvalidPayload
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	record, ok := s.readinessRecords[readinessID]
	if !ok {
		return ReadinessVerification{}, ErrNotFound
	}
	hash, err := ReadinessRecordHash(record)
	if err != nil {
		return ReadinessVerification{}, err
	}
	return ReadinessVerification{
		ReadinessID: readinessID, Status: record.Status, RecordHash: record.RecordHash,
		Verified: hash == record.RecordHash, StateStale: record.RolloutStateVersion != s.rolloutStageState.Version,
	}, nil
}

var _ WorkflowRolloutControl = (*MemoryStore)(nil)

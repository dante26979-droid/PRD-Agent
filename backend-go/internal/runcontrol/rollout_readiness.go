package runcontrol

import (
	"context"
	"fmt"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/id"
)

type RolloutStage string

const (
	RolloutLocalOnly         RolloutStage = "LOCAL_ONLY"
	RolloutLocallyVerified   RolloutStage = "LOCALLY_VERIFIED"
	RolloutStagingShadow     RolloutStage = "STAGING_SHADOW"
	RolloutStagingEnforce    RolloutStage = "STAGING_ENFORCE"
	RolloutProductionCanary  RolloutStage = "PRODUCTION_CANARY"
	RolloutProductionDefault RolloutStage = "PRODUCTION_DEFAULT"
	RolloutLegacyDrain       RolloutStage = "LEGACY_DRAIN"
	RolloutLegacyRetired     RolloutStage = "LEGACY_RETIRED"
)

type GateDecisionRecord struct {
	DecisionID   string          `json:"decision_id"`
	Candidate    WorkflowVersion `json:"candidate_version"`
	Baseline     WorkflowVersion `json:"baseline_version"`
	DatasetHash  string          `json:"dataset_hash"`
	ConfigHash   string          `json:"config_hash"`
	ReportHash   string          `json:"report_hash"`
	ManifestHash string          `json:"manifest_hash"`
	EvidenceHash string          `json:"evidence_hash"`
	Decision     GateDecision    `json:"decision"`
	CreatedAt    time.Time       `json:"created_at"`
}

type RecordGateDecisionCommand struct {
	Manifest     GateManifest
	Report       GateReport
	ManifestHash string
	EvidenceHash string
	Now          time.Time
}

type RolloutStageState struct {
	Stage                 RolloutStage `json:"stage"`
	PolicyVersion         string       `json:"policy_version"`
	LastGateDecisionID    string       `json:"last_gate_decision_id"`
	EvidenceHash          string       `json:"evidence_hash"`
	Paused                bool         `json:"paused"`
	PauseReasonCode       string       `json:"pause_reason_code,omitempty"`
	RollbackPolicyVersion string       `json:"rollback_policy_version,omitempty"`
	Version               int          `json:"version"`
	UpdatedAt             time.Time    `json:"updated_at"`
}

type RolloutOperatorCommandKind string

const (
	RolloutCommandPause           RolloutOperatorCommandKind = "PAUSE"
	RolloutCommandResume          RolloutOperatorCommandKind = "RESUME"
	RolloutCommandRollback        RolloutOperatorCommandKind = "ROLLBACK_NEW_ASSIGNMENTS"
	RolloutCommandRecordGate      RolloutOperatorCommandKind = "RECORD_GATE"
	RolloutCommandTransition      RolloutOperatorCommandKind = "TRANSITION_STAGE"
	RolloutCommandRecordReadiness RolloutOperatorCommandKind = "RECORD_READINESS"
)

type RolloutOperatorCommand struct {
	CommandID       string                     `json:"command_id"`
	Kind            RolloutOperatorCommandKind `json:"command_kind"`
	ExpectedVersion int                        `json:"expected_state_version"`
	ReasonCode      string                     `json:"reason_code,omitempty"`
	PolicyVersion   string                     `json:"policy_version,omitempty"`
	GateDecisionID  string                     `json:"gate_decision_id,omitempty"`
	EvidenceHash    string                     `json:"evidence_hash"`
	ActorRef        string                     `json:"actor_ref"`
	Apply           bool                       `json:"apply"`
	Now             time.Time                  `json:"-"`
}

type RolloutOperatorResult struct {
	CommandID   string            `json:"command_id"`
	RequestHash string            `json:"request_hash"`
	Applied     bool              `json:"applied"`
	Replayed    bool              `json:"replayed"`
	State       RolloutStageState `json:"state"`
}

func (command RolloutOperatorCommand) RequestHash() (string, error) {
	if command.CommandID == "" || command.ExpectedVersion < 1 || command.EvidenceHash == "" || command.ActorRef == "" {
		return "", ErrInvalidPayload
	}
	switch command.Kind {
	case RolloutCommandPause:
		if command.ReasonCode == "" {
			return "", ErrInvalidPayload
		}
	case RolloutCommandResume:
		if command.GateDecisionID == "" {
			return "", ErrInvalidPayload
		}
	case RolloutCommandRollback:
		if command.PolicyVersion == "" || command.ReasonCode == "" {
			return "", ErrInvalidPayload
		}
	default:
		return "", ErrInvalidPayload
	}
	payload, err := canonicalJSON(map[string]any{
		"command_id": command.CommandID, "command_kind": command.Kind,
		"expected_state_version": command.ExpectedVersion, "reason_code": command.ReasonCode,
		"policy_version": command.PolicyVersion, "gate_decision_id": command.GateDecisionID,
		"evidence_hash": command.EvidenceHash, "actor_ref": command.ActorRef,
	})
	if err != nil {
		return "", err
	}
	return "sha256:" + hashBytesHex(payload), nil
}

type RolloutTransitionCommand struct {
	Target          RolloutStage
	GateDecisionID  string
	PolicyVersion   string
	EvidenceHash    string
	ExpectedVersion int
}

type LegacyInventory struct {
	WorkflowVersion        WorkflowVersion `json:"workflow_version"`
	ActiveRunCount         int64           `json:"active_run_count"`
	ActiveDispatchCount    int64           `json:"active_dispatch_count"`
	UnpublishedOutboxCount int64           `json:"unpublished_outbox_count"`
	NonTerminalLedgerCount int64           `json:"non_terminal_ledger_count"`
	SnapshotCount          int64           `json:"snapshot_count"`
	ReaderHitCount         int64           `json:"reader_hit_count"`
	InventoryHash          string          `json:"inventory_hash"`
	ObservedAt             time.Time       `json:"observed_at"`
}

func (value LegacyInventory) Empty() bool {
	return value.ActiveRunCount == 0 && value.ActiveDispatchCount == 0 &&
		value.UnpublishedOutboxCount == 0 && value.NonTerminalLedgerCount == 0 &&
		value.SnapshotCount == 0 && value.ReaderHitCount == 0
}

type DrainStatus string

const (
	DrainStarted   DrainStatus = "STARTED"
	DrainBlocked   DrainStatus = "BLOCKED"
	DrainCompleted DrainStatus = "COMPLETED"
)

type DrainRecord struct {
	DrainID         string          `json:"drain_id"`
	WorkflowVersion WorkflowVersion `json:"workflow_version"`
	Status          DrainStatus     `json:"status"`
	EvidenceHash    string          `json:"evidence_hash"`
	Inventory       LegacyInventory `json:"inventory"`
	StartedAt       time.Time       `json:"started_at"`
	CompletedAt     *time.Time      `json:"completed_at,omitempty"`
}

type BeginDrainCommand struct {
	WorkflowVersion WorkflowVersion
	EvidenceHash    string
	Now             time.Time
}

type WorkflowRolloutControl interface {
	ResolveNewRun(ctx context.Context, request AssignmentRequest) (RolloutAssignment, error)
	RecordGateDecision(ctx context.Context, command RecordGateDecisionCommand) (GateDecisionRecord, error)
	AuthorizeRolloutTransition(ctx context.Context, command RolloutTransitionCommand) (RolloutStageState, error)
	InspectLegacy(ctx context.Context, workflow WorkflowVersion, now time.Time) (LegacyInventory, error)
	BeginDrain(ctx context.Context, command BeginDrainCommand) (DrainRecord, error)
	RefreshDrain(ctx context.Context, drainID string, now time.Time) (DrainRecord, error)
	InspectRolloutState(ctx context.Context) (RolloutStageState, error)
	ApplyRolloutCommand(ctx context.Context, command RolloutOperatorCommand) (RolloutOperatorResult, error)
	ApplyGateCommand(ctx context.Context, command RolloutGateOperatorCommand) (RolloutGateOperatorResult, error)
	ApplyStageCommand(ctx context.Context, command RolloutStageOperatorCommand) (RolloutStageOperatorResult, error)
	ApplyReadinessCommand(ctx context.Context, command ReadinessOperatorCommand) (ReadinessOperatorResult, error)
	VerifyReadiness(ctx context.Context, readinessID string) (ReadinessVerification, error)
}

func (s *MemoryStore) ResolveNewRun(_ context.Context, request AssignmentRequest) (RolloutAssignment, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	policy := s.policy.RolloutPolicy
	if policy == nil {
		policy = &RolloutPolicy{
			PolicyVersion:   "memory-bootstrap:" + string(DefaultWorkflowVersion(s.policy.DefaultWorkflowVersion)),
			DefaultWorkflow: DefaultWorkflowVersion(s.policy.DefaultWorkflowVersion),
		}
	}
	return policy.Assign(request)
}

func (s *MemoryStore) RecordGateDecision(_ context.Context, command RecordGateDecisionCommand) (GateDecisionRecord, error) {
	if command.ManifestHash == "" || command.EvidenceHash == "" || command.Report.ReportHash == "" || command.Report.ConfigHash == "" {
		return GateDecisionRecord{}, ErrInvalidPayload
	}
	decision := EvaluateRolloutGate(command.Manifest, command.Report)
	decisionID, err := id.New("rollout-gate")
	if err != nil {
		return GateDecisionRecord{}, err
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
	s.mu.Lock()
	defer s.mu.Unlock()
	s.gateDecisionRecords[decisionID] = record
	return record, nil
}

func (s *MemoryStore) AuthorizeRolloutTransition(_ context.Context, command RolloutTransitionCommand) (RolloutStageState, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	current := s.rolloutStageState
	if command.ExpectedVersion < 1 || current.Version != command.ExpectedVersion {
		return RolloutStageState{}, ErrTaskVersionConflict
	}
	decision, ok := s.gateDecisionRecords[command.GateDecisionID]
	if !ok {
		return RolloutStageState{}, ErrNotFound
	}
	if !decision.Decision.Passed || decision.EvidenceHash != command.EvidenceHash || current.Paused {
		return RolloutStageState{}, ErrInvalidRunStatus
	}
	if !ValidRolloutTransition(current.Stage, command.Target) {
		return RolloutStageState{}, fmt.Errorf("%w: rollout stage transition %s -> %s", ErrInvalidRunStatus, current.Stage, command.Target)
	}
	if command.PolicyVersion == "" {
		return RolloutStageState{}, ErrInvalidPayload
	}
	current.Stage = command.Target
	current.PolicyVersion = command.PolicyVersion
	current.LastGateDecisionID = decision.DecisionID
	current.EvidenceHash = command.EvidenceHash
	current.Version++
	current.UpdatedAt = time.Now().UTC()
	s.rolloutStageState = current
	return current, nil
}

func (s *MemoryStore) InspectRolloutState(_ context.Context) (RolloutStageState, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	state := s.rolloutStageState
	if state.PolicyVersion == "" && s.policy.RolloutPolicy != nil {
		state.PolicyVersion = s.policy.RolloutPolicy.PolicyVersion
	}
	return state, nil
}

func (s *MemoryStore) ApplyRolloutCommand(_ context.Context, command RolloutOperatorCommand) (RolloutOperatorResult, error) {
	requestHash, err := command.RequestHash()
	if err != nil {
		return RolloutOperatorResult{}, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if existingHash, ok := s.rolloutCommandHashes[command.CommandID]; ok {
		if existingHash != requestHash {
			return RolloutOperatorResult{}, ErrInvalidIdempotency
		}
		existing, ok := s.rolloutOperatorResults[command.CommandID]
		if !ok {
			return RolloutOperatorResult{}, ErrInvalidIdempotency
		}
		existing.Replayed = true
		return existing, nil
	}
	if existing, ok := s.rolloutOperatorResults[command.CommandID]; ok {
		if existing.RequestHash != requestHash {
			return RolloutOperatorResult{}, ErrInvalidIdempotency
		}
		existing.Replayed = true
		return existing, nil
	}
	current := s.rolloutStageState
	if current.PolicyVersion == "" && s.policy.RolloutPolicy != nil {
		current.PolicyVersion = s.policy.RolloutPolicy.PolicyVersion
	}
	if current.Version != command.ExpectedVersion {
		return RolloutOperatorResult{}, ErrTaskVersionConflict
	}
	next := current
	switch command.Kind {
	case RolloutCommandPause:
		if current.Paused {
			return RolloutOperatorResult{}, ErrInvalidRunStatus
		}
		next.Paused, next.PauseReasonCode = true, command.ReasonCode
	case RolloutCommandResume:
		decision, ok := s.gateDecisionRecords[command.GateDecisionID]
		if !current.Paused || !ok || !decision.Decision.Passed || decision.EvidenceHash != command.EvidenceHash {
			return RolloutOperatorResult{}, ErrInvalidRunStatus
		}
		next.Paused, next.PauseReasonCode = false, ""
		next.LastGateDecisionID = command.GateDecisionID
	case RolloutCommandRollback:
		if s.policy.RolloutPolicy == nil || s.policy.RolloutPolicy.PolicyVersion != command.PolicyVersion {
			return RolloutOperatorResult{}, ErrNotFound
		}
		next.RollbackPolicyVersion = current.PolicyVersion
		next.PolicyVersion = command.PolicyVersion
		next.Paused, next.PauseReasonCode = true, command.ReasonCode
	}
	next.EvidenceHash = command.EvidenceHash
	next.Version++
	if command.Now.IsZero() {
		command.Now = time.Now().UTC()
	}
	next.UpdatedAt = command.Now
	result := RolloutOperatorResult{CommandID: command.CommandID, RequestHash: requestHash, Applied: command.Apply, State: next}
	if command.Apply {
		s.rolloutStageState = next
		s.rolloutOperatorResults[command.CommandID] = result
		s.rolloutCommandHashes[command.CommandID] = requestHash
	}
	return result, nil
}

func ValidRolloutTransition(current, target RolloutStage) bool {
	next := map[RolloutStage]RolloutStage{
		RolloutLocalOnly:         RolloutLocallyVerified,
		RolloutLocallyVerified:   RolloutStagingShadow,
		RolloutStagingShadow:     RolloutStagingEnforce,
		RolloutStagingEnforce:    RolloutProductionCanary,
		RolloutProductionCanary:  RolloutProductionDefault,
		RolloutProductionDefault: RolloutLegacyDrain,
		RolloutLegacyDrain:       RolloutLegacyRetired,
	}
	return next[current] == target
}

func (s *MemoryStore) InspectLegacy(_ context.Context, workflow WorkflowVersion, now time.Time) (LegacyInventory, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.inspectLegacyLocked(workflow, now)
}

func (s *MemoryStore) BeginDrain(_ context.Context, command BeginDrainCommand) (DrainRecord, error) {
	if command.WorkflowVersion != WorkflowVersionV1 || command.EvidenceHash == "" {
		return DrainRecord{}, ErrInvalidPayload
	}
	if command.Now.IsZero() {
		command.Now = time.Now().UTC()
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	inventory, err := s.inspectLegacyLocked(command.WorkflowVersion, command.Now)
	if err != nil {
		return DrainRecord{}, err
	}
	drainID, err := id.New("rollout-drain")
	if err != nil {
		return DrainRecord{}, err
	}
	status := DrainBlocked
	var completedAt *time.Time
	if inventory.Empty() {
		status = DrainCompleted
		completed := command.Now
		completedAt = &completed
	}
	record := DrainRecord{
		DrainID: drainID, WorkflowVersion: command.WorkflowVersion, Status: status,
		EvidenceHash: command.EvidenceHash, Inventory: inventory, StartedAt: command.Now, CompletedAt: completedAt,
	}
	s.drainRecords[drainID] = record
	return record, nil
}

func (s *MemoryStore) RefreshDrain(_ context.Context, drainID string, now time.Time) (DrainRecord, error) {
	if now.IsZero() {
		now = time.Now().UTC()
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	record, ok := s.drainRecords[drainID]
	if !ok {
		return DrainRecord{}, ErrNotFound
	}
	if record.Status == DrainCompleted {
		return record, nil
	}
	inventory, err := s.inspectLegacyLocked(record.WorkflowVersion, now)
	if err != nil {
		return DrainRecord{}, err
	}
	record.Inventory = inventory
	if inventory.Empty() {
		record.Status = DrainCompleted
		completed := now
		record.CompletedAt = &completed
	} else {
		record.Status = DrainBlocked
	}
	s.drainRecords[drainID] = record
	return record, nil
}

func (s *MemoryStore) inspectLegacyLocked(workflow WorkflowVersion, now time.Time) (LegacyInventory, error) {
	if workflow != WorkflowVersionV1 {
		return LegacyInventory{}, ErrInvalidPayload
	}
	runIDs := make(map[string]bool)
	value := LegacyInventory{WorkflowVersion: workflow, ObservedAt: now}
	for _, run := range s.runs {
		if run.WorkflowVersion != workflow {
			continue
		}
		runIDs[run.RunID] = true
		if !run.Status.Terminal() {
			value.ActiveRunCount++
		}
	}
	for _, dispatch := range s.dispatches {
		if runIDs[dispatch.RunID] && (dispatch.Status == DispatchStarted || dispatch.Status == DispatchRunning) {
			value.ActiveDispatchCount++
		}
	}
	for _, entry := range s.outbox {
		if runIDs[entry.message.AggregateID] && !entry.published {
			value.UnpublishedOutboxCount++
		}
	}
	for _, entry := range s.ledgerEntries {
		if runIDs[entry.RunID] && (entry.Status == LedgerReserved || entry.Status == LedgerCallStarted || entry.Status == LedgerOutcomeUnknown) {
			value.NonTerminalLedgerCount++
		}
	}
	for runID, checkpoint := range s.checkpoints {
		if runIDs[runID] && checkpoint.sequence > 0 {
			value.SnapshotCount++
		}
	}
	payload, err := canonicalJSON(map[string]any{
		"workflow_version":          value.WorkflowVersion,
		"active_run_count":          value.ActiveRunCount,
		"active_dispatch_count":     value.ActiveDispatchCount,
		"unpublished_outbox_count":  value.UnpublishedOutboxCount,
		"non_terminal_ledger_count": value.NonTerminalLedgerCount,
		"snapshot_count":            value.SnapshotCount,
		"reader_hit_count":          value.ReaderHitCount,
	})
	if err != nil {
		return LegacyInventory{}, err
	}
	value.InventoryHash = hashBytesHex(payload)
	return value, nil
}

var _ WorkflowRolloutControl = (*MemoryStore)(nil)

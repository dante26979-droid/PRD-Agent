package runcontrol

import (
	"sort"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/id"
)

const ReadinessSchemaVersion = "agent-runtime-readiness.v1"

type RolloutGateOperatorCommand struct {
	CommandID       string       `json:"command_id"`
	ExpectedVersion int          `json:"expected_state_version"`
	Manifest        GateManifest `json:"-"`
	Report          GateReport   `json:"-"`
	ManifestHash    string       `json:"manifest_hash"`
	EvidenceHash    string       `json:"evidence_hash"`
	ActorRef        string       `json:"actor_ref"`
	Apply           bool         `json:"apply"`
	Now             time.Time    `json:"-"`
}

type RolloutGateOperatorResult struct {
	CommandID   string             `json:"command_id"`
	RequestHash string             `json:"request_hash"`
	Applied     bool               `json:"applied"`
	Replayed    bool               `json:"replayed"`
	Record      GateDecisionRecord `json:"record"`
	State       RolloutStageState  `json:"state"`
}

func (command RolloutGateOperatorCommand) RequestHash() (string, error) {
	if command.CommandID == "" || command.ExpectedVersion < 1 || command.ManifestHash == "" || command.EvidenceHash == "" || command.ActorRef == "" || command.Report.ReportHash == "" || command.Report.ConfigHash == "" {
		return "", ErrInvalidPayload
	}
	payload, err := canonicalJSON(map[string]any{
		"command_id": command.CommandID, "command_kind": RolloutCommandRecordGate,
		"expected_state_version": command.ExpectedVersion, "manifest_hash": command.ManifestHash,
		"evidence_hash": command.EvidenceHash, "actor_ref": command.ActorRef,
		"candidate_version": command.Report.Candidate, "baseline_version": command.Report.Baseline,
		"dataset_hash": command.Report.DatasetHash, "config_hash": command.Report.ConfigHash,
		"report_hash": command.Report.ReportHash,
	})
	if err != nil {
		return "", err
	}
	return "sha256:" + hashBytesHex(payload), nil
}

type RolloutStageOperatorCommand struct {
	CommandID             string       `json:"command_id"`
	ExpectedVersion       int          `json:"expected_state_version"`
	Target                RolloutStage `json:"target_stage"`
	GateDecisionID        string       `json:"gate_decision_id"`
	PolicyVersion         string       `json:"policy_version"`
	EvidenceHash          string       `json:"evidence_hash"`
	DrainID               string       `json:"drain_id,omitempty"`
	RemovalInventoryHash  string       `json:"removal_inventory_hash,omitempty"`
	ReaderObservationHash string       `json:"reader_observation_hash,omitempty"`
	ActorRef              string       `json:"actor_ref"`
	Apply                 bool         `json:"apply"`
	Now                   time.Time    `json:"-"`
}

type RolloutStageOperatorResult struct {
	CommandID   string            `json:"command_id"`
	RequestHash string            `json:"request_hash"`
	Applied     bool              `json:"applied"`
	Replayed    bool              `json:"replayed"`
	State       RolloutStageState `json:"state"`
}

func (command RolloutStageOperatorCommand) RequestHash() (string, error) {
	if command.CommandID == "" || command.ExpectedVersion < 1 || command.Target == "" || command.GateDecisionID == "" || command.PolicyVersion == "" || command.EvidenceHash == "" || command.ActorRef == "" {
		return "", ErrInvalidPayload
	}
	if command.Target == RolloutLegacyRetired && (command.DrainID == "" || command.RemovalInventoryHash == "" || command.ReaderObservationHash == "") {
		return "", ErrInvalidPayload
	}
	payload, err := canonicalJSON(map[string]any{
		"command_id": command.CommandID, "command_kind": RolloutCommandTransition,
		"expected_state_version": command.ExpectedVersion, "target_stage": command.Target,
		"gate_decision_id": command.GateDecisionID, "policy_version": command.PolicyVersion,
		"evidence_hash": command.EvidenceHash, "actor_ref": command.ActorRef,
		"drain_id": command.DrainID, "removal_inventory_hash": command.RemovalInventoryHash,
		"reader_observation_hash": command.ReaderObservationHash,
	})
	if err != nil {
		return "", err
	}
	return "sha256:" + hashBytesHex(payload), nil
}

type ReadinessStatus string

const (
	ReadinessBlocked      ReadinessStatus = "BLOCKED"
	ReadinessStagingReady ReadinessStatus = "STAGING_READY"
)

type ReadinessInput struct {
	CommitHash         string `json:"commit_hash"`
	MigrationSetHash   string `json:"migration_set_hash"`
	MigrationHead      string `json:"migration_head"`
	ContractReportHash string `json:"contract_report_hash"`
	PostgresReportHash string `json:"postgres_report_hash"`
	EvalManifestHash   string `json:"eval_manifest_hash"`
	ShadowPolicyHash   string `json:"shadow_policy_hash"`
	GateDecisionID     string `json:"gate_decision_id"`
}

type ReadinessRecord struct {
	ReadinessID         string          `json:"readiness_id"`
	SchemaVersion       string          `json:"schema_version"`
	Candidate           WorkflowVersion `json:"candidate_version"`
	Baseline            WorkflowVersion `json:"baseline_version"`
	CommitHash          string          `json:"commit_hash"`
	MigrationSetHash    string          `json:"migration_set_hash"`
	MigrationHead       string          `json:"migration_head"`
	ContractReportHash  string          `json:"contract_report_hash"`
	PostgresReportHash  string          `json:"postgres_report_hash"`
	EvalManifestHash    string          `json:"eval_manifest_hash"`
	ShadowPolicyHash    string          `json:"shadow_policy_hash"`
	GateDecisionID      string          `json:"gate_decision_id"`
	RolloutStateVersion int             `json:"rollout_state_version"`
	BlockingGateCodes   []string        `json:"blocking_gate_codes"`
	RecordHash          string          `json:"record_hash"`
	Status              ReadinessStatus `json:"status"`
	CreatedAt           time.Time       `json:"created_at"`
}

type ReadinessOperatorCommand struct {
	CommandID       string         `json:"command_id"`
	ExpectedVersion int            `json:"expected_state_version"`
	Input           ReadinessInput `json:"input"`
	ActorRef        string         `json:"actor_ref"`
	Apply           bool           `json:"apply"`
	Now             time.Time      `json:"-"`
}

type ReadinessOperatorResult struct {
	CommandID   string          `json:"command_id"`
	RequestHash string          `json:"request_hash"`
	Applied     bool            `json:"applied"`
	Replayed    bool            `json:"replayed"`
	Record      ReadinessRecord `json:"record"`
}

type ReadinessVerification struct {
	ReadinessID string          `json:"readiness_id"`
	Status      ReadinessStatus `json:"status"`
	RecordHash  string          `json:"record_hash"`
	Verified    bool            `json:"verified"`
	StateStale  bool            `json:"state_stale"`
}

func (command ReadinessOperatorCommand) RequestHash() (string, error) {
	if command.CommandID == "" || command.ExpectedVersion < 1 || command.ActorRef == "" {
		return "", ErrInvalidPayload
	}
	payload, err := canonicalJSON(map[string]any{
		"command_id": command.CommandID, "command_kind": RolloutCommandRecordReadiness,
		"expected_state_version": command.ExpectedVersion, "actor_ref": command.ActorRef, "input": command.Input,
	})
	if err != nil {
		return "", err
	}
	return "sha256:" + hashBytesHex(payload), nil
}

func BuildReadinessRecord(input ReadinessInput, state RolloutStageState, activePolicy string, gate *GateDecisionRecord, now time.Time) (ReadinessRecord, error) {
	if now.IsZero() {
		now = time.Now().UTC()
	}
	blockers := make([]string, 0)
	required := []struct{ value, code string }{
		{input.CommitHash, "MISSING_COMMIT_HASH"}, {input.MigrationSetHash, "MISSING_MIGRATION_SET_HASH"},
		{input.ContractReportHash, "MISSING_CONTRACT_REPORT_HASH"}, {input.PostgresReportHash, "MISSING_POSTGRES_REPORT_HASH"},
		{input.EvalManifestHash, "MISSING_EVAL_MANIFEST_HASH"}, {input.ShadowPolicyHash, "MISSING_SHADOW_POLICY_HASH"},
		{input.GateDecisionID, "MISSING_GATE_DECISION"},
	}
	for _, item := range required {
		if item.value == "" {
			blockers = append(blockers, item.code)
		}
	}
	if input.MigrationHead != "0021" {
		blockers = append(blockers, "MIGRATION_HEAD_NOT_0021")
	}
	if state.Paused {
		blockers = append(blockers, "ROLLOUT_PAUSED")
	}
	if state.Stage != RolloutLocallyVerified {
		blockers = append(blockers, "STAGE_NOT_LOCALLY_VERIFIED")
	}
	if state.PolicyVersion == "" || activePolicy != state.PolicyVersion {
		blockers = append(blockers, "ACTIVE_POLICY_STATE_MISMATCH")
	}
	if gate == nil || !gate.Decision.Passed || gate.DecisionID != input.GateDecisionID || state.LastGateDecisionID != input.GateDecisionID {
		blockers = append(blockers, "GATE_DECISION_NOT_BOUND")
	} else if gate.ManifestHash != input.EvalManifestHash {
		blockers = append(blockers, "GATE_MANIFEST_HASH_MISMATCH")
	}
	sort.Strings(blockers)
	status := ReadinessBlocked
	if len(blockers) == 0 {
		status = ReadinessStagingReady
	}
	readinessID, err := id.New("rollout-readiness")
	if err != nil {
		return ReadinessRecord{}, err
	}
	record := ReadinessRecord{
		ReadinessID: readinessID, SchemaVersion: ReadinessSchemaVersion,
		Candidate: WorkflowVersionV4, Baseline: WorkflowVersionV1,
		CommitHash: input.CommitHash, MigrationSetHash: input.MigrationSetHash, MigrationHead: input.MigrationHead,
		ContractReportHash: input.ContractReportHash, PostgresReportHash: input.PostgresReportHash,
		EvalManifestHash: input.EvalManifestHash, ShadowPolicyHash: input.ShadowPolicyHash,
		GateDecisionID: input.GateDecisionID, RolloutStateVersion: state.Version,
		BlockingGateCodes: blockers, Status: status, CreatedAt: now,
	}
	hash, err := ReadinessRecordHash(record)
	if err != nil {
		return ReadinessRecord{}, err
	}
	record.RecordHash = hash
	return record, nil
}

func ReadinessRecordHash(record ReadinessRecord) (string, error) {
	payload, err := canonicalJSON(map[string]any{
		"schema_version": record.SchemaVersion, "candidate_version": record.Candidate, "baseline_version": record.Baseline,
		"commit_hash": record.CommitHash, "migration_set_hash": record.MigrationSetHash, "migration_head": record.MigrationHead,
		"contract_report_hash": record.ContractReportHash, "postgres_report_hash": record.PostgresReportHash,
		"eval_manifest_hash": record.EvalManifestHash, "shadow_policy_hash": record.ShadowPolicyHash,
		"gate_decision_id": record.GateDecisionID, "rollout_state_version": record.RolloutStateVersion,
		"blocking_gate_codes": record.BlockingGateCodes, "status": record.Status,
	})
	if err != nil {
		return "", err
	}
	return "sha256:" + hashBytesHex(payload), nil
}

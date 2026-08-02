package runcontrol

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strings"
)

type EvaluationMode string

const (
	EvaluationOff     EvaluationMode = "OFF"
	EvaluationShadow  EvaluationMode = "SHADOW"
	EvaluationEnforce EvaluationMode = "ENFORCE"
)

type RolloutCohort string

const (
	CohortControl  RolloutCohort = "CONTROL"
	CohortInternal RolloutCohort = "INTERNAL"
	CohortCanary   RolloutCohort = "CANARY"
	CohortDefault  RolloutCohort = "DEFAULT"
)

type AssignmentRequest struct {
	TenantID string
	OwnerID  string
	TaskID   string
}

type RolloutAssignment struct {
	AuthoritativeWorkflowVersion WorkflowVersion `json:"authoritative_workflow_version"`
	EvaluationMode               EvaluationMode  `json:"evaluation_mode"`
	ShadowWorkflowVersion        WorkflowVersion `json:"shadow_workflow_version,omitempty"`
	Cohort                       RolloutCohort   `json:"cohort"`
	PolicyVersion                string          `json:"policy_version"`
	AssignmentReason             string          `json:"assignment_reason"`
	AssignmentHash               string          `json:"assignment_hash"`
}

func (assignment RolloutAssignment) WithHash() RolloutAssignment {
	assignment.AssignmentHash = ""
	payload := strings.Join([]string{
		string(assignment.AuthoritativeWorkflowVersion),
		string(assignment.EvaluationMode),
		string(assignment.ShadowWorkflowVersion),
		string(assignment.Cohort),
		assignment.PolicyVersion,
		assignment.AssignmentReason,
	}, "\x00")
	digest := sha256.Sum256([]byte(payload))
	assignment.AssignmentHash = "sha256:" + fmt.Sprintf("%x", digest[:])
	return assignment
}

type RolloutPolicy struct {
	PolicyVersion     string          `json:"policy_version"`
	DefaultWorkflow   WorkflowVersion `json:"default_workflow"`
	CanaryBasisPoints int             `json:"canary_basis_points"`
	Shadow            bool            `json:"shadow"`
	EmergencyDeny     map[string]bool `json:"emergency_deny"`
	ExplicitV4Tasks   map[string]bool `json:"explicit_v4_tasks"`
	InternalOwners    map[string]bool `json:"internal_owners"`
}

func (policy RolloutPolicy) Assign(request AssignmentRequest) (RolloutAssignment, error) {
	if request.TenantID == "" || request.OwnerID == "" || request.TaskID == "" || policy.PolicyVersion == "" {
		return RolloutAssignment{}, fmt.Errorf("%w: rollout assignment requires identities and policy version", ErrInvalidPayload)
	}
	if !policy.DefaultWorkflow.Supported() || policy.CanaryBasisPoints < 0 || policy.CanaryBasisPoints > 10000 {
		return RolloutAssignment{}, fmt.Errorf("%w: invalid rollout policy", ErrInvalidPayload)
	}
	identity := request.TenantID + ":" + request.OwnerID
	assignment := RolloutAssignment{
		AuthoritativeWorkflowVersion: policy.DefaultWorkflow,
		EvaluationMode:               EvaluationEnforce,
		Cohort:                       CohortDefault,
		PolicyVersion:                policy.PolicyVersion,
		AssignmentReason:             "default",
	}
	if policy.EmergencyDeny[identity] {
		assignment.AuthoritativeWorkflowVersion = WorkflowVersionV1
		assignment.EvaluationMode = EvaluationOff
		assignment.Cohort = CohortControl
		assignment.AssignmentReason = "emergency_deny"
		return assignment.WithHash(), nil
	}
	if policy.ExplicitV4Tasks[request.TaskID] {
		assignment.AuthoritativeWorkflowVersion = WorkflowVersionV4
		assignment.EvaluationMode = EvaluationEnforce
		assignment.Cohort = CohortInternal
		assignment.AssignmentReason = "explicit_task"
		return assignment.WithHash(), nil
	}
	if policy.InternalOwners[identity] {
		assignment.AuthoritativeWorkflowVersion = WorkflowVersionV4
		assignment.EvaluationMode = EvaluationEnforce
		assignment.Cohort = CohortInternal
		assignment.AssignmentReason = "owner_allowlist"
		return assignment.WithHash(), nil
	}
	if rolloutBucket(request, policy.PolicyVersion) < policy.CanaryBasisPoints {
		assignment.AuthoritativeWorkflowVersion = WorkflowVersionV4
		assignment.EvaluationMode = EvaluationEnforce
		assignment.Cohort = CohortCanary
		assignment.AssignmentReason = "percentage"
		return assignment.WithHash(), nil
	}
	if policy.Shadow && assignment.AuthoritativeWorkflowVersion == WorkflowVersionV1 {
		assignment.EvaluationMode = EvaluationShadow
		assignment.ShadowWorkflowVersion = WorkflowVersionV4
		assignment.Cohort = CohortControl
		assignment.AssignmentReason = "shadow"
	}
	return assignment.WithHash(), nil
}

func rolloutBucket(request AssignmentRequest, policyVersion string) int {
	digest := sha256.Sum256([]byte(strings.Join([]string{request.TenantID, request.OwnerID, request.TaskID, policyVersion}, "\x00")))
	return int(binary.BigEndian.Uint64(digest[:8]) % 10000)
}

type RemoteEffectCounters struct {
	ModelPhysicalCalls      int64
	CapabilityPhysicalCalls int64
	DraftWrites             int64
	UnitWrites              int64
	PublishWrites           int64
}

func ValidateShadowEffects(baseline, shadow RemoteEffectCounters) error {
	if shadow.ModelPhysicalCalls != baseline.ModelPhysicalCalls || shadow.CapabilityPhysicalCalls != baseline.CapabilityPhysicalCalls || shadow.DraftWrites != baseline.DraftWrites || shadow.UnitWrites != baseline.UnitWrites || shadow.PublishWrites != baseline.PublishWrites {
		return fmt.Errorf("%w: SHADOW_REMOTE_EFFECT_FORBIDDEN", errNoRemoteEffects)
	}
	return nil
}

type ShadowEvaluationArtifact struct {
	SchemaVersion                string         `json:"schema_version"`
	AuthoritativeTraceHash       string         `json:"authoritative_trace_hash"`
	AuthoritativeTrace           map[string]any `json:"authoritative_trace"`
	CandidatePolicyVersion       string         `json:"candidate_policy_version"`
	AssignmentHash               string         `json:"assignment_hash"`
	ExtraModelPhysicalCalls      int64          `json:"extra_model_physical_calls"`
	ExtraCapabilityPhysicalCalls int64          `json:"extra_capability_physical_calls"`
	ExtraDraftWrites             int64          `json:"extra_draft_writes"`
	ExtraUnitWrites              int64          `json:"extra_unit_writes"`
	ExtraPublishWrites           int64          `json:"extra_publish_writes"`
}

// AuthoritativeTrace is deliberately limited to identities persisted by the
// Go control plane before the shadow artifact is accepted. This prevents a
// worker from presenting a self-consistent, but fabricated, trace/hash pair.
type AuthoritativeTrace struct {
	RunID                        string `json:"run_id"`
	TaskID                       string `json:"task_id"`
	AuthoritativeWorkflowVersion string `json:"authoritative_workflow_version"`
	RunPurpose                   int    `json:"run_purpose"`
	CheckpointSequence           int64  `json:"checkpoint_sequence"`
	OutputKey                    string `json:"output_key"`
	OutputKind                   int    `json:"output_kind"`
	OutputContentHash            string `json:"output_content_hash"`
	DraftKey                     string `json:"draft_key"`
	DraftContentHash             string `json:"draft_content_hash"`
	SubmissionDisposition        string `json:"submission_disposition"`
}

type PersistedAuthoritativeResult struct {
	RunID             string
	TaskID            string
	OutputKey         string
	OutputKind        RunOutputKind
	OutputContentHash string
	DraftKey          string
	DraftContentHash  string
}

// ParseShadowEvaluationArtifact performs the self-contained cryptographic and
// assignment checks and returns the typed trace for an adapter-level lookup.
func ParseShadowEvaluationArtifact(runID string, artifact RunArtifact, assignment RolloutAssignment) (AuthoritativeTrace, error) {
	var empty AuthoritativeTrace
	if assignment.EvaluationMode != EvaluationShadow || artifact.ArtifactType != "SHADOW_EVALUATION" || artifact.ArtifactKey != runID+":shadow-evaluation:1" || artifact.Generation != 1 {
		return empty, fmt.Errorf("%w: invalid shadow artifact identity", ErrInvalidPayload)
	}
	var value ShadowEvaluationArtifact
	if err := json.Unmarshal(artifact.Content, &value); err != nil {
		return empty, fmt.Errorf("%w: invalid shadow artifact payload", ErrInvalidPayload)
	}
	if value.SchemaVersion != "shadow-evaluation.v1" || value.AuthoritativeTraceHash == "" || value.CandidatePolicyVersion != assignment.PolicyVersion || value.AssignmentHash != assignment.AssignmentHash {
		return empty, fmt.Errorf("%w: shadow artifact assignment drift", ErrInvalidPayload)
	}
	tracePayload, err := canonicalJSON(value.AuthoritativeTrace)
	if err != nil {
		return empty, fmt.Errorf("%w: invalid shadow authoritative trace", ErrInvalidPayload)
	}
	var trace AuthoritativeTrace
	if err := json.Unmarshal(tracePayload, &trace); err != nil || trace.RunID != runID || trace.AuthoritativeWorkflowVersion != string(assignment.AuthoritativeWorkflowVersion) {
		return empty, fmt.Errorf("%w: shadow authoritative trace identity drift", ErrInvalidPayload)
	}
	traceDigest := sha256.Sum256(tracePayload)
	if value.AuthoritativeTraceHash != "sha256:"+hex.EncodeToString(traceDigest[:]) {
		return empty, fmt.Errorf("%w: shadow authoritative trace hash drift", ErrInvalidPayload)
	}
	effects := RemoteEffectCounters{
		ModelPhysicalCalls: value.ExtraModelPhysicalCalls, CapabilityPhysicalCalls: value.ExtraCapabilityPhysicalCalls,
		DraftWrites: value.ExtraDraftWrites, UnitWrites: value.ExtraUnitWrites, PublishWrites: value.ExtraPublishWrites,
	}
	if err := ValidateShadowEffects(RemoteEffectCounters{}, effects); err != nil {
		return empty, err
	}
	digest := sha256.Sum256([]byte(strings.Join([]string{value.AuthoritativeTraceHash, value.CandidatePolicyVersion, value.AssignmentHash}, "\x00")))
	if artifact.RequestHash != "sha256:"+hex.EncodeToString(digest[:]) {
		return empty, fmt.Errorf("%w: shadow artifact request hash drift", ErrInvalidPayload)
	}
	return trace, nil
}

func ValidateShadowEvaluationArtifact(runID string, artifact RunArtifact, assignment RolloutAssignment) error {
	_, err := ParseShadowEvaluationArtifact(runID, artifact, assignment)
	return err
}

// ValidatePersistedAuthoritativeTrace binds the signed trace to the result
// already acknowledged by the control plane. Exactly one authoritative result
// kind must be present; a trace cannot choose an unpersisted worker-local value.
func ValidatePersistedAuthoritativeTrace(trace AuthoritativeTrace, persisted PersistedAuthoritativeResult) error {
	if trace.RunID != persisted.RunID || trace.TaskID == "" || trace.TaskID != persisted.TaskID {
		return fmt.Errorf("%w: shadow persisted trace identity drift", ErrInvalidPayload)
	}
	hasOutput := trace.OutputKey != "" || trace.OutputContentHash != "" || trace.OutputKind != 0
	hasDraft := trace.DraftKey != "" || trace.DraftContentHash != ""
	if hasOutput == hasDraft {
		return fmt.Errorf("%w: shadow trace must bind exactly one persisted result", ErrInvalidPayload)
	}
	if hasOutput {
		if trace.OutputKey != persisted.OutputKey || runOutputKindFromTrace(trace.OutputKind) != persisted.OutputKind || bareSHA256(trace.OutputContentHash) != bareSHA256(persisted.OutputContentHash) {
			return fmt.Errorf("%w: shadow output trace drift", ErrInvalidPayload)
		}
		return nil
	}
	if trace.DraftKey != persisted.DraftKey || bareSHA256(trace.DraftContentHash) != bareSHA256(persisted.DraftContentHash) {
		return fmt.Errorf("%w: shadow draft trace drift", ErrInvalidPayload)
	}
	return nil
}

func runOutputKindFromTrace(value int) RunOutputKind {
	switch value {
	case 1:
		return RunOutputOutlineCandidate
	case 2:
		return RunOutputUnitCandidate
	case 3:
		return RunOutputUnitPatch
	case 4:
		return RunOutputFullReviewReport
	default:
		return ""
	}
}

type GateThreshold struct {
	Metric       string   `json:"metric"`
	Minimum      *float64 `json:"minimum,omitempty"`
	Maximum      *float64 `json:"maximum,omitempty"`
	HardRequired bool     `json:"hard_required"`
}

type GateManifest struct {
	SchemaVersion      string          `json:"schema_version"`
	CandidateVersion   WorkflowVersion `json:"candidate_version"`
	BaselineVersion    WorkflowVersion `json:"baseline_version"`
	DatasetHash        string          `json:"dataset_hash"`
	MinimumRepetitions int             `json:"minimum_repetitions"`
	Thresholds         []GateThreshold `json:"thresholds"`
	BlockingCases      []string        `json:"blocking_cases"`
}

type GateReport struct {
	DatasetHash string             `json:"dataset_hash"`
	Repetitions int                `json:"repetitions"`
	Metrics     map[string]float64 `json:"metrics"`
	FailedCases []string           `json:"failed_cases"`
	ReportHash  string             `json:"report_hash"`
	ConfigHash  string             `json:"config_hash"`
	Candidate   WorkflowVersion    `json:"candidate_version"`
	Baseline    WorkflowVersion    `json:"baseline_version"`
}

type GateDecision struct {
	Passed          bool     `json:"passed"`
	FailedGateCodes []string `json:"failed_gate_codes"`
}

func EvaluateRolloutGate(manifest GateManifest, report GateReport) GateDecision {
	failed := make([]string, 0)
	if manifest.SchemaVersion != "agent-runtime-gate.v1" || manifest.DatasetHash == "" || report.DatasetHash != manifest.DatasetHash {
		failed = append(failed, "DATASET_HASH_MISMATCH")
	}
	if report.Candidate != manifest.CandidateVersion || report.Baseline != manifest.BaselineVersion || report.ReportHash == "" || report.ConfigHash == "" {
		failed = append(failed, "REPORT_IDENTITY_MISMATCH")
	}
	if report.Repetitions < manifest.MinimumRepetitions || report.Repetitions < 1 {
		failed = append(failed, "INSUFFICIENT_REPETITIONS")
	}
	failedCaseSet := make(map[string]bool, len(report.FailedCases))
	for _, item := range report.FailedCases {
		failedCaseSet[item] = true
	}
	for _, item := range manifest.BlockingCases {
		if failedCaseSet[item] {
			failed = append(failed, "BLOCKING_CASE:"+item)
		}
	}
	for _, threshold := range manifest.Thresholds {
		if threshold.Metric == "" || (threshold.Minimum == nil && threshold.Maximum == nil) || (threshold.Minimum != nil && !finite(*threshold.Minimum)) || (threshold.Maximum != nil && !finite(*threshold.Maximum)) || (threshold.Minimum != nil && threshold.Maximum != nil && *threshold.Minimum > *threshold.Maximum) {
			failed = append(failed, "INVALID_THRESHOLD:"+threshold.Metric)
			continue
		}
		value, exists := report.Metrics[threshold.Metric]
		if !exists {
			if threshold.HardRequired {
				failed = append(failed, "MISSING_METRIC:"+threshold.Metric)
			}
			continue
		}
		if !finite(value) {
			failed = append(failed, "NON_FINITE_METRIC:"+threshold.Metric)
			continue
		}
		if threshold.Minimum != nil && value < *threshold.Minimum {
			failed = append(failed, "MINIMUM:"+threshold.Metric)
		}
		if threshold.Maximum != nil && value > *threshold.Maximum {
			failed = append(failed, "MAXIMUM:"+threshold.Metric)
		}
	}
	sort.Strings(failed)
	return GateDecision{Passed: len(failed) == 0, FailedGateCodes: failed}
}

func finite(value float64) bool {
	return !math.IsNaN(value) && !math.IsInf(value, 0)
}

package runcontrol

import (
	"context"
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"time"
)

const (
	ProjectMemorySchemaVersion = "project-memory-record.v1"
	ProjectMemoryPolicyV1      = "project-memory-policy.v1"
	MaxMemoryStatementBytes    = 2 << 10
	MaxMemoryValueBytes        = 4 << 10
	MaxMemorySourceRefs        = 20
	MaxMemorySearchLimit       = 50
)

func (value MemoryCandidateStatus) Valid() bool {
	return value == MemoryCandidatePending || value == MemoryCandidateConfirmed || value == MemoryCandidateRejected || value == MemoryCandidateExpired
}

type ProjectMemoryType string

const (
	MemoryDomainTerm         ProjectMemoryType = "DOMAIN_TERM"
	MemoryProjectDecision    ProjectMemoryType = "PROJECT_DECISION"
	MemoryProjectConstraint  ProjectMemoryType = "PROJECT_CONSTRAINT"
	MemoryWorkflowPreference ProjectMemoryType = "WORKFLOW_PREFERENCE"
	MemoryAcceptancePattern  ProjectMemoryType = "ACCEPTANCE_PATTERN"
	MemorySourcePointer      ProjectMemoryType = "SOURCE_POINTER"
	MemoryOpenQuestion       ProjectMemoryType = "OPEN_QUESTION"
	MemoryRiskNote           ProjectMemoryType = "RISK_NOTE"
)

func (value ProjectMemoryType) Valid() bool {
	switch value {
	case MemoryDomainTerm, MemoryProjectDecision, MemoryProjectConstraint,
		MemoryWorkflowPreference, MemoryAcceptancePattern, MemorySourcePointer,
		MemoryOpenQuestion, MemoryRiskNote:
		return true
	default:
		return false
	}
}

type MemoryAuthorityClass string

const (
	MemoryUserConfirmed     MemoryAuthorityClass = "USER_CONFIRMED"
	MemorySourceVerified    MemoryAuthorityClass = "SOURCE_VERIFIED"
	MemoryWorkflowConfirmed MemoryAuthorityClass = "WORKFLOW_CONFIRMED"
	MemoryDerivedProposal   MemoryAuthorityClass = "DERIVED_PROPOSAL"
)

func (value MemoryAuthorityClass) Valid() bool {
	return value == MemoryUserConfirmed || value == MemorySourceVerified ||
		value == MemoryWorkflowConfirmed || value == MemoryDerivedProposal
}

type ProjectMemoryStatus string

const (
	MemoryActive     ProjectMemoryStatus = "ACTIVE"
	MemorySuperseded ProjectMemoryStatus = "SUPERSEDED"
	MemoryRevoked    ProjectMemoryStatus = "REVOKED"
	MemoryStale      ProjectMemoryStatus = "STALE"
)

type MemoryCandidateStatus string

const (
	MemoryCandidatePending   MemoryCandidateStatus = "PENDING_REVIEW"
	MemoryCandidateConfirmed MemoryCandidateStatus = "CONFIRMED"
	MemoryCandidateRejected  MemoryCandidateStatus = "REJECTED"
	MemoryCandidateExpired   MemoryCandidateStatus = "EXPIRED"
)

type MemorySpace struct {
	SpaceID       string    `json:"space_id"`
	TenantID      string    `json:"tenant_id"`
	OwnerID       string    `json:"owner_id"`
	ProjectKey    string    `json:"project_key"`
	DisplayName   string    `json:"display_name"`
	Status        string    `json:"status"`
	MemoryEpoch   int64     `json:"memory_epoch"`
	PolicyVersion string    `json:"policy_version"`
	CreatedAt     time.Time `json:"created_at"`
	UpdatedAt     time.Time `json:"updated_at"`
}

type MemorySourceRef struct {
	SourceKind      string `json:"source_kind"`
	BindingID       string `json:"binding_id,omitempty"`
	SourceID        string `json:"source_id"`
	SourceVersion   string `json:"source_version"`
	Locator         string `json:"locator"`
	ContentHash     string `json:"content_hash"`
	AccessScopeHash string `json:"access_scope_hash,omitempty"`
}

type ProjectMemoryVersion struct {
	SchemaVersion         string               `json:"schema_version"`
	MemoryID              string               `json:"memory_id"`
	SpaceID               string               `json:"space_id"`
	Version               int64                `json:"version"`
	MemoryType            ProjectMemoryType    `json:"memory_type"`
	Subject               string               `json:"subject"`
	Predicate             string               `json:"predicate"`
	Value                 any                  `json:"value"`
	Statement             string               `json:"statement"`
	AuthorityClass        MemoryAuthorityClass `json:"authority_class"`
	Status                ProjectMemoryStatus  `json:"status"`
	Tags                  []string             `json:"tags"`
	Sensitivity           string               `json:"sensitivity"`
	SourceRevisionSetHash string               `json:"source_revision_set_hash"`
	SourceRefs            []MemorySourceRef    `json:"source_refs"`
	ValidFrom             time.Time            `json:"valid_from"`
	ValidUntil            *time.Time           `json:"valid_until,omitempty"`
	CommittedEpoch        int64                `json:"committed_epoch"`
	ContentHash           string               `json:"content_hash"`
	CreatedByKind         string               `json:"created_by_kind"`
	CreatedByRef          string               `json:"created_by_ref"`
	CreatedAt             time.Time            `json:"created_at"`
}

type ProjectMemoryRecord struct {
	MemoryID      string               `json:"memory_id"`
	SpaceID       string               `json:"space_id"`
	SemanticKey   string               `json:"semantic_key"`
	CurrentStatus ProjectMemoryStatus  `json:"current_status"`
	Current       ProjectMemoryVersion `json:"current"`
	CreatedAt     time.Time            `json:"created_at"`
	UpdatedAt     time.Time            `json:"updated_at"`
}

type ProjectMemoryCandidate struct {
	CandidateID      string                `json:"candidate_id"`
	SpaceID          string                `json:"space_id"`
	MemoryType       ProjectMemoryType     `json:"memory_type"`
	Subject          string                `json:"subject"`
	Predicate        string                `json:"predicate"`
	Value            any                   `json:"value"`
	Statement        string                `json:"statement"`
	AuthorityClass   MemoryAuthorityClass  `json:"authority_class"`
	Tags             []string              `json:"tags"`
	Sensitivity      string                `json:"sensitivity"`
	SourceRefs       []MemorySourceRef     `json:"source_refs"`
	ReasonCode       string                `json:"reason_code"`
	ProposedByRunID  string                `json:"proposed_by_run_id,omitempty"`
	ExtractorVersion string                `json:"extractor_version"`
	RequestHash      string                `json:"request_hash"`
	Status           MemoryCandidateStatus `json:"status"`
	CreatedAt        time.Time             `json:"created_at"`
	ReviewedAt       *time.Time            `json:"reviewed_at,omitempty"`
	ReviewedBy       string                `json:"reviewed_by,omitempty"`
}

type ProjectMemoryConflict struct {
	ConflictID         string     `json:"conflict_id"`
	SpaceID            string     `json:"space_id"`
	Subject            string     `json:"subject"`
	Predicate          string     `json:"predicate"`
	MemoryIDs          []string   `json:"memory_ids"`
	Status             string     `json:"status"`
	CommittedEpoch     int64      `json:"committed_epoch"`
	ResolutionMemoryID string     `json:"resolution_memory_id,omitempty"`
	CreatedAt          time.Time  `json:"created_at"`
	ResolvedAt         *time.Time `json:"resolved_at,omitempty"`
	ResolvedEpoch      *int64     `json:"resolved_epoch,omitempty"`
}

type RunMemoryAssignment struct {
	RunID           string    `json:"run_id"`
	SpaceID         string    `json:"space_id"`
	MemoryWatermark int64     `json:"memory_watermark"`
	PolicyVersion   string    `json:"policy_version"`
	AccessScopeHash string    `json:"access_scope_hash"`
	AssignmentHash  string    `json:"assignment_hash"`
	CreatedAt       time.Time `json:"created_at"`
}

type CreateMemorySpaceCommand struct {
	TenantID       string
	OwnerID        string
	ProjectKey     string
	DisplayName    string
	IdempotencyKey string
	Now            time.Time
}

type ProposeMemoryCommand struct {
	TenantID         string
	OwnerID          string
	SpaceID          string
	MemoryType       ProjectMemoryType
	Subject          string
	Predicate        string
	Value            any
	Statement        string
	AuthorityClass   MemoryAuthorityClass
	Tags             []string
	Sensitivity      string
	SourceRefs       []MemorySourceRef
	ReasonCode       string
	ProposedByRunID  string
	ExtractorVersion string
	IdempotencyKey   string
	Now              time.Time
}

type ReviewMemoryCandidateCommand struct {
	TenantID       string
	OwnerID        string
	SpaceID        string
	CandidateID    string
	ExpectedHash   string
	ActorRef       string
	IdempotencyKey string
	Now            time.Time
}

type RevokeMemoryCommand struct {
	TenantID        string
	OwnerID         string
	SpaceID         string
	MemoryID        string
	ExpectedVersion int64
	ActorRef        string
	IdempotencyKey  string
	Now             time.Time
}

type ProjectMemorySearchRequest struct {
	TenantID        string
	OwnerID         string
	SpaceID         string
	Watermark       int64
	AccessScopeHash string
	Query           string
	Operation       string
	MemoryTypes     []ProjectMemoryType
	Tags            []string
	Limit           int
	AsOf            time.Time
}

type ProjectMemorySearchResult struct {
	SpaceID         string                  `json:"space_id"`
	MemoryWatermark int64                   `json:"memory_watermark"`
	Records         []ProjectMemoryVersion  `json:"records"`
	Conflicts       []ProjectMemoryConflict `json:"conflicts"`
	ExcludedCount   int                     `json:"excluded_count"`
	SourceSetHash   string                  `json:"source_set_hash"`
}

type ProjectMemoryStore interface {
	CreateMemorySpace(ctx context.Context, command CreateMemorySpaceCommand) (MemorySpace, error)
	ListMemorySpaces(ctx context.Context, tenantID, ownerID string) ([]MemorySpace, error)
	ProposeProjectMemory(ctx context.Context, command ProposeMemoryCommand) (ProjectMemoryCandidate, error)
	ListProjectMemoryCandidates(ctx context.Context, tenantID, ownerID, spaceID string, status MemoryCandidateStatus) ([]ProjectMemoryCandidate, error)
	ConfirmProjectMemoryCandidate(ctx context.Context, command ReviewMemoryCandidateCommand) (ProjectMemoryRecord, error)
	RejectProjectMemoryCandidate(ctx context.Context, command ReviewMemoryCandidateCommand) (ProjectMemoryCandidate, error)
	RevokeProjectMemory(ctx context.Context, command RevokeMemoryCommand) (ProjectMemoryRecord, error)
	ListProjectMemory(ctx context.Context, tenantID, ownerID, spaceID string, includeInactive bool) ([]ProjectMemoryRecord, error)
	GetProjectMemoryHistory(ctx context.Context, tenantID, ownerID, spaceID, memoryID string) ([]ProjectMemoryVersion, error)
	ListProjectMemoryConflicts(ctx context.Context, tenantID, ownerID, spaceID string, includeResolved bool) ([]ProjectMemoryConflict, error)
	SearchProjectMemory(ctx context.Context, request ProjectMemorySearchRequest) (ProjectMemorySearchResult, error)
}

func DefaultMemorySpaceID(tenantID, ownerID string) string {
	return "memory-space-" + stableHash(tenantID + "\x00" + ownerID + "\x00default")[:24]
}

func BuildRunMemoryAssignment(runID string, space MemorySpace, now time.Time) RunMemoryAssignment {
	accessHash := stableHash(space.TenantID + "\x00" + space.OwnerID + "\x00" + space.SpaceID)
	assignmentHash := stableHash(strings.Join([]string{
		runID, space.SpaceID, fmt.Sprint(space.MemoryEpoch), space.PolicyVersion, accessHash,
	}, "\x00"))
	return RunMemoryAssignment{
		RunID: runID, SpaceID: space.SpaceID, MemoryWatermark: space.MemoryEpoch,
		PolicyVersion: space.PolicyVersion, AccessScopeHash: accessHash,
		AssignmentHash: assignmentHash, CreatedAt: now,
	}
}

var memorySensitivePattern = regexp.MustCompile(`(?i)(authorization\s*:|api[_-]?key|access[_-]?token|refresh[_-]?token|password\s*=|-----BEGIN [A-Z ]*PRIVATE KEY-----)`)

func ValidateMemoryCandidate(command ProposeMemoryCommand) error {
	command.Subject = strings.TrimSpace(command.Subject)
	command.Predicate = strings.TrimSpace(command.Predicate)
	command.Statement = strings.TrimSpace(command.Statement)
	if !command.MemoryType.Valid() || !command.AuthorityClass.Valid() ||
		command.SpaceID == "" || command.Subject == "" || command.Predicate == "" ||
		command.Statement == "" || command.IdempotencyKey == "" {
		return fmt.Errorf("%w: incomplete project memory candidate", ErrInvalidPayload)
	}
	if len(command.Subject) > 240 || len(command.Predicate) > 120 ||
		len([]byte(command.Statement)) > MaxMemoryStatementBytes || len(command.SourceRefs) > MaxMemorySourceRefs {
		return fmt.Errorf("%w: project memory candidate exceeds limits", ErrPayloadTooLarge)
	}
	valueBytes, err := canonicalJSON(command.Value)
	if err != nil || len(valueBytes) > MaxMemoryValueBytes {
		return fmt.Errorf("%w: invalid project memory value", ErrInvalidPayload)
	}
	if memorySensitivePattern.MatchString(command.Statement) || memorySensitivePattern.Match(valueBytes) {
		return ErrSensitivePayload
	}
	if command.AuthorityClass == MemorySourceVerified && len(command.SourceRefs) == 0 {
		return fmt.Errorf("%w: source-verified memory requires provenance", ErrInvalidPayload)
	}
	for _, ref := range command.SourceRefs {
		if ref.SourceKind == "" || ref.SourceID == "" || ref.SourceVersion == "" ||
			ref.Locator == "" || !validSHA256(ref.ContentHash) {
			return fmt.Errorf("%w: invalid project memory source ref", ErrInvalidPayload)
		}
	}
	return nil
}

func MemorySemanticKey(spaceID string, memoryType ProjectMemoryType, subject, predicate string) string {
	return stableHash(strings.Join([]string{
		spaceID,
		string(memoryType),
		strings.ToLower(strings.TrimSpace(subject)),
		strings.ToLower(strings.TrimSpace(predicate)),
	}, "\x00"))
}

func MemoryCandidateRequestHash(command ProposeMemoryCommand) (string, error) {
	payload := map[string]any{
		"space_id": command.SpaceID, "memory_type": command.MemoryType,
		"subject": strings.TrimSpace(command.Subject), "predicate": strings.TrimSpace(command.Predicate),
		"value": command.Value, "statement": strings.TrimSpace(command.Statement),
		"authority_class": command.AuthorityClass, "tags": normalizedMemoryTerms(command.Tags),
		"sensitivity": command.Sensitivity, "source_refs": command.SourceRefs,
		"reason_code": command.ReasonCode, "proposed_by_run_id": command.ProposedByRunID,
		"extractor_version": command.ExtractorVersion,
	}
	value, err := canonicalJSON(payload)
	if err != nil {
		return "", err
	}
	return stableHash(string(value)), nil
}

func memoryVersionContentHash(value ProjectMemoryVersion) (string, error) {
	payload := map[string]any{
		"schema_version": value.SchemaVersion, "memory_id": value.MemoryID,
		"space_id": value.SpaceID, "version": value.Version, "memory_type": value.MemoryType,
		"subject": value.Subject, "predicate": value.Predicate, "value": value.Value,
		"statement": value.Statement, "authority_class": value.AuthorityClass,
		"status": value.Status, "tags": value.Tags, "sensitivity": value.Sensitivity,
		"source_revision_set_hash": value.SourceRevisionSetHash, "source_refs": value.SourceRefs,
		"valid_from": value.ValidFrom.UTC().Format(time.RFC3339Nano), "valid_until": value.ValidUntil,
		"committed_epoch": value.CommittedEpoch, "created_by_kind": value.CreatedByKind,
		"created_by_ref": value.CreatedByRef,
	}
	raw, err := canonicalJSON(payload)
	if err != nil {
		return "", err
	}
	return stableHash(string(raw)), nil
}

func MemoryVersionContentHash(value ProjectMemoryVersion) (string, error) {
	return memoryVersionContentHash(value)
}

func memorySourceSetHash(refs []MemorySourceRef) string {
	raw, _ := canonicalJSON(refs)
	return stableHash(string(raw))
}

func MemorySourceSetHash(refs []MemorySourceRef) string {
	return memorySourceSetHash(refs)
}

func NormalizeMemoryTerms(values []string) []string {
	return normalizedMemoryTerms(values)
}

func normalizedMemoryTerms(values []string) []string {
	seen := map[string]struct{}{}
	output := make([]string, 0, len(values))
	for _, value := range values {
		value = strings.ToLower(strings.TrimSpace(value))
		if value == "" {
			continue
		}
		if _, ok := seen[value]; ok {
			continue
		}
		seen[value] = struct{}{}
		output = append(output, value)
	}
	sort.Strings(output)
	return output
}

func memoryJSONEqual(left, right any) bool {
	l, lerr := json.Marshal(left)
	r, rerr := json.Marshal(right)
	return lerr == nil && rerr == nil && string(l) == string(r)
}

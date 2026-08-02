package runcontrol

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
)

type RunPurpose string

const (
	RunPurposePlanOutline  RunPurpose = "PLAN_OUTLINE"
	RunPurposeGenerateUnit RunPurpose = "GENERATE_UNIT"
	RunPurposeReviseUnit   RunPurpose = "REVISE_UNIT"
	RunPurposeFullReview   RunPurpose = "FULL_REVIEW"
)

func (purpose RunPurpose) Valid() bool {
	switch purpose {
	case RunPurposePlanOutline, RunPurposeGenerateUnit, RunPurposeReviseUnit, RunPurposeFullReview:
		return true
	default:
		return false
	}
}

type RunOutputKind string

const (
	RunOutputOutlineCandidate RunOutputKind = "OUTLINE_CANDIDATE"
	RunOutputUnitCandidate    RunOutputKind = "UNIT_CANDIDATE"
	RunOutputUnitPatch        RunOutputKind = "UNIT_PATCH"
	RunOutputFullReviewReport RunOutputKind = "FULL_REVIEW_REPORT"
)

func outputKindForPurpose(purpose RunPurpose) RunOutputKind {
	switch purpose {
	case RunPurposePlanOutline:
		return RunOutputOutlineCandidate
	case RunPurposeGenerateUnit:
		return RunOutputUnitCandidate
	case RunPurposeReviseUnit:
		return RunOutputUnitPatch
	case RunPurposeFullReview:
		return RunOutputFullReviewReport
	default:
		return ""
	}
}

type OutlineNode struct {
	NodeKey         string   `json:"node_key"`
	ParentKey       string   `json:"parent_key"`
	Ordinal         int      `json:"ordinal"`
	Title           string   `json:"title"`
	Questions       []string `json:"questions"`
	RequiredContent []string `json:"required_content"`
	UnitKey         string   `json:"unit_key"`
}

type OutlineUnit struct {
	UnitKey   string   `json:"unit_key"`
	Title     string   `json:"title"`
	Ordinal   int      `json:"ordinal"`
	NodeKeys  []string `json:"node_keys"`
	DependsOn []string `json:"depends_on"`
}

type OutlineCandidate struct {
	SchemaVersion   string        `json:"schema_version"`
	Title           string        `json:"title"`
	RequirementSize string        `json:"requirement_size"`
	Nodes           []OutlineNode `json:"nodes"`
	Units           []OutlineUnit `json:"units"`
	ContentHash     string        `json:"content_hash,omitempty"`
}

func ParseOutlineCandidate(payload []byte) (OutlineCandidate, error) {
	var candidate OutlineCandidate
	if err := json.Unmarshal(payload, &candidate); err != nil {
		return OutlineCandidate{}, fmt.Errorf("%w: invalid outline candidate", ErrInvalidPayload)
	}
	if err := validateOutlineCandidate(candidate); err != nil {
		return OutlineCandidate{}, err
	}
	canonical, err := canonicalJSON(outlineHashPayload(candidate))
	if err != nil {
		return OutlineCandidate{}, err
	}
	computed := hashBytesHex(canonical)
	if candidate.ContentHash != "" && bareSHA256(candidate.ContentHash) != computed {
		return OutlineCandidate{}, fmt.Errorf("%w: outline candidate content hash mismatch", ErrInvalidPayload)
	}
	candidate.ContentHash = computed
	return candidate, nil
}

func outlineHashPayload(candidate OutlineCandidate) map[string]any {
	nodes := make([]map[string]any, 0, len(candidate.Nodes))
	for _, node := range candidate.Nodes {
		nodes = append(nodes, map[string]any{
			"node_key": node.NodeKey, "parent_key": node.ParentKey, "ordinal": node.Ordinal,
			"title": node.Title, "questions": nonNilStrings(node.Questions),
			"required_content": nonNilStrings(node.RequiredContent), "unit_key": node.UnitKey,
		})
	}
	units := make([]map[string]any, 0, len(candidate.Units))
	for _, unit := range candidate.Units {
		units = append(units, map[string]any{
			"unit_key": unit.UnitKey, "title": unit.Title, "ordinal": unit.Ordinal,
			"node_keys": nonNilStrings(unit.NodeKeys), "depends_on": nonNilStrings(unit.DependsOn),
		})
	}
	return map[string]any{
		"schema_version": candidate.SchemaVersion, "title": candidate.Title,
		"requirement_size": candidate.RequirementSize, "nodes": nodes, "units": units,
	}
}

func validateOutlineCandidate(candidate OutlineCandidate) error {
	if candidate.SchemaVersion != "outline-candidate.v1" || strings.TrimSpace(candidate.Title) == "" || strings.TrimSpace(candidate.RequirementSize) == "" || len(candidate.Nodes) == 0 || len(candidate.Units) == 0 || len(candidate.Units) > 15 {
		return fmt.Errorf("%w: invalid outline identity or size", ErrInvalidPayload)
	}
	nodes := make(map[string]OutlineNode, len(candidate.Nodes))
	units := make(map[string]OutlineUnit, len(candidate.Units))
	for _, unit := range candidate.Units {
		if unit.UnitKey == "" || unit.Title == "" || unit.Ordinal < 0 || len(unit.NodeKeys) == 0 {
			return fmt.Errorf("%w: invalid outline unit", ErrInvalidPayload)
		}
		if _, exists := units[unit.UnitKey]; exists {
			return fmt.Errorf("%w: duplicate outline unit", ErrInvalidPayload)
		}
		units[unit.UnitKey] = unit
	}
	for _, node := range candidate.Nodes {
		if node.NodeKey == "" || node.Title == "" || node.Ordinal < 0 {
			return fmt.Errorf("%w: invalid outline node", ErrInvalidPayload)
		}
		if _, exists := nodes[node.NodeKey]; exists {
			return fmt.Errorf("%w: duplicate outline node", ErrInvalidPayload)
		}
		nodes[node.NodeKey] = node
	}
	assigned := make(map[string]bool, len(nodes))
	for _, node := range candidate.Nodes {
		if _, exists := units[node.UnitKey]; !exists {
			return fmt.Errorf("%w: outline node has missing unit", ErrInvalidPayload)
		}
		depth, parent, seen := 1, node.ParentKey, map[string]bool{node.NodeKey: true}
		for parent != "" {
			parentNode, exists := nodes[parent]
			if !exists || seen[parent] || depth >= 3 {
				return fmt.Errorf("%w: outline parent is missing, cyclic, or too deep", ErrInvalidPayload)
			}
			seen[parent], depth, parent = true, depth+1, parentNode.ParentKey
		}
	}
	for _, unit := range candidate.Units {
		for _, key := range unit.NodeKeys {
			node, exists := nodes[key]
			if !exists || node.UnitKey != unit.UnitKey || assigned[key] {
				return fmt.Errorf("%w: invalid outline node assignment", ErrInvalidPayload)
			}
			assigned[key] = true
		}
		for _, dependency := range unit.DependsOn {
			if _, exists := units[dependency]; !exists || dependency == unit.UnitKey {
				return fmt.Errorf("%w: invalid outline dependency", ErrInvalidPayload)
			}
		}
	}
	if len(assigned) != len(nodes) {
		return fmt.Errorf("%w: every outline node must be assigned once", ErrInvalidPayload)
	}
	visiting, visited := map[string]bool{}, map[string]bool{}
	var visit func(string) error
	visit = func(key string) error {
		if visiting[key] {
			return fmt.Errorf("%w: outline dependency cycle", ErrInvalidPayload)
		}
		if visited[key] {
			return nil
		}
		visiting[key] = true
		for _, dependency := range units[key].DependsOn {
			if err := visit(dependency); err != nil {
				return err
			}
		}
		delete(visiting, key)
		visited[key] = true
		return nil
	}
	for key := range units {
		if err := visit(key); err != nil {
			return err
		}
	}
	return nil
}

func nonNilStrings(items []string) []string {
	if items == nil {
		return []string{}
	}
	return items
}

type ConfirmedUnitContext struct {
	UnitKey         string `json:"unit_key"`
	UnitVersion     int64  `json:"unit_version"`
	ContentHash     string `json:"content_hash"`
	Summary         string `json:"summary,omitempty"`
	WorkingDraftRef string `json:"working_draft_ref,omitempty"`
	Markdown        string `json:"markdown,omitempty"`
}

type UnitScope struct {
	SchemaVersion      string                 `json:"schema_version"`
	Purpose            RunPurpose             `json:"purpose"`
	OutlineID          string                 `json:"outline_id,omitempty"`
	OutlineVersion     int64                  `json:"outline_version,omitempty"`
	OutlineHash        string                 `json:"outline_hash,omitempty"`
	CurrentUnitKey     string                 `json:"current_unit_key,omitempty"`
	CurrentUnitTitle   string                 `json:"current_unit_title,omitempty"`
	CurrentUnitOrdinal int                    `json:"current_unit_ordinal,omitempty"`
	SectionNodeKeys    []string               `json:"section_node_keys,omitempty"`
	DependencyUnitKeys []string               `json:"dependency_unit_keys,omitempty"`
	ConfirmedContext   []ConfirmedUnitContext `json:"confirmed_context,omitempty"`
	ReopenedUnitKeys   []string               `json:"reopened_unit_keys,omitempty"`
	ImmutableUnitKeys  []string               `json:"immutable_unit_keys,omitempty"`
	RequirementRef     string                 `json:"requirement_brief_ref,omitempty"`
	RequirementHash    string                 `json:"requirement_brief_hash,omitempty"`
	BaseUnitHash       string                 `json:"base_unit_hash,omitempty"`
	UserFeedback       string                 `json:"user_feedback,omitempty"`
	ScopeHash          string                 `json:"scope_hash"`
}

func BuildUnitScope(scope UnitScope) (UnitScope, error) {
	normalized := scope
	if normalized.SchemaVersion == "" {
		normalized.SchemaVersion = "unit-scope.v1"
	}
	normalized.OutlineHash = bareSHA256(normalized.OutlineHash)
	normalized.RequirementHash = bareSHA256(normalized.RequirementHash)
	normalized.BaseUnitHash = bareSHA256(normalized.BaseUnitHash)
	normalized.UserFeedback = strings.TrimSpace(normalized.UserFeedback)
	normalized.SectionNodeKeys = uniqueStrings(normalized.SectionNodeKeys, false)
	normalized.DependencyUnitKeys = uniqueStrings(normalized.DependencyUnitKeys, false)
	normalized.ReopenedUnitKeys = uniqueStrings(normalized.ReopenedUnitKeys, true)
	normalized.ImmutableUnitKeys = uniqueStrings(normalized.ImmutableUnitKeys, true)
	for index := range normalized.ConfirmedContext {
		normalized.ConfirmedContext[index].ContentHash = bareSHA256(normalized.ConfirmedContext[index].ContentHash)
	}
	sort.Slice(normalized.ConfirmedContext, func(i, j int) bool {
		return normalized.ConfirmedContext[i].UnitKey < normalized.ConfirmedContext[j].UnitKey
	})
	normalized.ScopeHash = ""
	if err := normalized.validate(); err != nil {
		return UnitScope{}, err
	}
	payload, err := canonicalJSON(unitScopeHashPayload(normalized))
	if err != nil {
		return UnitScope{}, err
	}
	normalized.ScopeHash = hashBytesHex(payload)
	return normalized, nil
}

func unitScopeHashPayload(scope UnitScope) map[string]any {
	confirmed := make([]map[string]any, 0, len(scope.ConfirmedContext))
	for _, item := range scope.ConfirmedContext {
		confirmed = append(confirmed, map[string]any{
			"unit_key": item.UnitKey, "unit_version": item.UnitVersion,
			"content_hash": item.ContentHash, "summary": item.Summary,
			"working_draft_ref": item.WorkingDraftRef, "markdown": item.Markdown,
		})
	}
	return map[string]any{
		"schema_version":         scope.SchemaVersion,
		"purpose":                scope.Purpose,
		"outline_id":             scope.OutlineID,
		"outline_version":        scope.OutlineVersion,
		"outline_hash":           scope.OutlineHash,
		"current_unit_key":       scope.CurrentUnitKey,
		"current_unit_title":     scope.CurrentUnitTitle,
		"current_unit_ordinal":   scope.CurrentUnitOrdinal,
		"section_node_keys":      scope.SectionNodeKeys,
		"dependency_unit_keys":   scope.DependencyUnitKeys,
		"confirmed_context":      confirmed,
		"reopened_unit_keys":     scope.ReopenedUnitKeys,
		"immutable_unit_keys":    scope.ImmutableUnitKeys,
		"requirement_brief_ref":  scope.RequirementRef,
		"requirement_brief_hash": scope.RequirementHash,
		"base_unit_hash":         scope.BaseUnitHash,
		"user_feedback":          scope.UserFeedback,
	}
}

func canonicalJSON(value any) ([]byte, error) {
	payload, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	var canonical any
	if err := json.Unmarshal(payload, &canonical); err != nil {
		return nil, err
	}
	return json.Marshal(canonical)
}

func (scope UnitScope) validate() error {
	if scope.SchemaVersion != "unit-scope.v1" || !scope.Purpose.Valid() {
		return fmt.Errorf("%w: invalid unit scope schema or purpose", ErrInvalidPayload)
	}
	if scope.Purpose != RunPurposePlanOutline {
		if strings.TrimSpace(scope.OutlineID) == "" || scope.OutlineVersion < 1 || !validSHA256(scope.OutlineHash) {
			return fmt.Errorf("%w: unit scope requires a locked outline", ErrInvalidPayload)
		}
	}
	if scope.Purpose == RunPurposeGenerateUnit || scope.Purpose == RunPurposeReviseUnit {
		if strings.TrimSpace(scope.CurrentUnitKey) == "" || strings.TrimSpace(scope.CurrentUnitTitle) == "" || len(scope.SectionNodeKeys) == 0 {
			return fmt.Errorf("%w: generation scope requires a current unit", ErrInvalidPayload)
		}
	} else if scope.CurrentUnitKey != "" {
		return fmt.Errorf("%w: purpose cannot write a current unit", ErrInvalidPayload)
	}
	if scope.Purpose == RunPurposeReviseUnit {
		if !stringInSlice(scope.CurrentUnitKey, scope.ReopenedUnitKeys) || scope.UserFeedback == "" || !validSHA256(scope.BaseUnitHash) {
			return fmt.Errorf("%w: revision scope requires reopened current unit, base hash and feedback", ErrInvalidPayload)
		}
	}
	for _, key := range scope.ReopenedUnitKeys {
		if stringInSlice(key, scope.ImmutableUnitKeys) {
			return fmt.Errorf("%w: reopened and immutable unit sets overlap", ErrInvalidPayload)
		}
	}
	contextKeys := make(map[string]struct{}, len(scope.ConfirmedContext))
	for _, item := range scope.ConfirmedContext {
		if item.UnitKey == "" || item.UnitVersion < 1 || !validSHA256(item.ContentHash) {
			return fmt.Errorf("%w: invalid confirmed unit context", ErrInvalidPayload)
		}
		if _, exists := contextKeys[item.UnitKey]; exists {
			return fmt.Errorf("%w: duplicate confirmed unit context", ErrInvalidPayload)
		}
		contextKeys[item.UnitKey] = struct{}{}
		if item.Markdown != "" && hashText(item.Markdown) != item.ContentHash {
			return fmt.Errorf("%w: confirmed unit markdown hash mismatch", ErrInvalidPayload)
		}
	}
	for _, dependency := range scope.DependencyUnitKeys {
		if _, ok := contextKeys[dependency]; !ok {
			return fmt.Errorf("%w: dependency is not confirmed", ErrInvalidPayload)
		}
	}
	return nil
}

type RunOutput struct {
	SchemaVersion       string        `json:"schema_version"`
	OutputKey           string        `json:"output_key"`
	OutputKind          RunOutputKind `json:"output_kind"`
	RunPurpose          RunPurpose    `json:"run_purpose"`
	ScopeHash           string        `json:"scope_hash"`
	ExpectedTaskVersion int           `json:"expected_task_version"`
	ContentHash         string        `json:"content_hash"`
	Payload             []byte        `json:"payload"`
}

const MaxRunOutputBytes = 2 * 1024 * 1024

func ValidateRunOutput(scope UnitScope, output RunOutput, expectedTaskVersion int) error {
	if output.SchemaVersion != "run-output.v1" || output.OutputKey == "" {
		return fmt.Errorf("%w: invalid run output identity", ErrInvalidPayload)
	}
	if output.RunPurpose != scope.Purpose || output.OutputKind != outputKindForPurpose(scope.Purpose) {
		return fmt.Errorf("%w: run output kind does not match purpose", ErrInvalidPayload)
	}
	if output.ExpectedTaskVersion != expectedTaskVersion {
		return ErrTaskVersionConflict
	}
	if bareSHA256(output.ScopeHash) != bareSHA256(scope.ScopeHash) {
		return fmt.Errorf("%w: run output scope hash mismatch", ErrInvalidPayload)
	}
	if len(output.Payload) == 0 || len(output.Payload) > MaxRunOutputBytes {
		return ErrPayloadTooLarge
	}
	if !validSHA256(output.ContentHash) || hashBytesHex(output.Payload) != bareSHA256(output.ContentHash) {
		return fmt.Errorf("%w: run output content hash mismatch", ErrInvalidPayload)
	}
	switch output.OutputKind {
	case RunOutputUnitCandidate:
		return validateUnitCandidatePayload(scope, output.Payload)
	case RunOutputUnitPatch:
		return validateUnitPatchPayload(scope, output.Payload)
	case RunOutputFullReviewReport:
		return validateFullReviewPayload(scope, output.Payload)
	case RunOutputOutlineCandidate:
		_, err := ParseOutlineCandidate(output.Payload)
		return err
	default:
		return fmt.Errorf("%w: unsupported run output kind", ErrInvalidPayload)
	}
}

func validateUnitCandidatePayload(scope UnitScope, payload []byte) error {
	var candidate struct {
		SchemaVersion string   `json:"schema_version"`
		UnitKey       string   `json:"unit_key"`
		Title         string   `json:"title"`
		Ordinal       int      `json:"ordinal"`
		NodeKeys      []string `json:"node_keys"`
		Markdown      string   `json:"markdown"`
		ContentHash   string   `json:"content_hash"`
	}
	if err := json.Unmarshal(payload, &candidate); err != nil {
		return fmt.Errorf("%w: invalid unit candidate", ErrInvalidPayload)
	}
	if candidate.SchemaVersion != "unit-candidate.v1" || candidate.UnitKey != scope.CurrentUnitKey || candidate.Title != scope.CurrentUnitTitle || candidate.Ordinal != scope.CurrentUnitOrdinal {
		return fmt.Errorf("%w: unit candidate escaped locked scope", ErrInvalidPayload)
	}
	if !equalStrings(candidate.NodeKeys, scope.SectionNodeKeys) || strings.TrimSpace(candidate.Markdown) == "" || hashText(strings.TrimSpace(candidate.Markdown)) != bareSHA256(candidate.ContentHash) {
		return fmt.Errorf("%w: invalid unit candidate content", ErrInvalidPayload)
	}
	return nil
}

func validateUnitPatchPayload(scope UnitScope, payload []byte) error {
	var patch struct {
		SchemaVersion       string `json:"schema_version"`
		UnitKey             string `json:"unit_key"`
		BaseContentHash     string `json:"base_content_hash"`
		ReplacementMarkdown string `json:"replacement_markdown"`
	}
	if err := json.Unmarshal(payload, &patch); err != nil {
		return fmt.Errorf("%w: invalid unit patch", ErrInvalidPayload)
	}
	if patch.SchemaVersion != "unit-patch.v1" || patch.UnitKey != scope.CurrentUnitKey || bareSHA256(patch.BaseContentHash) != scope.BaseUnitHash || strings.TrimSpace(patch.ReplacementMarkdown) == "" {
		return fmt.Errorf("%w: unit patch escaped locked scope or has stale base", ErrInvalidPayload)
	}
	return nil
}

func validateFullReviewPayload(scope UnitScope, payload []byte) error {
	var report struct {
		SchemaVersion string            `json:"schema_version"`
		OutlineHash   string            `json:"outline_hash"`
		UnitHashes    map[string]string `json:"unit_hashes"`
	}
	if err := json.Unmarshal(payload, &report); err != nil || report.SchemaVersion != "full-review-report.v1" || bareSHA256(report.OutlineHash) != scope.OutlineHash {
		return fmt.Errorf("%w: invalid full review report", ErrInvalidPayload)
	}
	if len(report.UnitHashes) != len(scope.ConfirmedContext) {
		return fmt.Errorf("%w: full review unit hash set mismatch", ErrInvalidPayload)
	}
	for _, item := range scope.ConfirmedContext {
		if bareSHA256(report.UnitHashes[item.UnitKey]) != item.ContentHash {
			return fmt.Errorf("%w: full review unit hash mismatch", ErrInvalidPayload)
		}
	}
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(payload, &raw); err != nil {
		return fmt.Errorf("%w: invalid full review report", ErrInvalidPayload)
	}
	if _, writesContent := raw["replacement_markdown"]; writesContent {
		return fmt.Errorf("%w: full review cannot modify confirmed content", ErrInvalidPayload)
	}
	return nil
}

type PlannedConfirmationUnit struct {
	UnitKey            string
	Ordinal            int
	DependsOn          []string
	ConfirmationStatus string
}

func NextDependencyReadyUnit(units []PlannedConfirmationUnit) (PlannedConfirmationUnit, bool, error) {
	status := make(map[string]string, len(units))
	for _, unit := range units {
		if unit.UnitKey == "" {
			return PlannedConfirmationUnit{}, false, fmt.Errorf("%w: unit key is required", ErrInvalidPayload)
		}
		if _, exists := status[unit.UnitKey]; exists {
			return PlannedConfirmationUnit{}, false, fmt.Errorf("%w: duplicate unit key", ErrInvalidPayload)
		}
		status[unit.UnitKey] = unit.ConfirmationStatus
	}
	ready := make([]PlannedConfirmationUnit, 0)
	for _, unit := range units {
		if unit.ConfirmationStatus != "PENDING" && unit.ConfirmationStatus != "REOPENED" {
			continue
		}
		isReady := true
		for _, dependency := range unit.DependsOn {
			dependencyStatus, exists := status[dependency]
			if !exists {
				return PlannedConfirmationUnit{}, false, fmt.Errorf("%w: missing unit dependency", ErrInvalidPayload)
			}
			if dependencyStatus != "CONFIRMED" {
				isReady = false
			}
		}
		if isReady {
			ready = append(ready, unit)
		}
	}
	if len(ready) == 0 {
		return PlannedConfirmationUnit{}, false, nil
	}
	sort.Slice(ready, func(i, j int) bool {
		if ready[i].Ordinal == ready[j].Ordinal {
			return ready[i].UnitKey < ready[j].UnitKey
		}
		return ready[i].Ordinal < ready[j].Ordinal
	})
	return ready[0], true, nil
}

func uniqueStrings(values []string, sorted bool) []string {
	seen := make(map[string]struct{}, len(values))
	result := make([]string, 0, len(values))
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value == "" {
			continue
		}
		if _, exists := seen[value]; exists {
			continue
		}
		seen[value] = struct{}{}
		result = append(result, value)
	}
	if sorted {
		sort.Strings(result)
	}
	return result
}

func equalStrings(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}

func hashBytesHex(value []byte) string {
	digest := sha256.Sum256(value)
	return hex.EncodeToString(digest[:])
}

func hashText(value string) string { return hashBytesHex([]byte(value)) }

func bareSHA256(value string) string { return strings.TrimPrefix(value, "sha256:") }

func validSHA256(value string) bool {
	raw := bareSHA256(value)
	if len(raw) != 64 {
		return false
	}
	_, err := hex.DecodeString(raw)
	return err == nil
}

var errNoRemoteEffects = errors.New("remote effects are forbidden")
var ErrNoRemoteEffects = errNoRemoteEffects

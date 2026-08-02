package runcontrol

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"
)

func TestBuildUnitScopeRequiresConfirmedDependencies(t *testing.T) {
	_, err := BuildUnitScope(UnitScope{
		Purpose: RunPurposeGenerateUnit, OutlineID: "outline-1", OutlineVersion: 1,
		OutlineHash: hashText("outline"), CurrentUnitKey: "acceptance",
		CurrentUnitTitle: "验收标准", CurrentUnitOrdinal: 20,
		SectionNodeKeys: []string{"acceptance"}, DependencyUnitKeys: []string{"solution"},
	})
	if !errors.Is(err, ErrInvalidPayload) {
		t.Fatalf("expected dependency validation error, got %v", err)
	}
}

func TestParseOutlineCandidateRejectsDependencyCycleAndAcceptsCanonicalHash(t *testing.T) {
	payload, _ := json.Marshal(map[string]any{
		"schema_version": "outline-candidate.v1", "title": "PRD", "requirement_size": "SMALL",
		"nodes": []map[string]any{
			{"node_key": "a", "parent_key": "", "ordinal": 1, "title": "A", "questions": []string{}, "required_content": []string{}, "unit_key": "a"},
			{"node_key": "b", "parent_key": "", "ordinal": 2, "title": "B", "questions": []string{}, "required_content": []string{}, "unit_key": "b"},
		},
		"units": []map[string]any{
			{"unit_key": "a", "title": "A", "ordinal": 1, "node_keys": []string{"a"}, "depends_on": []string{}},
			{"unit_key": "b", "title": "B", "ordinal": 2, "node_keys": []string{"b"}, "depends_on": []string{"a"}},
		},
	})
	candidate, err := ParseOutlineCandidate(payload)
	if err != nil || !validSHA256(candidate.ContentHash) {
		t.Fatalf("valid outline rejected: %+v, err=%v", candidate, err)
	}

	var raw map[string]any
	_ = json.Unmarshal(payload, &raw)
	units := raw["units"].([]any)
	units[0].(map[string]any)["depends_on"] = []string{"b"}
	cyclic, _ := json.Marshal(raw)
	if _, err := ParseOutlineCandidate(cyclic); !errors.Is(err, ErrInvalidPayload) {
		t.Fatalf("expected dependency cycle rejection, got %v", err)
	}
}

func TestBuildUnitScopeHashIsStableAcrossSetOrder(t *testing.T) {
	base := UnitScope{
		Purpose: RunPurposeGenerateUnit, OutlineID: "outline-1", OutlineVersion: 1,
		OutlineHash: hashText("outline"), CurrentUnitKey: "acceptance",
		CurrentUnitTitle: "验收标准", CurrentUnitOrdinal: 20,
		SectionNodeKeys:   []string{"acceptance"},
		ImmutableUnitKeys: []string{"solution", "goal"},
	}
	left, err := BuildUnitScope(base)
	if err != nil {
		t.Fatal(err)
	}
	base.ImmutableUnitKeys = []string{"goal", "solution"}
	right, err := BuildUnitScope(base)
	if err != nil {
		t.Fatal(err)
	}
	if left.ScopeHash != right.ScopeHash {
		t.Fatalf("scope hash changed with set order: %s != %s", left.ScopeHash, right.ScopeHash)
	}
}

func TestUnitScopeHashMatchesPythonCanonicalContract(t *testing.T) {
	scope, err := BuildUnitScope(UnitScope{
		Purpose: RunPurposeGenerateUnit, OutlineID: "outline-1", OutlineVersion: 2,
		OutlineHash: strings.Repeat("a", 64), CurrentUnitKey: "unit-a", CurrentUnitTitle: "背景",
		CurrentUnitOrdinal: 1, SectionNodeKeys: []string{"node-a"},
		RequirementRef: "req://1", RequirementHash: strings.Repeat("b", 64),
	})
	if err != nil {
		t.Fatal(err)
	}
	const pythonScopeHash = "4b9e5dee4c843ffc6d791b7753b96f639b193539643d5713c25114a85a8eaacb"
	if scope.ScopeHash != pythonScopeHash {
		t.Fatalf("scope hash = %s, want Python canonical hash %s", scope.ScopeHash, pythonScopeHash)
	}
}

func TestValidateRunOutputAcceptsCurrentUnitOnly(t *testing.T) {
	scope, err := BuildUnitScope(UnitScope{
		Purpose: RunPurposeGenerateUnit, OutlineID: "outline-1", OutlineVersion: 1,
		OutlineHash: hashText("outline"), CurrentUnitKey: "acceptance",
		CurrentUnitTitle: "验收标准", CurrentUnitOrdinal: 20,
		SectionNodeKeys: []string{"acceptance"},
	})
	if err != nil {
		t.Fatal(err)
	}
	markdown := "# 验收标准\n\n- 前置条件：已登录。"
	payload, _ := json.Marshal(map[string]any{
		"schema_version": "unit-candidate.v1", "unit_key": "acceptance",
		"title": "验收标准", "ordinal": 20, "node_keys": []string{"acceptance"},
		"markdown": markdown, "content_hash": hashText(markdown),
	})
	output := RunOutput{
		SchemaVersion: "run-output.v1", OutputKey: "output-1",
		OutputKind: RunOutputUnitCandidate, RunPurpose: RunPurposeGenerateUnit,
		ScopeHash: scope.ScopeHash, ExpectedTaskVersion: 4,
		ContentHash: hashBytesHex(payload), Payload: payload,
	}
	if err := ValidateRunOutput(scope, output, 4); err != nil {
		t.Fatal(err)
	}

	var escaped map[string]any
	_ = json.Unmarshal(payload, &escaped)
	escaped["unit_key"] = "other"
	payload, _ = json.Marshal(escaped)
	output.Payload, output.ContentHash = payload, hashBytesHex(payload)
	if err := ValidateRunOutput(scope, output, 4); !errors.Is(err, ErrInvalidPayload) {
		t.Fatalf("expected current-unit validation error, got %v", err)
	}
}

func TestValidateRunOutputRejectsPurposeScopeAndTaskVersionMismatch(t *testing.T) {
	scope, err := BuildUnitScope(UnitScope{Purpose: RunPurposePlanOutline})
	if err != nil {
		t.Fatal(err)
	}
	payload := []byte(`{"schema_version":"outline-candidate.v1","title":"PRD","requirement_size":"SMALL","nodes":[{"node_key":"requirements","parent_key":"","ordinal":1,"title":"需求","questions":[],"required_content":[],"unit_key":"requirements"}],"units":[{"unit_key":"requirements","title":"需求","ordinal":1,"node_keys":["requirements"],"depends_on":[]}]}`)
	output := RunOutput{
		SchemaVersion: "run-output.v1", OutputKey: "output-1",
		OutputKind: RunOutputUnitCandidate, RunPurpose: RunPurposeGenerateUnit,
		ScopeHash: scope.ScopeHash, ExpectedTaskVersion: 2,
		ContentHash: hashBytesHex(payload), Payload: payload,
	}
	if err := ValidateRunOutput(scope, output, 1); !errors.Is(err, ErrInvalidPayload) {
		t.Fatalf("expected purpose mismatch before version check, got %v", err)
	}
}

func TestNextDependencyReadyUnitUsesStableOrder(t *testing.T) {
	units := []PlannedConfirmationUnit{
		{UnitKey: "acceptance", Ordinal: 30, DependsOn: []string{"solution"}, ConfirmationStatus: "PENDING"},
		{UnitKey: "solution", Ordinal: 20, DependsOn: []string{"goal"}, ConfirmationStatus: "PENDING"},
		{UnitKey: "goal", Ordinal: 10, ConfirmationStatus: "CONFIRMED"},
	}
	next, ok, err := NextDependencyReadyUnit(units)
	if err != nil || !ok || next.UnitKey != "solution" {
		t.Fatalf("unexpected next unit: %+v, ok=%v, err=%v", next, ok, err)
	}
}

func TestFullReviewCannotWriteConfirmedContent(t *testing.T) {
	markdown := "# Goal"
	scope, err := BuildUnitScope(UnitScope{
		Purpose: RunPurposeFullReview, OutlineID: "outline-1", OutlineVersion: 1,
		OutlineHash:      hashText("outline"),
		ConfirmedContext: []ConfirmedUnitContext{{UnitKey: "goal", UnitVersion: 1, ContentHash: hashText(markdown), Markdown: markdown}},
	})
	if err != nil {
		t.Fatal(err)
	}
	payload, _ := json.Marshal(map[string]any{
		"schema_version": "full-review-report.v1", "outline_hash": scope.OutlineHash,
		"unit_hashes":          map[string]string{"goal": hashText(markdown)},
		"replacement_markdown": "mutated",
	})
	output := RunOutput{
		SchemaVersion: "run-output.v1", OutputKey: "report-1",
		OutputKind: RunOutputFullReviewReport, RunPurpose: RunPurposeFullReview,
		ScopeHash: scope.ScopeHash, ExpectedTaskVersion: 1,
		ContentHash: hashBytesHex(payload), Payload: payload,
	}
	if err := ValidateRunOutput(scope, output, 1); !errors.Is(err, ErrInvalidPayload) {
		t.Fatalf("expected full review write rejection, got %v", err)
	}
}

func TestMemoryStorePersistsUnitScopeAndIdempotentRunOutput(t *testing.T) {
	store := NewMemoryStore(QueuePolicy{DefaultWorkflowVersion: WorkflowVersionV4})
	created, err := store.CreateTaskWithRun(context.Background(), "tenant", "owner", "message", "idem")
	if err != nil {
		t.Fatal(err)
	}
	run, err := store.AcquireRun(context.Background(), created.Run.RunID, "worker", time.Now().UTC(), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	lease := LeaseContext{RunID: run.RunID, LeaseID: run.LeaseID, WorkerID: run.WorkerID, FencingToken: run.FencingToken, ExpiresAt: run.LeaseExpiresAt}
	scope, err := BuildUnitScope(UnitScope{Purpose: RunPurposePlanOutline})
	if err != nil {
		t.Fatal(err)
	}
	if err := store.SetRunUnitScope(context.Background(), "tenant", "owner", run.RunID, 1, scope); err != nil {
		t.Fatal(err)
	}
	input, err := store.GetRunContext(context.Background(), lease)
	if err != nil || input.RunPurpose != RunPurposePlanOutline || input.UnitScope.ScopeHash != scope.ScopeHash {
		t.Fatalf("unit scope was not resumed: %+v, err=%v", input.UnitScope, err)
	}
	payload := []byte(`{"schema_version":"outline-candidate.v1","title":"PRD","requirement_size":"SMALL","nodes":[{"node_key":"requirements","parent_key":"","ordinal":1,"title":"需求","questions":[],"required_content":[],"unit_key":"requirements"}],"units":[{"unit_key":"requirements","title":"需求","ordinal":1,"node_keys":["requirements"],"depends_on":[]}]}`)
	output := RunOutput{
		SchemaVersion: "run-output.v1", OutputKey: "outline-1",
		OutputKind: RunOutputOutlineCandidate, RunPurpose: RunPurposePlanOutline,
		ScopeHash: scope.ScopeHash, ExpectedTaskVersion: 1,
		ContentHash: hashBytesHex(payload), Payload: payload,
	}
	first, err := store.SubmitRunOutput(context.Background(), lease, output)
	if err != nil {
		t.Fatal(err)
	}
	replay, err := store.SubmitRunOutput(context.Background(), lease, output)
	if err != nil || replay != first {
		t.Fatalf("run output replay failed: %+v %+v %v", first, replay, err)
	}
	output.Payload = []byte(`{"schema_version":"outline-candidate.v1","title":"Changed","requirement_size":"SMALL","nodes":[{"node_key":"requirements","parent_key":"","ordinal":1,"title":"需求","questions":[],"required_content":[],"unit_key":"requirements"}],"units":[{"unit_key":"requirements","title":"需求","ordinal":1,"node_keys":["requirements"],"depends_on":[]}]}`)
	output.ContentHash = hashBytesHex(output.Payload)
	if _, err := store.SubmitRunOutput(context.Background(), lease, output); !errors.Is(err, ErrInvalidIdempotency) {
		t.Fatalf("expected output idempotency conflict, got %v", err)
	}
}

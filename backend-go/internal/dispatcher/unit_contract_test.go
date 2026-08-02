package dispatcher

import (
	"testing"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	agentv1 "github.com/dante26979-droid/prd-agent/contracts/gen/go/agent/v1"
)

func TestInputToProtoCarriesRunPurposeAndFrozenUnitScope(t *testing.T) {
	scope, err := runcontrol.BuildUnitScope(runcontrol.UnitScope{
		Purpose: runcontrol.RunPurposeGenerateUnit, OutlineID: "outline-1", OutlineVersion: 1,
		OutlineHash: hashForTest("outline"), CurrentUnitKey: "goal", CurrentUnitTitle: "需求目标",
		CurrentUnitOrdinal: 10, SectionNodeKeys: []string{"goal"},
	})
	if err != nil {
		t.Fatal(err)
	}
	value := inputToProto(runcontrol.AgentRunInput{
		Run:        runcontrol.AgentRun{RunID: "run-1", TaskID: "task-1"},
		RunPurpose: runcontrol.RunPurposeGenerateUnit, UnitScope: scope,
	})
	if value.RunPurpose != agentv1.RunPurpose_RUN_PURPOSE_GENERATE_UNIT || value.UnitScope == nil {
		t.Fatalf("purpose/scope missing from contract: %+v", value)
	}
	if value.UnitScope.ScopeHash != scope.ScopeHash || value.UnitScope.CurrentUnitKey != "goal" {
		t.Fatalf("unexpected unit scope: %+v", value.UnitScope)
	}
}

func TestInputToProtoCarriesDurableShadowEvaluationContext(t *testing.T) {
	value := inputToProto(runcontrol.AgentRunInput{
		Run:                    runcontrol.AgentRun{RunID: "run-shadow", TaskID: "task-shadow"},
		EvaluationMode:         runcontrol.EvaluationShadow,
		AuthoritativeWorkflow:  runcontrol.WorkflowVersionV1,
		ShadowWorkflow:         runcontrol.WorkflowVersionV4,
		CandidatePolicyVersion: "policy-v4",
		AssignmentHash:         "sha256:assignment",
	})
	if value.EvaluationMode != "SHADOW" || value.AuthoritativeWorkflowVersion != string(runcontrol.WorkflowVersionV1) || value.ShadowWorkflowVersion != string(runcontrol.WorkflowVersionV4) {
		t.Fatalf("shadow evaluation context missing from contract: %+v", value)
	}
	if value.CandidatePolicyVersion != "policy-v4" || value.AssignmentHash != "sha256:assignment" {
		t.Fatalf("shadow identity missing from contract: %+v", value)
	}
}

func TestRunOutputFromProtoRejectsUnspecifiedPurpose(t *testing.T) {
	_, err := runOutputFromProto(&agentv1.RunOutput{
		OutputKind: agentv1.RunOutputKind_RUN_OUTPUT_KIND_UNIT_CANDIDATE,
	})
	if err == nil {
		t.Fatal("expected unspecified purpose to be rejected")
	}
}

func hashForTest(value string) string {
	return "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}

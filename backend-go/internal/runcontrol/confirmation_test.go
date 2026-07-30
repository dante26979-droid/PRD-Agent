package runcontrol

import (
	"context"
	"encoding/json"
	"testing"
	"time"
)

func TestConfirmationUnitsConfirmAndScopedReopen(t *testing.T) {
	store, run, lease := newLeasedMemoryRun(t)
	scopeMarkdown := "## 范围\n\n生成结构化草稿。"
	acceptanceMarkdown := "## 验收\n\n支持独立确认。"
	patch, err := json.Marshal(map[string]any{
		"schema_version": "working-draft.v2",
		"task_id":        run.TaskID,
		"run_id":         run.RunID,
		"markdown":       scopeMarkdown + "\n\n" + acceptanceMarkdown,
		"confirmation_units": []map[string]any{
			{
				"unit_key": "scope", "title": "范围", "order": 10,
				"markdown": scopeMarkdown, "content_hash": stableHash(scopeMarkdown),
				"claim_ids": []string{"claim-scope"}, "unknown_ids": []string{}, "depends_on": []string{},
			},
			{
				"unit_key": "acceptance", "title": "验收", "order": 20,
				"markdown": acceptanceMarkdown, "content_hash": stableHash(acceptanceMarkdown),
				"claim_ids": []string{"claim-acceptance"}, "unknown_ids": []string{}, "depends_on": []string{"scope"},
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.SubmitDraft(context.Background(), lease, "draft-v2", 1, patch); err != nil {
		t.Fatal(err)
	}
	units, err := store.ListConfirmationUnits(context.Background(), "tenant", "alice", run.TaskID)
	if err != nil || len(units) != 2 {
		t.Fatalf("unexpected units: %+v %v", units, err)
	}
	if _, err := store.CreatePublishPreview(context.Background(), "tenant", "alice", run.TaskID, "preview-before-confirm", 2, time.Now().UTC()); err != ErrInvalidRunStatus {
		t.Fatalf("publish should wait for confirmations, got %v", err)
	}
	if _, err := store.ConfirmConfirmationUnit(context.Background(), "tenant", "alice", run.TaskID, units[1].UnitVersionID, "confirm-dependent", 2); err != ErrInvalidRunStatus {
		t.Fatalf("dependent unit confirmed before dependency: %v", err)
	}
	first, err := store.ConfirmConfirmationUnit(context.Background(), "tenant", "alice", run.TaskID, units[0].UnitVersionID, "confirm-scope", 2)
	if err != nil || first.TaskVersion != 3 {
		t.Fatalf("first confirmation failed: %+v %v", first, err)
	}
	replay, err := store.ConfirmConfirmationUnit(context.Background(), "tenant", "alice", run.TaskID, units[0].UnitVersionID, "confirm-scope", 2)
	if err != nil || replay.DecisionID != first.DecisionID {
		t.Fatalf("confirmation replay failed: %+v %v", replay, err)
	}
	if _, err := store.ConfirmConfirmationUnit(context.Background(), "tenant", "alice", run.TaskID, units[1].UnitVersionID, "stale", 2); err != ErrTaskVersionConflict {
		t.Fatalf("expected stale task version, got %v", err)
	}
	second, err := store.ConfirmConfirmationUnit(context.Background(), "tenant", "alice", run.TaskID, units[1].UnitVersionID, "confirm-acceptance", 3)
	if err != nil || second.TaskVersion != 4 {
		t.Fatalf("second confirmation failed: %+v %v", second, err)
	}
	if _, err := store.CompleteRun(context.Background(), lease, RunSucceeded, time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
	reopened, err := store.ReopenConfirmationUnit(context.Background(), "tenant", "alice", run.TaskID, units[1].UnitVersionID, "补充异常场景", "reopen-acceptance", 4)
	if err != nil || reopened.Run == nil || reopened.TaskVersion != 5 {
		t.Fatalf("reopen failed: %+v %v", reopened, err)
	}
	revisionRun, err := store.AcquireRun(context.Background(), reopened.Run.RunID, "worker-b", time.Now().UTC(), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	input, err := store.GetRunContext(context.Background(), leaseFromRun(revisionRun))
	if err != nil {
		t.Fatal(err)
	}
	if len(input.RevisionScope.ReopenedUnitKeys) != 1 || input.RevisionScope.ReopenedUnitKeys[0] != "acceptance" {
		t.Fatalf("unexpected revision scope: %+v", input.RevisionScope)
	}
	if len(input.RevisionScope.ImmutableUnitKeys) != 1 || input.RevisionScope.ImmutableUnitKeys[0] != "scope" {
		t.Fatalf("confirmed unit is not immutable: %+v", input.RevisionScope)
	}
	if input.ResumeDraft == nil || len(input.ResumeDraft.Content) == 0 {
		t.Fatal("revision run did not receive the base draft")
	}
}

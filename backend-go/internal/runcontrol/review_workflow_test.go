package runcontrol

import (
	"context"
	"encoding/json"
	"testing"
	"time"
)

func newV4ReviewRun(t *testing.T) (*MemoryStore, TaskWithRun, LeaseContext, UnitScope) {
	t.Helper()
	store := NewMemoryStore(QueuePolicy{
		MaxGlobalRunnable:      4,
		MaxRunnablePerOwner:    1,
		DefaultWorkflowVersion: WorkflowVersionV4,
	})
	created, err := store.CreateTaskWithRun(context.Background(), "tenant", "owner", "write a PRD", "start-v4")
	if err != nil {
		t.Fatal(err)
	}
	run, err := store.AcquireRun(context.Background(), created.Run.RunID, "worker", time.Now().UTC(), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	input, err := store.GetRunContext(context.Background(), leaseFromRun(run))
	if err != nil {
		t.Fatal(err)
	}
	return store, created, leaseFromRun(run), input.UnitScope
}

func outlinePayload(t *testing.T) []byte {
	t.Helper()
	payload, err := json.Marshal(map[string]any{
		"schema_version":   "outline-candidate.v1",
		"title":            "PRD",
		"requirement_size": "SMALL",
		"nodes": []map[string]any{
			{"node_key": "goal", "parent_key": "", "ordinal": 10, "title": "目标", "questions": []string{}, "required_content": []string{"goal"}, "unit_key": "goal"},
			{"node_key": "acceptance", "parent_key": "", "ordinal": 20, "title": "验收标准", "questions": []string{}, "required_content": []string{"precondition", "trigger", "expected_result"}, "unit_key": "acceptance"},
		},
		"units": []map[string]any{
			{"unit_key": "goal", "title": "目标", "ordinal": 10, "node_keys": []string{"goal"}, "depends_on": []string{}},
			{"unit_key": "acceptance", "title": "验收标准", "ordinal": 20, "node_keys": []string{"acceptance"}, "depends_on": []string{"goal"}},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	return payload
}

func TestPlanOutlineOutputMaterializesReviewStateExactlyOnce(t *testing.T) {
	store, created, lease, scope := newV4ReviewRun(t)
	payload := outlinePayload(t)
	output := RunOutput{
		SchemaVersion: "run-output.v1", OutputKey: "outline-output-1",
		OutputKind: RunOutputOutlineCandidate, RunPurpose: RunPurposePlanOutline,
		ScopeHash: scope.ScopeHash, ExpectedTaskVersion: created.Task.Version,
		ContentHash: hashBytesHex(payload), Payload: payload,
	}

	first, err := store.SubmitRunOutput(context.Background(), lease, output)
	if err != nil {
		t.Fatal(err)
	}
	outline, err := store.GetReviewOutline(context.Background(), "tenant", "owner", created.Task.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	task, err := store.GetTask(context.Background(), "tenant", "owner", created.Task.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	if outline.Status != OutlineDraft || outline.SourceRunID != created.Run.RunID || task.Status != string(ReviewOutlineReview) || task.Version != 2 {
		t.Fatalf("outline was not materialized atomically: outline=%+v task=%+v", outline, task)
	}

	replay, err := store.SubmitRunOutput(context.Background(), lease, output)
	if err != nil {
		t.Fatal(err)
	}
	if replay != first || replay.TaskVersion != 2 {
		t.Fatalf("materialization replay changed the transition: first=%+v replay=%+v", first, replay)
	}
}

func materializeOutline(t *testing.T) (*MemoryStore, TaskWithRun, LeaseContext, ReviewOutlineVersion) {
	t.Helper()
	store, created, lease, scope := newV4ReviewRun(t)
	payload := outlinePayload(t)
	_, err := store.SubmitRunOutput(context.Background(), lease, RunOutput{
		SchemaVersion: "run-output.v1", OutputKey: "outline-output-1",
		OutputKind: RunOutputOutlineCandidate, RunPurpose: RunPurposePlanOutline,
		ScopeHash: scope.ScopeHash, ExpectedTaskVersion: created.Task.Version,
		ContentHash: hashBytesHex(payload), Payload: payload,
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.CompleteRun(context.Background(), lease, RunSucceeded, time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
	outline, err := store.GetReviewOutline(context.Background(), "tenant", "owner", created.Task.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	return store, created, lease, outline
}

func TestConfirmOutlineLocksItAndCreatesOnlyFirstDependencyReadyRun(t *testing.T) {
	store, created, _, outline := materializeOutline(t)
	command := ConfirmOutlineCommand{
		TenantID: "tenant", OwnerID: "owner", TaskID: created.Task.TaskID,
		OutlineVersionID: outline.OutlineVersionID, IdempotencyKey: "confirm-outline", ExpectedTaskVersion: 2,
	}

	first, err := store.ConfirmOutline(context.Background(), command)
	if err != nil {
		t.Fatal(err)
	}
	replay, err := store.ConfirmOutline(context.Background(), command)
	if err != nil {
		t.Fatal(err)
	}
	units, err := store.ListReviewUnits(context.Background(), "tenant", "owner", created.Task.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	if first.CreatedRun == nil || first.CreatedRun.RunID != replay.CreatedRun.RunID || first.TaskVersion != 3 {
		t.Fatalf("outline confirmation was not idempotent: first=%+v replay=%+v", first, replay)
	}
	scope, ok := store.unitScopes[first.CreatedRun.RunID]
	if !ok || scope.Purpose != RunPurposeGenerateUnit || scope.CurrentUnitKey != "goal" || len(scope.DependencyUnitKeys) != 0 {
		t.Fatalf("wrong dependency-ready unit scope: %+v", scope)
	}
	if len(units) != 2 || units[0].UnitKey != "goal" || units[1].UnitKey != "acceptance" || units[0].Status != UnitPending {
		t.Fatalf("outline units were not materialized: %+v", units)
	}
	locked, err := store.GetReviewOutline(context.Background(), "tenant", "owner", created.Task.TaskID)
	if err != nil || locked.Status != OutlineLocked || locked.LockedAt == nil {
		t.Fatalf("outline was not locked: %+v err=%v", locked, err)
	}
}

func confirmedOutlineWithFirstRun(t *testing.T) (*MemoryStore, TaskWithRun, ReviewTransition) {
	t.Helper()
	store, created, _, outline := materializeOutline(t)
	transition, err := store.ConfirmOutline(context.Background(), ConfirmOutlineCommand{
		TenantID: "tenant", OwnerID: "owner", TaskID: created.Task.TaskID,
		OutlineVersionID: outline.OutlineVersionID, IdempotencyKey: "confirm-outline", ExpectedTaskVersion: 2,
	})
	if err != nil {
		t.Fatal(err)
	}
	return store, created, transition
}

func TestGenerateUnitOutputMaterializesOnlyCurrentUnitAndWaitsForUser(t *testing.T) {
	store, created, transition := confirmedOutlineWithFirstRun(t)
	run, err := store.AcquireRun(context.Background(), transition.CreatedRun.RunID, "unit-worker", time.Now().UTC(), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	scope := store.unitScopes[run.RunID]
	markdown := "# 目标\n\n定义产品目标。"
	payload, err := json.Marshal(map[string]any{
		"schema_version": "unit-candidate.v1", "unit_key": "goal", "title": "目标", "ordinal": 10,
		"node_keys": []string{"goal"}, "markdown": markdown, "content_hash": hashText(markdown),
		"claim_ids": []string{"claim-goal"}, "unknown_ids": []string{}, "used_fact_ids": []string{},
		"quality_report": map[string]any{"outcome": "PASSED"},
	})
	if err != nil {
		t.Fatal(err)
	}
	receipt, err := store.SubmitRunOutput(context.Background(), leaseFromRun(run), RunOutput{
		SchemaVersion: "run-output.v1", OutputKey: "goal-output-1",
		OutputKind: RunOutputUnitCandidate, RunPurpose: RunPurposeGenerateUnit,
		ScopeHash: scope.ScopeHash, ExpectedTaskVersion: 3,
		ContentHash: hashBytesHex(payload), Payload: payload,
	})
	if err != nil {
		t.Fatal(err)
	}
	units, err := store.ListConfirmationUnits(context.Background(), "tenant", "owner", created.Task.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	task, _ := store.GetTask(context.Background(), "tenant", "owner", created.Task.TaskID)
	runs, _ := store.ListRuns(context.Background(), "tenant", "owner", created.Task.TaskID)
	if receipt.TaskVersion != 4 || task.Status != string(ReviewUnitReview) || len(units) != 1 {
		t.Fatalf("unit output was not materialized for review: receipt=%+v task=%+v units=%+v", receipt, task, units)
	}
	if units[0].UnitKey != "goal" || units[0].Markdown != markdown || units[0].ConfirmationStatus != string(UnitReviewing) || units[0].OutlineVersionID == "" || units[0].SourceRunID != run.RunID {
		t.Fatalf("wrong current unit version: %+v", units[0])
	}
	if len(runs) != 2 {
		t.Fatalf("unit output must not create the next run before confirmation: %+v", runs)
	}
}

func materializeGeneratedUnit(t *testing.T, store *MemoryStore, transition ReviewTransition, expectedTaskVersion int, markdown string) ConfirmationUnitVersion {
	t.Helper()
	run, err := store.AcquireRun(context.Background(), transition.CreatedRun.RunID, "unit-worker", time.Now().UTC(), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	scope := store.unitScopes[run.RunID]
	payload, err := json.Marshal(map[string]any{
		"schema_version": "unit-candidate.v1", "unit_key": scope.CurrentUnitKey,
		"title": scope.CurrentUnitTitle, "ordinal": scope.CurrentUnitOrdinal,
		"node_keys": scope.SectionNodeKeys, "markdown": markdown, "content_hash": hashText(markdown),
		"claim_ids": []string{"claim-" + scope.CurrentUnitKey}, "unknown_ids": []string{},
	})
	if err != nil {
		t.Fatal(err)
	}
	_, err = store.SubmitRunOutput(context.Background(), leaseFromRun(run), RunOutput{
		SchemaVersion: "run-output.v1", OutputKey: scope.CurrentUnitKey + "-output-1",
		OutputKind: RunOutputUnitCandidate, RunPurpose: RunPurposeGenerateUnit,
		ScopeHash: scope.ScopeHash, ExpectedTaskVersion: expectedTaskVersion,
		ContentHash: hashBytesHex(payload), Payload: payload,
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.CompleteRun(context.Background(), leaseFromRun(run), RunSucceeded, time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
	units, err := store.ListConfirmationUnits(context.Background(), "tenant", "owner", run.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	for _, unit := range units {
		if unit.UnitKey == scope.CurrentUnitKey {
			return unit
		}
	}
	t.Fatalf("materialized unit %q not found", scope.CurrentUnitKey)
	return ConfirmationUnitVersion{}
}

func TestUnitConfirmationSequencesNextUnitThenFullReview(t *testing.T) {
	store, created, firstRun := confirmedOutlineWithFirstRun(t)
	goal := materializeGeneratedUnit(t, store, firstRun, 3, "# 目标\n\n定义产品目标。")

	next, err := store.ConfirmConfirmationUnit(context.Background(), "tenant", "owner", created.Task.TaskID, goal.UnitVersionID, "confirm-goal", 4)
	if err != nil {
		t.Fatal(err)
	}
	if next.Run == nil || next.Run.WorkflowVersion != WorkflowVersionV4 {
		t.Fatalf("confirming the first unit did not create a v4 next run: %+v", next)
	}
	nextScope := store.unitScopes[next.Run.RunID]
	if nextScope.Purpose != RunPurposeGenerateUnit || nextScope.CurrentUnitKey != "acceptance" || len(nextScope.ConfirmedContext) != 1 || nextScope.ConfirmedContext[0].UnitKey != "goal" {
		t.Fatalf("next unit did not receive confirmed dependency context: %+v", nextScope)
	}

	acceptance := materializeGeneratedUnit(t, store, ReviewTransition{CreatedRun: next.Run}, 5, "# 验收标准\n\n前置条件：已登录；触发条件：提交；预期结果：成功。")
	fullReview, err := store.ConfirmConfirmationUnit(context.Background(), "tenant", "owner", created.Task.TaskID, acceptance.UnitVersionID, "confirm-acceptance", 6)
	if err != nil {
		t.Fatal(err)
	}
	if fullReview.Run == nil {
		t.Fatal("last unit confirmation did not create a full review run")
	}
	fullScope := store.unitScopes[fullReview.Run.RunID]
	task, _ := store.GetTask(context.Background(), "tenant", "owner", created.Task.TaskID)
	if fullScope.Purpose != RunPurposeFullReview || fullScope.CurrentUnitKey != "" || len(fullScope.ConfirmedContext) != 2 || task.Status != string(ReviewFullReviewRunning) || task.Version != 7 {
		t.Fatalf("full review transition is incomplete: scope=%+v task=%+v", fullScope, task)
	}
}

func completedUnitsWithFullReviewRun(t *testing.T) (*MemoryStore, TaskWithRun, AgentRun) {
	t.Helper()
	store, created, firstRun := confirmedOutlineWithFirstRun(t)
	goal := materializeGeneratedUnit(t, store, firstRun, 3, "# 目标\n\n定义产品目标。")
	next, err := store.ConfirmConfirmationUnit(context.Background(), "tenant", "owner", created.Task.TaskID, goal.UnitVersionID, "confirm-goal", 4)
	if err != nil {
		t.Fatal(err)
	}
	acceptance := materializeGeneratedUnit(t, store, ReviewTransition{CreatedRun: next.Run}, 5, "# 验收标准\n\n前置条件：已登录；触发条件：提交；预期结果：成功。")
	fullReview, err := store.ConfirmConfirmationUnit(context.Background(), "tenant", "owner", created.Task.TaskID, acceptance.UnitVersionID, "confirm-acceptance", 6)
	if err != nil {
		t.Fatal(err)
	}
	return store, created, *fullReview.Run
}

func TestPassedFullReviewBuildsV4PublishDocumentWithoutWorkingDraft(t *testing.T) {
	store, created, fullReviewRun := completedUnitsWithFullReviewRun(t)
	run, err := store.AcquireRun(context.Background(), fullReviewRun.RunID, "review-worker", time.Now().UTC(), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	scope := store.unitScopes[run.RunID]
	unitHashes := make(map[string]string, len(scope.ConfirmedContext))
	for _, unit := range scope.ConfirmedContext {
		unitHashes[unit.UnitKey] = unit.ContentHash
	}
	reportPayload, err := json.Marshal(map[string]any{
		"schema_version": "full-review-report.v1", "outline_hash": scope.OutlineHash,
		"unit_hashes": unitHashes, "outcome": "PASSED", "issues": []map[string]any{},
	})
	if err != nil {
		t.Fatal(err)
	}
	receipt, err := store.SubmitRunOutput(context.Background(), leaseFromRun(run), RunOutput{
		SchemaVersion: "run-output.v1", OutputKey: "full-review-output-1",
		OutputKind: RunOutputFullReviewReport, RunPurpose: RunPurposeFullReview,
		ScopeHash: scope.ScopeHash, ExpectedTaskVersion: 7,
		ContentHash: hashBytesHex(reportPayload), Payload: reportPayload,
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.CompleteRun(context.Background(), leaseFromRun(run), RunSucceeded, time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
	preview, err := store.CreatePublishPreview(context.Background(), "tenant", "owner", created.Task.TaskID, "v4-preview", 8, time.Now().UTC())
	if err != nil {
		t.Fatal(err)
	}
	if receipt.TaskVersion != 8 || preview.ContentHash == "" || preview.DraftVersion != 1 {
		t.Fatalf("full review did not produce a publishable immutable document: receipt=%+v preview=%+v", receipt, preview)
	}
	if _, err := store.ConfirmPublish(context.Background(), "tenant", "owner", created.Task.TaskID, preview.PublishID, preview.ConfirmationToken, "confirm-v4-publish", 8, time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
	jobs, err := store.ClaimPendingPublishes(context.Background(), "publisher", 1, time.Now().UTC(), time.Minute)
	if err != nil || len(jobs) != 1 {
		t.Fatalf("v4 publish document was not claimable: jobs=%+v err=%v", jobs, err)
	}
	want := "# PRD\n\n# 目标\n\n定义产品目标。\n\n# 验收标准\n\n前置条件：已登录；触发条件：提交；预期结果：成功。"
	if string(jobs[0].Content) != want || jobs[0].Record.ContentHash != hashText(want) {
		t.Fatalf("v4 publish aggregation is not deterministic: content=%q record=%+v", jobs[0].Content, jobs[0].Record)
	}
}

func passedFullReview(t *testing.T) (*MemoryStore, TaskWithRun) {
	t.Helper()
	store, created, fullReviewRun := completedUnitsWithFullReviewRun(t)
	run, err := store.AcquireRun(context.Background(), fullReviewRun.RunID, "review-worker", time.Now().UTC(), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	scope := store.unitScopes[run.RunID]
	unitHashes := make(map[string]string, len(scope.ConfirmedContext))
	for _, unit := range scope.ConfirmedContext {
		unitHashes[unit.UnitKey] = unit.ContentHash
	}
	payload, _ := json.Marshal(map[string]any{
		"schema_version": "full-review-report.v1", "outline_hash": scope.OutlineHash,
		"unit_hashes": unitHashes, "outcome": "PASSED", "issues": []map[string]any{},
	})
	_, err = store.SubmitRunOutput(context.Background(), leaseFromRun(run), RunOutput{
		SchemaVersion: "run-output.v1", OutputKey: "full-review-output-1",
		OutputKind: RunOutputFullReviewReport, RunPurpose: RunPurposeFullReview,
		ScopeHash: scope.ScopeHash, ExpectedTaskVersion: 7,
		ContentHash: hashBytesHex(payload), Payload: payload,
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.CompleteRun(context.Background(), leaseFromRun(run), RunSucceeded, time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
	return store, created
}

func TestReviewProjectionOwnsV4PublishReadinessAndDocumentAggregation(t *testing.T) {
	store, created := passedFullReview(t)

	view, err := store.GetReviewProjection(context.Background(), "tenant", "owner", created.Task.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	if view.WorkflowVersion != WorkflowVersionV4 || view.Outline == nil || view.FullReview == nil {
		t.Fatalf("v4 projection is incomplete: %+v", view)
	}
	if !view.PublishReadiness.Ready || len(view.PublishReadiness.Reasons) != 0 {
		t.Fatalf("passed review is not publish-ready: %+v", view.PublishReadiness)
	}
	want := "# PRD\n\n# 目标\n\n定义产品目标。\n\n# 验收标准\n\n前置条件：已登录；触发条件：提交；预期结果：成功。"
	hasPublish := false
	for _, action := range view.AvailableActions {
		hasPublish = hasPublish || action == "PUBLISH"
	}
	if view.DocumentMarkdown != want || !hasPublish {
		t.Fatalf("projection duplicated or lost workflow policy: %+v", view)
	}
}

func TestReopenLeafUnitInvalidatesReviewAndRevisionPreservesOtherHashes(t *testing.T) {
	store, created := passedFullReview(t)
	before, err := store.ListConfirmationUnits(context.Background(), "tenant", "owner", created.Task.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	var acceptance ConfirmationUnitVersion
	var goalHash string
	for _, unit := range before {
		if unit.UnitKey == "acceptance" {
			acceptance = unit
		}
		if unit.UnitKey == "goal" {
			goalHash = unit.ContentHash
		}
	}
	reopened, err := store.ReopenConfirmationUnit(context.Background(), "tenant", "owner", created.Task.TaskID, acceptance.UnitVersionID, "补充失败路径", "reopen-acceptance", 8)
	if err != nil {
		t.Fatal(err)
	}
	if reopened.Run == nil {
		t.Fatal("reopen did not create a revision run")
	}
	scope := store.unitScopes[reopened.Run.RunID]
	if scope.Purpose != RunPurposeReviseUnit || scope.BaseUnitHash != acceptance.ContentHash || len(scope.ReopenedUnitKeys) != 1 || scope.ReopenedUnitKeys[0] != "acceptance" {
		t.Fatalf("revision scope is not frozen to reopened unit: %+v", scope)
	}
	if _, ok := store.fullReviewReports[created.Task.TaskID]; ok {
		t.Fatal("reopen did not invalidate the previous full review")
	}
	if _, err := store.CreatePublishPreview(context.Background(), "tenant", "owner", created.Task.TaskID, "stale-preview", 9, time.Now().UTC()); err != ErrInvalidRunStatus {
		t.Fatalf("reopened task remained publishable: %v", err)
	}

	run, err := store.AcquireRun(context.Background(), reopened.Run.RunID, "revision-worker", time.Now().UTC(), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	replacement := "# 验收标准\n\n前置条件：已登录；触发条件：提交；预期结果：成功或返回明确错误。"
	patchPayload, _ := json.Marshal(map[string]any{
		"schema_version": "unit-patch.v1", "unit_key": "acceptance",
		"base_content_hash": acceptance.ContentHash, "replacement_markdown": replacement,
		"content_hash": hashText(replacement), "preserved_unknown_ids": []string{}, "used_fact_ids": []string{},
	})
	_, err = store.SubmitRunOutput(context.Background(), leaseFromRun(run), RunOutput{
		SchemaVersion: "run-output.v1", OutputKey: "acceptance-revision-1",
		OutputKind: RunOutputUnitPatch, RunPurpose: RunPurposeReviseUnit,
		ScopeHash: scope.ScopeHash, ExpectedTaskVersion: 9,
		ContentHash: hashBytesHex(patchPayload), Payload: patchPayload,
	})
	if err != nil {
		t.Fatal(err)
	}
	after, _ := store.ListConfirmationUnits(context.Background(), "tenant", "owner", created.Task.TaskID)
	for _, unit := range after {
		if unit.UnitKey == "goal" && unit.ContentHash != goalHash {
			t.Fatalf("revision changed immutable unit hash: before=%s after=%s", goalHash, unit.ContentHash)
		}
		if unit.UnitKey == "acceptance" && (unit.UnitVersionNo != 2 || unit.Markdown != replacement) {
			t.Fatalf("revision was not materialized as a new unit version: %+v", unit)
		}
	}
}

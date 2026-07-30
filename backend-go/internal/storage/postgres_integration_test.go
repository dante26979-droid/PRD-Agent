package storage

import (
	"context"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
)

func TestPostgresStoreCreatesTaskAndInitialAgentRun(t *testing.T) {
	dsn := os.Getenv("PRD_AGENT_TEST_DATABASE_DSN")
	if dsn == "" {
		t.Skip("PRD_AGENT_TEST_DATABASE_DSN is not configured")
	}
	store, err := NewPostgresStore(context.Background(), Config{
		DSN:                 dsn,
		MinConns:            1,
		MaxConns:            2,
		MaxGlobalRunnable:   10,
		MaxRunnablePerOwner: 10,
		RepositoryBindingID: "github:dante26979-droid/PRD-Agent",
		RepositoryRevision:  strings.Repeat("a", 40),
	})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()

	suffix := strconv.FormatInt(time.Now().UnixNano(), 10)
	cachedRevision := strings.Repeat("b", 40)
	if err := store.RecordRepositoryHead(context.Background(), RepositoryHead{
		BindingID:     "github:dante26979-droid/PRD-Agent",
		Repository:    "dante26979-droid/PRD-Agent",
		DefaultBranch: "main",
		Revision:      cachedRevision,
		LastSuccessAt: time.Now().UTC(),
		LastAttemptAt: time.Now().UTC(),
	}); err != nil {
		t.Fatal(err)
	}
	created, err := store.CreateTaskWithRun(
		context.Background(),
		"integration-"+suffix,
		"owner-"+suffix,
		"create a PRD",
		"idempotency-"+suffix,
	)
	if err != nil {
		t.Fatal(err)
	}
	if created.Task.TaskID == "" || created.Run.RunID == "" {
		t.Fatalf("missing durable identifiers: %+v", created)
	}
	if created.Run.TaskID != created.Task.TaskID {
		t.Fatalf("run does not belong to task: %+v", created)
	}
	acquired, err := store.AcquireRun(context.Background(), created.Run.RunID, "repository-worker-"+suffix, time.Now().UTC(), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	input, err := store.GetRunContext(context.Background(), runcontrol.LeaseContext{
		RunID: acquired.RunID, LeaseID: acquired.LeaseID, WorkerID: acquired.WorkerID,
		FencingToken: acquired.FencingToken, ExpiresAt: acquired.LeaseExpiresAt,
	})
	if err != nil {
		t.Fatal(err)
	}
	if input.RepositoryBindingID != "github:dante26979-droid/PRD-Agent" || input.RepositoryRevision != cachedRevision {
		t.Fatalf("latest mirrored repository HEAD was not bound to task: %+v", input)
	}
	if _, err := store.CompleteRun(context.Background(), runcontrol.LeaseContext{
		RunID: acquired.RunID, LeaseID: acquired.LeaseID, WorkerID: acquired.WorkerID,
		FencingToken: acquired.FencingToken, ExpiresAt: acquired.LeaseExpiresAt,
	}, runcontrol.RunSucceeded, time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
}

func TestPostgresStorePromotesWaitingRunWithMissingScheduleCursor(t *testing.T) {
	dsn := os.Getenv("PRD_AGENT_TEST_DATABASE_DSN")
	if dsn == "" {
		t.Skip("PRD_AGENT_TEST_DATABASE_DSN is not configured")
	}
	store, err := NewPostgresStore(context.Background(), Config{
		DSN:                 dsn,
		MinConns:            1,
		MaxConns:            2,
		MaxGlobalRunnable:   1,
		MaxRunnablePerOwner: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()

	suffix := strconv.FormatInt(time.Now().UnixNano(), 10)
	first, err := store.CreateTaskWithRun(
		context.Background(),
		"promotion-tenant-"+suffix,
		"promotion-owner-a-"+suffix,
		"occupy the only queue slot",
		"promotion-first-"+suffix,
	)
	if err != nil {
		t.Fatal(err)
	}
	waiting, err := store.CreateTaskWithRun(
		context.Background(),
		"promotion-tenant-"+suffix,
		"promotion-owner-b-"+suffix,
		"wait for queue capacity",
		"promotion-waiting-"+suffix,
	)
	if err != nil {
		t.Fatal(err)
	}
	if waiting.Run.Status != runcontrol.RunWaitingCapacity {
		t.Fatalf("second run should wait for capacity: %+v", waiting.Run)
	}

	now := time.Now().UTC()
	acquired, err := store.AcquireRun(context.Background(), first.Run.RunID, "promotion-worker-"+suffix, now, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	lease := runcontrol.LeaseContext{
		RunID: acquired.RunID, LeaseID: acquired.LeaseID, WorkerID: acquired.WorkerID,
		FencingToken: acquired.FencingToken, ExpiresAt: acquired.LeaseExpiresAt,
	}
	if _, err := store.CompleteRun(context.Background(), lease, runcontrol.RunSucceeded, now.Add(time.Second)); err != nil {
		t.Fatal(err)
	}

	promoted, err := store.PromoteWaiting(context.Background(), now.Add(2*time.Second))
	if err != nil {
		t.Fatal(err)
	}
	if len(promoted) != 1 || promoted[0].RunID != waiting.Run.RunID || promoted[0].Status != runcontrol.RunQueued {
		t.Fatalf("waiting run was not promoted: %+v", promoted)
	}
}

func TestPostgresFixedWikiPublishLifecycleAndReconciliation(t *testing.T) {
	dsn := os.Getenv("PRD_AGENT_TEST_DATABASE_DSN")
	if dsn == "" {
		t.Skip("PRD_AGENT_TEST_DATABASE_DSN is not configured")
	}
	store, err := NewPostgresStore(context.Background(), Config{
		DSN: dsn, MinConns: 1, MaxConns: 2,
		MaxGlobalRunnable: 10, MaxRunnablePerOwner: 10,
		ConfirmationSecret: "integration-confirmation-secret-32-bytes",
	})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()

	suffix := strconv.FormatInt(time.Now().UnixNano(), 10)
	tenantID, ownerID := "publish-tenant-"+suffix, "publish-owner-"+suffix
	created, err := store.CreateTaskWithRun(context.Background(), tenantID, ownerID, "publish a PRD", "start-"+suffix)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	run, err := store.AcquireRun(context.Background(), created.Run.RunID, "agent-"+suffix, now, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	lease := runcontrol.LeaseContext{
		RunID: run.RunID, LeaseID: run.LeaseID, WorkerID: run.WorkerID,
		FencingToken: run.FencingToken, ExpiresAt: run.LeaseExpiresAt,
	}
	draft := []byte(`{"markdown":"# Integration PRD"}`)
	receipt, err := store.SubmitDraft(context.Background(), lease, "draft-"+suffix, 1, draft)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.CompleteRun(context.Background(), lease, runcontrol.RunSucceeded, now.Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	preview, err := store.CreatePublishPreview(
		context.Background(), tenantID, ownerID, created.Task.TaskID,
		"preview-"+suffix, receipt.TaskVersion, now.Add(2*time.Second),
	)
	if err != nil {
		t.Fatal(err)
	}
	record, err := store.ConfirmPublish(
		context.Background(), tenantID, ownerID, created.Task.TaskID,
		preview.PublishID, preview.ConfirmationToken, "confirm-"+suffix,
		receipt.TaskVersion, now.Add(3*time.Second),
	)
	if err != nil || record.Status != runcontrol.PublishPending {
		t.Fatalf("publish confirmation failed: %+v %v", record, err)
	}
	jobs, err := store.ClaimPendingPublishes(context.Background(), "integration-"+suffix, 1, now.Add(4*time.Second), time.Minute)
	if err != nil || len(jobs) != 1 || jobs[0].ReconcileOnly || string(jobs[0].Content) != string(draft) {
		t.Fatalf("publish claim failed: %+v %v", jobs, err)
	}
	if err := store.CompletePublish(context.Background(), "integration-"+suffix, preview.PublishID, runcontrol.PublishResult{
		Status: runcontrol.PublishReconciling, ErrorCode: "RESULT_UNKNOWN",
	}, now.Add(5*time.Second)); err != nil {
		t.Fatal(err)
	}
	jobs, err = store.ClaimPendingPublishes(context.Background(), "integration-"+suffix, 1, now.Add(16*time.Second), time.Minute)
	if err != nil || len(jobs) != 1 || !jobs[0].ReconcileOnly {
		t.Fatalf("reconciliation claim failed: %+v %v", jobs, err)
	}
	if err := store.CompletePublish(context.Background(), "integration-"+suffix, preview.PublishID, runcontrol.PublishResult{
		Status: runcontrol.PublishSucceeded, SafeURL: "https://example.feishu.cn/wiki/fixed",
		ProviderRevision: "8",
	}, now.Add(17*time.Second)); err != nil {
		t.Fatal(err)
	}
	task, err := store.GetTask(context.Background(), tenantID, ownerID, created.Task.TaskID)
	if err != nil || task.Status != "PUBLISHED" {
		t.Fatalf("task did not reach published state: %+v %v", task, err)
	}
}

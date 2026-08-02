package runcontrol

import (
	"context"
	"testing"
	"time"
)

func TestPublishPayloadRetentionPurgesOnlyExpiredTerminalV4Payload(t *testing.T) {
	store, created := passedFullReview(t)
	now := time.Now().UTC()
	preview, err := store.CreatePublishPreview(context.Background(), "tenant", "owner", created.Task.TaskID, "retention-preview", 8, now)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.ConfirmPublish(context.Background(), "tenant", "owner", created.Task.TaskID, preview.PublishID, preview.ConfirmationToken, "retention-confirm", 8, now); err != nil {
		t.Fatal(err)
	}
	jobs, err := store.ClaimPendingPublishes(context.Background(), "publisher", 1, now, time.Minute)
	if err != nil || len(jobs) != 1 {
		t.Fatalf("claim publish: jobs=%+v err=%v", jobs, err)
	}
	if err := store.CompletePublish(context.Background(), "publisher", preview.PublishID, PublishResult{Status: PublishSucceeded, SafeURL: "https://example.feishu.cn/wiki/fixed", ProviderRevision: "1"}, now.Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	leases, err := store.ClaimExpiredPublishPayloads(context.Background(), "retention", now.Add(23*time.Hour), 1, time.Minute)
	if err != nil || len(leases) != 0 {
		t.Fatalf("payload was claimable before retention elapsed: %+v %v", leases, err)
	}
	leases, err = store.ClaimExpiredPublishPayloads(context.Background(), "retention", now.Add(25*time.Hour), 1, time.Minute)
	if err != nil || len(leases) != 1 {
		t.Fatalf("expired payload was not claimable: %+v %v", leases, err)
	}
	if err := store.PurgePublishPayload(context.Background(), leases[0], now.Add(25*time.Hour)); err != nil {
		t.Fatal(err)
	}
	value := store.publishes[preview.PublishID]
	if len(value.content) != 0 || value.payloadPurgedAt.IsZero() || value.record.ContentHash != preview.ContentHash {
		t.Fatalf("purge did not preserve identity and audit: %+v", value)
	}
	if err := store.PurgePublishPayload(context.Background(), leases[0], now.Add(25*time.Hour)); err != nil {
		t.Fatalf("purge replay is not idempotent: %v", err)
	}
}

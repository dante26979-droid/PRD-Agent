package integration

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/feishu"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
)

type fakePublisher struct {
	publishErr   error
	reconcileErr error
	publishCalls int
}

func (p *fakePublisher) Publish(context.Context, []byte) (feishu.Result, error) {
	p.publishCalls++
	return feishu.Result{}, p.publishErr
}

func (p *fakePublisher) Reconcile(context.Context, []byte) (feishu.Result, error) {
	return feishu.Result{}, p.reconcileErr
}

func TestReconciliationRetryNeverReturnsToWritePath(t *testing.T) {
	client := &fakePublisher{reconcileErr: &feishu.ProviderError{Code: "TEMPORARY", Retryable: true}}
	worker := &Worker{client: client}
	result, err := worker.execute(context.Background(), runcontrol.PublishJob{
		ReconcileOnly: true,
		Content:       []byte("# PRD"),
	})
	if err == nil {
		t.Fatal("expected provider error to remain observable")
	}
	if result.Status != runcontrol.PublishReconciling || result.Retryable {
		t.Fatalf("ambiguous write was downgraded to a replayable state: %+v", result)
	}
	if client.publishCalls != 0 {
		t.Fatal("reconciliation invoked the document write")
	}
}

func TestManualReconciliationFailureRequiresReview(t *testing.T) {
	client := &fakePublisher{reconcileErr: &feishu.ProviderError{Code: "CONTENT_MISMATCH"}}
	worker := &Worker{client: client, now: func() time.Time { return time.Unix(1, 0).UTC() }}
	result, err := worker.execute(context.Background(), runcontrol.PublishJob{
		ReconcileOnly: true,
		Content:       []byte("# PRD"),
	})
	if !errors.As(err, new(*feishu.ProviderError)) {
		t.Fatalf("expected typed provider error, got %v", err)
	}
	if result.Status != runcontrol.PublishManualReview {
		t.Fatalf("expected manual review, got %+v", result)
	}
}

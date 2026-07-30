package integration

import (
	"context"
	"errors"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/feishu"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
)

type Worker struct {
	store    runcontrol.PublishStore
	client   publisher
	workerID string
	now      func() time.Time
}

type publisher interface {
	Publish(context.Context, []byte) (feishu.Result, error)
	Reconcile(context.Context, []byte) (feishu.Result, error)
}

type Report struct {
	Processed   int
	Succeeded   int
	Retryable   int
	Reconciling int
	Manual      int
}

func NewWorker(store runcontrol.PublishStore, client publisher, workerID string) (*Worker, error) {
	if store == nil || client == nil || workerID == "" {
		return nil, errors.New("publish store, Feishu client and worker id are required")
	}
	return &Worker{store: store, client: client, workerID: workerID, now: func() time.Time { return time.Now().UTC() }}, nil
}

func (w *Worker) ProcessOnce(ctx context.Context) (Report, error) {
	jobs, err := w.store.ClaimPendingPublishes(ctx, w.workerID, 1, w.now(), time.Minute)
	if err != nil {
		return Report{}, err
	}
	report := Report{}
	var processingErr error
	for _, job := range jobs {
		report.Processed++
		result, publishErr := w.execute(ctx, job)
		if err := w.store.CompletePublish(ctx, w.workerID, job.Record.PublishID, result, w.now()); err != nil {
			return report, errors.Join(publishErr, err)
		}
		processingErr = errors.Join(processingErr, publishErr)
		switch result.Status {
		case runcontrol.PublishSucceeded:
			report.Succeeded++
		case runcontrol.PublishRetryable:
			report.Retryable++
		case runcontrol.PublishReconciling:
			report.Reconciling++
		case runcontrol.PublishManualReview:
			report.Manual++
		}
	}
	return report, processingErr
}

func (w *Worker) execute(ctx context.Context, job runcontrol.PublishJob) (runcontrol.PublishResult, error) {
	var providerResult feishu.Result
	var err error
	if job.ReconcileOnly {
		providerResult, err = w.client.Reconcile(ctx, job.Content)
	} else {
		providerResult, err = w.client.Publish(ctx, job.Content)
	}
	if err == nil {
		return runcontrol.PublishResult{
			Status: runcontrol.PublishSucceeded, SafeURL: providerResult.SafeURL,
			ProviderRevision: providerResult.ProviderRevision,
		}, nil
	}
	var providerError *feishu.ProviderError
	if !errors.As(err, &providerError) {
		return runcontrol.PublishResult{
			Status: runcontrol.PublishReconciling, ErrorCode: "RESULT_UNKNOWN",
		}, err
	}
	// Once a write result is unknown, every later attempt is read-only
	// reconciliation. A temporary reconciliation error must never downgrade the
	// job to a state that permits the document write to run again.
	if job.ReconcileOnly {
		if providerError.ResultUnknown || providerError.Retryable {
			return runcontrol.PublishResult{
				Status: runcontrol.PublishReconciling, ErrorCode: providerError.Code,
			}, err
		}
		return runcontrol.PublishResult{
			Status: runcontrol.PublishManualReview, ErrorCode: providerError.Code,
		}, err
	}
	if providerError.ResultUnknown {
		return runcontrol.PublishResult{
			Status: runcontrol.PublishReconciling, ErrorCode: providerError.Code,
		}, err
	}
	if providerError.Retryable {
		return runcontrol.PublishResult{
			Status: runcontrol.PublishRetryable, ErrorCode: providerError.Code, Retryable: true,
		}, err
	}
	return runcontrol.PublishResult{
		Status: runcontrol.PublishManualReview, ErrorCode: providerError.Code,
	}, err
}

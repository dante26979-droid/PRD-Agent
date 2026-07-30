package runcontrol

import (
	"context"
	"time"
)

type Store interface {
	Health(ctx context.Context) error
	CreateTaskWithRun(ctx context.Context, tenantID, ownerID, message, idempotencyKey string) (TaskWithRun, error)
	RetryTask(ctx context.Context, tenantID, ownerID, taskID, idempotencyKey string, expectedTaskVersion int) (AgentRun, error)
	ListTasks(ctx context.Context, tenantID, ownerID string, limit int) ([]Task, error)
	GetTask(ctx context.Context, tenantID, ownerID, taskID string) (Task, error)
	AppendTaskEvent(ctx context.Context, tenantID, ownerID, taskID, eventType string, payload []byte) (TaskEvent, error)
	ListTaskEvents(ctx context.Context, tenantID, ownerID, taskID string, afterSequence int64, limit int) ([]TaskEvent, error)
	ListRuns(ctx context.Context, tenantID, ownerID, taskID string) ([]AgentRun, error)
	GetLatestDraft(ctx context.Context, tenantID, ownerID, taskID string) (WorkingDraft, error)
	ListConfirmationUnits(ctx context.Context, tenantID, ownerID, taskID string) ([]ConfirmationUnitVersion, error)
	ConfirmConfirmationUnit(ctx context.Context, tenantID, ownerID, taskID, unitVersionID, idempotencyKey string, expectedTaskVersion int) (ConfirmationMutationResult, error)
	ReopenConfirmationUnit(ctx context.Context, tenantID, ownerID, taskID, unitVersionID, feedback, idempotencyKey string, expectedTaskVersion int) (ConfirmationMutationResult, error)
	ListEvidence(ctx context.Context, tenantID, ownerID, taskID string, limit int) ([]EvidenceRecord, error)
	ListModelAttempts(ctx context.Context, tenantID, ownerID, taskID string, limit int) ([]ModelAttemptRecord, error)
	CreatePublishPreview(ctx context.Context, tenantID, ownerID, taskID, idempotencyKey string, expectedTaskVersion int, now time.Time) (PublishPreviewResult, error)
	ConfirmPublish(ctx context.Context, tenantID, ownerID, taskID, publishID, confirmationToken, idempotencyKey string, expectedTaskVersion int, now time.Time) (PublishRecord, error)
	ListPublishes(ctx context.Context, tenantID, ownerID, taskID string, limit int) ([]PublishRecord, error)
	StopRun(ctx context.Context, tenantID, ownerID, taskID, runID string) (AgentRun, error)
	AcquireRun(ctx context.Context, runID, workerID string, now time.Time, ttl time.Duration) (AgentRun, error)
	Heartbeat(ctx context.Context, lease LeaseContext, now time.Time, ttl time.Duration) (LeaseContext, error)
	CompleteRun(ctx context.Context, lease LeaseContext, status RunStatus, now time.Time) (AgentRun, error)
	PromoteWaiting(ctx context.Context, now time.Time) ([]AgentRun, error)
	ClaimOutbox(ctx context.Context, limit int, now time.Time) ([]OutboxMessage, error)
	MarkOutboxPublished(ctx context.Context, messageID string, now time.Time) error
	MarkOutboxFailed(ctx context.Context, messageID string, nextAttemptAt time.Time) error
}

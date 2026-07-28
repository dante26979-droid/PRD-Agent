package runcontrol

import (
	"context"
	"time"
)

type Store interface {
	Health(ctx context.Context) error
	CreateTaskWithRun(ctx context.Context, tenantID, ownerID, message, idempotencyKey string) (TaskWithRun, error)
	ListTasks(ctx context.Context, tenantID, ownerID string, limit int) ([]Task, error)
	GetTask(ctx context.Context, tenantID, ownerID, taskID string) (Task, error)
	AppendTaskEvent(ctx context.Context, tenantID, ownerID, taskID, eventType string, payload []byte) (TaskEvent, error)
	ListTaskEvents(ctx context.Context, tenantID, ownerID, taskID string, afterSequence int64, limit int) ([]TaskEvent, error)
	ListRuns(ctx context.Context, tenantID, ownerID, taskID string) ([]AgentRun, error)
	StopRun(ctx context.Context, tenantID, ownerID, taskID, runID string) (AgentRun, error)
	AcquireRun(ctx context.Context, runID, workerID string, now time.Time, ttl time.Duration) (AgentRun, error)
	Heartbeat(ctx context.Context, lease LeaseContext, now time.Time, ttl time.Duration) (LeaseContext, error)
	CompleteRun(ctx context.Context, lease LeaseContext, status RunStatus, now time.Time) (AgentRun, error)
	PromoteWaiting(ctx context.Context, now time.Time) ([]AgentRun, error)
	ClaimOutbox(ctx context.Context, limit int, now time.Time) ([]OutboxMessage, error)
	MarkOutboxPublished(ctx context.Context, messageID string, now time.Time) error
	MarkOutboxFailed(ctx context.Context, messageID string, nextAttemptAt time.Time) error
}

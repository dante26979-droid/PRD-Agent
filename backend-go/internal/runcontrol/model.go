package runcontrol

import (
	"context"
	"time"
)

type RunStatus string

const (
	RunWaitingCapacity RunStatus = "WAITING_CAPACITY"
	RunQueued          RunStatus = "QUEUED"
	RunRunning         RunStatus = "RUNNING"
	RunWaitingUser     RunStatus = "WAITING_USER"
	RunSucceeded       RunStatus = "SUCCEEDED"
	RunFailed          RunStatus = "FAILED"
	RunStopping        RunStatus = "STOPPING"
	RunStopped         RunStatus = "STOPPED"
)

func (status RunStatus) Terminal() bool {
	return status == RunSucceeded || status == RunFailed || status == RunStopped
}

type Task struct {
	TaskID    string    `json:"task_id"`
	TenantID  string    `json:"tenant_id"`
	OwnerID   string    `json:"owner_id"`
	Message   string    `json:"message"`
	Status    string    `json:"status"`
	Version   int       `json:"version"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

type AgentRun struct {
	RunID             string    `json:"run_id"`
	TaskID            string    `json:"task_id"`
	TenantID          string    `json:"tenant_id"`
	OwnerID           string    `json:"owner_id"`
	Status            RunStatus `json:"status"`
	QueueSlotAcquired bool      `json:"queue_slot_acquired"`
	AttemptCount      int       `json:"attempt_count"`
	LeaseID           string    `json:"lease_id,omitempty"`
	WorkerID          string    `json:"worker_id,omitempty"`
	FencingToken      int64     `json:"fencing_token"`
	LeaseExpiresAt    time.Time `json:"lease_expires_at,omitempty"`
	CreatedAt         time.Time `json:"created_at"`
	UpdatedAt         time.Time `json:"updated_at"`
}

// LeaseContext is the capability a worker must present for every run mutation.
// The fencing token prevents a worker with an expired lease from writing after
// another worker has taken over the run.
type LeaseContext struct {
	RunID        string
	LeaseID      string
	WorkerID     string
	FencingToken int64
	ExpiresAt    time.Time
}

type TaskWithRun struct {
	Task Task     `json:"task"`
	Run  AgentRun `json:"run"`
}

type QueuePolicy struct {
	MaxGlobalRunnable   int
	MaxRunnablePerOwner int
}

type OutboxMessage struct {
	MessageID   string
	AggregateID string
	EventType   string
	Payload     []byte
	AvailableAt time.Time
	Attempts    int
}

// TaskEvent is the durable, owner-scoped projection consumed by the public
// API and its SSE endpoint. Sequence is monotonic within a task and is the
// reconnect cursor exposed as Last-Event-ID.
type TaskEvent struct {
	EventID    string    `json:"event_id"`
	TaskID     string    `json:"task_id"`
	TenantID   string    `json:"tenant_id"`
	OwnerID    string    `json:"owner_id"`
	Sequence   int64     `json:"sequence"`
	EventType  string    `json:"event_type"`
	Payload    []byte    `json:"payload"`
	OccurredAt time.Time `json:"occurred_at"`
}

type DispatchStatus string

const (
	DispatchStarted   DispatchStatus = "STARTED"
	DispatchRunning   DispatchStatus = "RUNNING"
	DispatchSucceeded DispatchStatus = "SUCCEEDED"
	DispatchFailed    DispatchStatus = "FAILED"
	DispatchUnknown   DispatchStatus = "UNKNOWN"
)

type AgentDispatch struct {
	DispatchID  string         `json:"dispatch_id"`
	RunID       string         `json:"run_id"`
	TenantID    string         `json:"tenant_id"`
	OwnerID     string         `json:"owner_id"`
	WorkerID    string         `json:"worker_id"`
	AttemptNo   int            `json:"attempt_no"`
	RequestHash string         `json:"request_hash"`
	Status      DispatchStatus `json:"status"`
	LastError   string         `json:"last_error,omitempty"`
	StartedAt   time.Time      `json:"started_at"`
	FinishedAt  *time.Time     `json:"finished_at,omitempty"`
}

type AgentRunInput struct {
	Run                 AgentRun
	TaskMessage         string
	WorkflowVersion     string
	Checkpoint          []byte
	CheckpointSequence  int64
	TaskVersion         int
	RepositoryBindingID string
	RepositoryRevision  string
}

type ModelAttempt struct {
	AttemptKey           string
	Operation            string
	PromptVersion        string
	Provider             string
	RequestHash          string
	Status               string
	ResponseMetadataJSON string
	TokenUsageJSON       string
	ErrorCategory        string
}

type EvidenceItem struct {
	SourceType  string
	SourceID    string
	Locator     string
	ExcerptHash string
	Excerpt     string
}

type CheckpointReceipt struct {
	Sequence    int64
	ContentHash string
}

type DraftReceipt struct {
	TaskVersion int
}

type AgentExecutionStore interface {
	Store
	GetRunContext(ctx context.Context, lease LeaseContext) (AgentRunInput, error)
	RecordModelAttempt(ctx context.Context, lease LeaseContext, attempt ModelAttempt) (string, error)
	AppendEvidence(ctx context.Context, lease LeaseContext, items []EvidenceItem) (int, error)
	SaveCheckpoint(ctx context.Context, lease LeaseContext, sequence int64, checkpoint []byte) (CheckpointReceipt, error)
	SubmitDraft(ctx context.Context, lease LeaseContext, draftKey string, expectedTaskVersion int, patch []byte) (DraftReceipt, error)
}

type DispatchStore interface {
	AgentExecutionStore
	ListQueuedRuns(ctx context.Context, limit int) ([]AgentRun, error)
	ListStoppingRuns(ctx context.Context, limit int) ([]AgentRun, error)
	CreateDispatch(ctx context.Context, dispatch AgentDispatch) (AgentDispatch, error)
	UpdateDispatch(ctx context.Context, dispatchID string, status DispatchStatus, lastError string, finishedAt *time.Time) error
	ListDispatches(ctx context.Context, status DispatchStatus, limit int) ([]AgentDispatch, error)
}

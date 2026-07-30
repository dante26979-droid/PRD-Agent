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
	MaxWaitingRuns      int
	ConfirmationSecret  string
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
	DispatchStarted     DispatchStatus = "STARTED"
	DispatchRunning     DispatchStatus = "RUNNING"
	DispatchSucceeded   DispatchStatus = "SUCCEEDED"
	DispatchFailed      DispatchStatus = "FAILED"
	DispatchUnknown     DispatchStatus = "UNKNOWN"
	DispatchQuarantined DispatchStatus = "QUARANTINED"
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
	ResumeEvidence      []EvidenceItem
	ResumeArtifacts     []RunArtifact
	RevisionScope       RevisionScope
	ResumeDraft         *SubmittedDraftReceipt
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

type RunArtifact struct {
	ArtifactKey  string
	ArtifactType string
	Generation   int64
	RequestHash  string
	ContentHash  string
	Content      []byte
}

type RunArtifactReceipt struct {
	ArtifactKey string
	ContentHash string
}

type RevisionScope struct {
	BaseDraftID       string
	BaseDraftHash     string
	ReopenedUnitKeys  []string
	ImmutableUnitKeys []string
	UserFeedback      string
}

type SubmittedDraftReceipt struct {
	DraftKey    string
	ContentHash string
	TaskVersion int
	Content     []byte
}

type CheckpointReceipt struct {
	Sequence    int64
	ContentHash string
}

type DraftReceipt struct {
	TaskVersion int
}

type WorkingDraft struct {
	DraftID     string    `json:"draft_id"`
	TaskID      string    `json:"task_id"`
	RunID       string    `json:"run_id"`
	DraftKey    string    `json:"draft_key"`
	Content     []byte    `json:"-"`
	ContentHash string    `json:"content_hash"`
	TaskVersion int       `json:"task_version"`
	CreatedAt   time.Time `json:"created_at"`
}

type ConfirmationUnitVersion struct {
	UnitID             string    `json:"unit_id"`
	UnitVersionID      string    `json:"unit_version_id"`
	DraftID            string    `json:"draft_id"`
	UnitKey            string    `json:"unit_key"`
	Title              string    `json:"title"`
	Ordinal            int       `json:"order"`
	Markdown           string    `json:"markdown"`
	ContentHash        string    `json:"content_hash"`
	ClaimIDs           []string  `json:"claim_ids"`
	UnknownIDs         []string  `json:"unknown_ids"`
	DependsOn          []string  `json:"depends_on"`
	ConfirmationStatus string    `json:"confirmation_status"`
	CreatedAt          time.Time `json:"created_at"`
}

type ConfirmationMutationResult struct {
	DecisionID  string                  `json:"decision_id"`
	TaskVersion int                     `json:"task_version"`
	Unit        ConfirmationUnitVersion `json:"unit"`
	Run         *AgentRun               `json:"run,omitempty"`
}

type EvidenceRecord struct {
	EvidenceID  string    `json:"evidence_id"`
	RunID       string    `json:"run_id"`
	SourceType  string    `json:"source_type"`
	SourceID    string    `json:"source_id"`
	Locator     string    `json:"locator"`
	ExcerptHash string    `json:"excerpt_hash"`
	Excerpt     string    `json:"-"`
	CreatedAt   time.Time `json:"created_at"`
}

type ModelAttemptRecord struct {
	AttemptID     string    `json:"attempt_id"`
	RunID         string    `json:"run_id"`
	AttemptKey    string    `json:"attempt_key"`
	Operation     string    `json:"operation"`
	PromptVersion string    `json:"prompt_version,omitempty"`
	Provider      string    `json:"provider,omitempty"`
	RequestHash   string    `json:"request_hash"`
	Status        string    `json:"status"`
	ErrorCategory string    `json:"error_category,omitempty"`
	CreatedAt     time.Time `json:"created_at"`
}

type PublishStatus string

const (
	PublishPreview      PublishStatus = "PREVIEW"
	PublishPending      PublishStatus = "PENDING"
	PublishRunning      PublishStatus = "RUNNING"
	PublishSucceeded    PublishStatus = "SUCCEEDED"
	PublishRetryable    PublishStatus = "FAILED_RETRYABLE"
	PublishReconciling  PublishStatus = "RECONCILING"
	PublishManualReview PublishStatus = "MANUAL_REVIEW"
)

type PublishPreviewResult struct {
	PublishID         string    `json:"publish_id"`
	ConfirmationToken string    `json:"confirmation_token"`
	TaskVersion       int       `json:"task_version"`
	DraftVersion      int       `json:"draft_version"`
	ContentHash       string    `json:"content_hash"`
	Target            string    `json:"target"`
	ExpiresAt         time.Time `json:"expires_at"`
}

type PublishRecord struct {
	PublishID        string        `json:"publish_id"`
	TaskID           string        `json:"task_id"`
	Status           PublishStatus `json:"status"`
	TaskVersion      int           `json:"task_version"`
	DraftVersion     int           `json:"draft_version"`
	ContentHash      string        `json:"content_hash"`
	SafeURL          string        `json:"safe_url,omitempty"`
	ProviderRevision string        `json:"provider_revision,omitempty"`
	ErrorCode        string        `json:"error_code,omitempty"`
	Retryable        bool          `json:"retryable"`
	CreatedAt        time.Time     `json:"created_at"`
	UpdatedAt        time.Time     `json:"updated_at"`
}

type PublishJob struct {
	Record        PublishRecord
	TenantID      string
	OwnerID       string
	Content       []byte
	ReconcileOnly bool
}

type PublishResult struct {
	Status           PublishStatus
	SafeURL          string
	ProviderRevision string
	ErrorCode        string
	Retryable        bool
}

type AgentExecutionStore interface {
	Store
	GetRun(ctx context.Context, runID string) (AgentRun, error)
	GetRunContext(ctx context.Context, lease LeaseContext) (AgentRunInput, error)
	HasModelAttempts(ctx context.Context, runID string) (bool, error)
	RecordModelAttempt(ctx context.Context, lease LeaseContext, attempt ModelAttempt) (string, error)
	AppendEvidence(ctx context.Context, lease LeaseContext, items []EvidenceItem) (int, error)
	SaveRunArtifact(ctx context.Context, lease LeaseContext, artifact RunArtifact) (RunArtifactReceipt, error)
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

type PublishStore interface {
	Store
	ClaimPendingPublishes(ctx context.Context, workerID string, limit int, now time.Time, ttl time.Duration) ([]PublishJob, error)
	CompletePublish(ctx context.Context, workerID, publishID string, result PublishResult, now time.Time) error
}

// Package agentpool owns the bounded Go -> Python Agent RPC execution pool.
// It deliberately exposes a small interface so dispatcher tests do not need
// sockets while production uses generated gRPC clients.
package agentpool

import (
	"context"
	"errors"
	"fmt"
	"io"
	"sync"
	"sync/atomic"

	agentv1 "github.com/dante26979-droid/prd-agent/contracts/gen/go/agent/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/proto"
)

var (
	ErrPoolSaturated      = errors.New("agent RPC pool is saturated")
	ErrPoolDraining       = errors.New("agent RPC pool is draining")
	ErrNoWorker           = errors.New("no agent worker is available")
	ErrNoCompatibleWorker = errors.New("no compatible agent worker is available")
)

type EventStream interface {
	Recv() (*agentv1.ExecuteRunResponse, error)
}

type WorkerClient interface {
	Execute(ctx context.Context, request *agentv1.ExecuteRunRequest) (EventStream, error)
}

type EventAcknowledger interface {
	Acknowledge(ctx context.Context, request *agentv1.AcknowledgeEventRequest) (bool, error)
}

type WorkerControlClient interface {
	WorkerClient
	Cancel(ctx context.Context, request *agentv1.CancelRunRequest) (bool, error)
}

type WorkerHealthClient interface {
	Health(context.Context, string) (*agentv1.HealthResponse, error)
}

type ClientSlot struct {
	WorkerID                         string
	Client                           WorkerClient
	MaxInflight                      int
	Close                            func() error
	SupportedWorkflowVersions        []string
	SupportedExecutionLedgerVersions []string
	CapabilitiesUnknown              bool
}

type ExecutionResult struct {
	WorkerID string
	Events   []*agentv1.ExecuteRunResponse
}

type Reservation struct {
	pool     *Pool
	slot     *workerSlot
	released atomic.Bool
}

type Pool struct {
	mu       sync.RWMutex
	slots    []*workerSlot
	draining bool
	active   sync.WaitGroup
	next     atomic.Uint64
}

type workerSlot struct {
	workerID                         string
	client                           WorkerClient
	tokens                           chan struct{}
	close                            func() error
	supportedWorkflowVersions        map[string]struct{}
	supportedExecutionLedgerVersions map[string]struct{}
}

func NewPool(slots []ClientSlot) (*Pool, error) {
	if len(slots) == 0 {
		return nil, ErrNoWorker
	}
	pool := &Pool{slots: make([]*workerSlot, 0, len(slots))}
	for _, slot := range slots {
		if slot.WorkerID == "" || slot.Client == nil {
			return nil, fmt.Errorf("worker id and client are required")
		}
		maxInflight := slot.MaxInflight
		if maxInflight < 1 {
			maxInflight = 1
		}
		versions := slot.SupportedWorkflowVersions
		if len(versions) == 0 && !slot.CapabilitiesUnknown {
			versions = []string{"agent-runtime.v1"}
		}
		supported := make(map[string]struct{}, len(versions))
		for _, version := range versions {
			if version != "" {
				supported[version] = struct{}{}
			}
		}
		ledgerVersions := make(map[string]struct{}, len(slot.SupportedExecutionLedgerVersions))
		for _, version := range slot.SupportedExecutionLedgerVersions {
			if version != "" {
				ledgerVersions[version] = struct{}{}
			}
		}
		pool.slots = append(pool.slots, &workerSlot{
			workerID:                         slot.WorkerID,
			client:                           slot.Client,
			tokens:                           make(chan struct{}, maxInflight),
			close:                            slot.Close,
			supportedWorkflowVersions:        supported,
			supportedExecutionLedgerVersions: ledgerVersions,
		})
	}
	return pool, nil
}

// NewGRPCPool dials each configured Python Worker once and reuses the
// generated gRPC ClientConn for all executions assigned to that Worker.
func NewGRPCPool(ctx context.Context, endpoints []string, maxInflight int, token string) (*Pool, error) {
	if len(endpoints) == 0 {
		return nil, ErrNoWorker
	}
	slots := make([]ClientSlot, 0, len(endpoints))
	for index, endpoint := range endpoints {
		if endpoint == "" {
			return nil, fmt.Errorf("worker endpoint %d is empty", index)
		}
		// Dial is intentionally non-blocking. gRPC manages reconnect/backoff;
		// the pool reports an execution error until the Worker becomes ready.
		conn, err := grpc.DialContext(ctx, endpoint, grpc.WithTransportCredentials(insecure.NewCredentials()))
		if err != nil {
			for _, existing := range slots {
				if existing.Close != nil {
					_ = existing.Close()
				}
			}
			return nil, fmt.Errorf("dial agent worker %s: %w", endpoint, err)
		}
		slots = append(slots, ClientSlot{
			WorkerID:            fmt.Sprintf("worker-%d", index+1),
			Client:              grpcWorkerClient{client: agentv1.NewAgentWorkerServiceClient(conn), token: token},
			MaxInflight:         maxInflight,
			Close:               conn.Close,
			CapabilitiesUnknown: true,
		})
	}
	return NewPool(slots)
}

type grpcWorkerClient struct {
	client agentv1.AgentWorkerServiceClient
	token  string
}

func (c grpcWorkerClient) Execute(ctx context.Context, request *agentv1.ExecuteRunRequest) (EventStream, error) {
	return c.client.ExecuteRun(c.authorize(ctx), request)
}

func (c grpcWorkerClient) Acknowledge(ctx context.Context, request *agentv1.AcknowledgeEventRequest) (bool, error) {
	response, err := c.client.AcknowledgeEvent(c.authorize(ctx), request)
	if err != nil {
		return false, err
	}
	return response.GetAccepted(), nil
}

func (c grpcWorkerClient) Cancel(ctx context.Context, request *agentv1.CancelRunRequest) (bool, error) {
	response, err := c.client.CancelRun(c.authorize(ctx), request)
	if err != nil {
		return false, err
	}
	return response.GetAccepted(), nil
}

func (c grpcWorkerClient) Health(ctx context.Context, workerID string) (*agentv1.HealthResponse, error) {
	return c.client.Health(c.authorize(ctx), &agentv1.HealthRequest{
		Meta:     &agentv1.RequestMeta{ContractVersion: "agent-execution.v2", RequestId: "health-" + workerID, CorrelationId: workerID},
		WorkerId: workerID,
	})
}

func (c grpcWorkerClient) authorize(ctx context.Context) context.Context {
	if c.token == "" {
		return ctx
	}
	return metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+c.token)
}

func (p *Pool) Execute(ctx context.Context, request *agentv1.ExecuteRunRequest) (ExecutionResult, error) {
	if request == nil {
		return ExecutionResult{}, fmt.Errorf("execute request is required")
	}
	reservation, err := p.Reserve(request.WorkerId)
	if err != nil {
		return ExecutionResult{}, err
	}
	defer reservation.Release()
	return reservation.Execute(ctx, request)
}

func (p *Pool) Reserve(workerID string) (*Reservation, error) {
	slot, err := p.acquire(workerID, "", "")
	if err != nil {
		return nil, err
	}
	return &Reservation{pool: p, slot: slot}, nil
}

func (p *Pool) ReserveForWorkflow(workflowVersion string) (*Reservation, error) {
	return p.ReserveForRun(workflowVersion, "")
}

func (p *Pool) ReserveForRun(workflowVersion, executionLedgerVersion string) (*Reservation, error) {
	if workflowVersion == "" {
		return nil, ErrNoCompatibleWorker
	}
	slot, err := p.acquire("", workflowVersion, executionLedgerVersion)
	if err != nil {
		return nil, err
	}
	return &Reservation{pool: p, slot: slot}, nil
}

// RefreshCapabilities makes gRPC slots eligible only after a successful
// Worker Health response. Empty capability fields retain legacy-v1 support.
func (p *Pool) RefreshCapabilities(ctx context.Context) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	var joined error
	for _, slot := range p.slots {
		client, ok := slot.client.(WorkerHealthClient)
		if !ok {
			continue
		}
		response, err := client.Health(ctx, slot.workerID)
		if err != nil {
			slot.supportedWorkflowVersions = map[string]struct{}{}
			slot.supportedExecutionLedgerVersions = map[string]struct{}{}
			joined = errors.Join(joined, fmt.Errorf("health %s: %w", slot.workerID, err))
			continue
		}
		versions := response.GetSupportedWorkflowVersions()
		if len(versions) == 0 {
			versions = []string{"agent-runtime.v1"}
		}
		supported := make(map[string]struct{}, len(versions))
		for _, version := range versions {
			if version != "" {
				supported[version] = struct{}{}
			}
		}
		slot.supportedWorkflowVersions = supported
		ledgers := make(map[string]struct{}, len(response.GetSupportedExecutionLedgerVersions()))
		for _, version := range response.GetSupportedExecutionLedgerVersions() {
			if version != "" {
				ledgers[version] = struct{}{}
			}
		}
		slot.supportedExecutionLedgerVersions = ledgers
	}
	return joined
}

func (r *Reservation) WorkerID() string { return r.slot.workerID }

func (r *Reservation) Execute(ctx context.Context, request *agentv1.ExecuteRunRequest) (ExecutionResult, error) {
	result := ExecutionResult{WorkerID: r.slot.workerID, Events: make([]*agentv1.ExecuteRunResponse, 0, 8)}
	err := r.Stream(ctx, request, func(event *agentv1.ExecuteRunResponse) error {
		result.Events = append(result.Events, event)
		return nil
	})
	if err != nil {
		return ExecutionResult{}, err
	}
	return result, nil
}

// Stream delivers every Worker event to onEvent before reading the next
// frame. A caller can therefore commit and acknowledge durable progress
// incrementally instead of buffering the complete execution in memory.
func (r *Reservation) Stream(ctx context.Context, request *agentv1.ExecuteRunRequest, onEvent func(*agentv1.ExecuteRunResponse) error) error {
	if request == nil {
		return fmt.Errorf("execute request is required")
	}
	if onEvent == nil {
		return fmt.Errorf("event handler is required")
	}

	cloned, ok := proto.Clone(request).(*agentv1.ExecuteRunRequest)
	if !ok {
		return fmt.Errorf("clone execute request")
	}
	cloned.WorkerId = r.slot.workerID
	stream, err := r.slot.client.Execute(ctx, cloned)
	if err != nil {
		return err
	}
	for {
		event, err := stream.Recv()
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err
		}
		if event == nil {
			return fmt.Errorf("agent worker returned nil event")
		}
		if err := onEvent(event); err != nil {
			return err
		}
		if acknowledger, ok := r.slot.client.(EventAcknowledger); ok {
			accepted, err := acknowledger.Acknowledge(ctx, &agentv1.AcknowledgeEventRequest{
				Meta:              cloned.Meta,
				DispatchId:        cloned.DispatchId,
				RunId:             cloned.RunId,
				EventId:           event.EventId,
				EventSequence:     event.EventSequence,
				CommittedSequence: event.EventSequence,
				WorkerId:          r.slot.workerID,
			})
			if err != nil {
				return fmt.Errorf("acknowledge committed Agent event %s: %w", event.EventId, err)
			}
			if !accepted {
				return fmt.Errorf("Agent worker rejected event acknowledgement %s", event.EventId)
			}
		}
	}
}

func (r *Reservation) Release() {
	if r == nil || r.released.Swap(true) {
		return
	}
	r.pool.release(r.slot)
}

func (p *Pool) acquire(workerID, workflowVersion, executionLedgerVersion string) (*workerSlot, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.draining {
		return nil, ErrPoolDraining
	}
	if len(p.slots) == 0 {
		return nil, ErrNoWorker
	}
	start := int(p.next.Add(1) % uint64(len(p.slots)))
	compatible := workflowVersion == "" && executionLedgerVersion == ""
	for offset := 0; offset < len(p.slots); offset++ {
		slot := p.slots[(start+offset)%len(p.slots)]
		if workerID != "" && slot.workerID != workerID {
			continue
		}
		if workflowVersion != "" {
			if _, ok := slot.supportedWorkflowVersions[workflowVersion]; !ok {
				continue
			}
		}
		if executionLedgerVersion != "" {
			if _, ok := slot.supportedExecutionLedgerVersions[executionLedgerVersion]; !ok {
				continue
			}
		}
		compatible = true
		select {
		case slot.tokens <- struct{}{}:
			p.active.Add(1)
			return slot, nil
		default:
		}
	}
	if !compatible {
		return nil, ErrNoCompatibleWorker
	}
	return nil, ErrPoolSaturated
}

func (p *Pool) release(slot *workerSlot) {
	<-slot.tokens
	p.active.Done()
}

func (p *Pool) Drain(ctx context.Context) error {
	p.mu.Lock()
	p.draining = true
	p.mu.Unlock()
	done := make(chan struct{})
	go func() {
		p.active.Wait()
		close(done)
	}()
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (p *Pool) IsDraining() bool {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return p.draining
}

func (p *Pool) Cancel(ctx context.Context, workerID, dispatchID, runID string) error {
	p.mu.RLock()
	defer p.mu.RUnlock()
	for _, slot := range p.slots {
		if slot.workerID != workerID {
			continue
		}
		client, ok := slot.client.(WorkerControlClient)
		if !ok {
			return fmt.Errorf("worker %s does not support cancellation", workerID)
		}
		accepted, err := client.Cancel(ctx, &agentv1.CancelRunRequest{
			Meta:       &agentv1.RequestMeta{ContractVersion: "agent-execution.v2", RequestId: "cancel-" + dispatchID, CorrelationId: runID},
			DispatchId: dispatchID,
			RunId:      runID,
			WorkerId:   workerID,
		})
		if err != nil {
			return err
		}
		if !accepted {
			return ErrNoWorker
		}
		return nil
	}
	return ErrNoWorker
}

func (p *Pool) Close() error {
	if err := p.Drain(context.Background()); err != nil {
		return err
	}
	p.mu.RLock()
	defer p.mu.RUnlock()
	var joined error
	for _, slot := range p.slots {
		if slot.close != nil {
			joined = errors.Join(joined, slot.close())
		}
	}
	return joined
}

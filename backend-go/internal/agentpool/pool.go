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
	"google.golang.org/protobuf/proto"
)

var (
	ErrPoolSaturated = errors.New("agent RPC pool is saturated")
	ErrPoolDraining  = errors.New("agent RPC pool is draining")
	ErrNoWorker      = errors.New("no agent worker is available")
)

type EventStream interface {
	Recv() (*agentv1.ExecuteRunResponse, error)
}

type WorkerClient interface {
	Execute(ctx context.Context, request *agentv1.ExecuteRunRequest) (EventStream, error)
}

type WorkerControlClient interface {
	WorkerClient
	Cancel(ctx context.Context, request *agentv1.CancelRunRequest) (bool, error)
}

type ClientSlot struct {
	WorkerID    string
	Client      WorkerClient
	MaxInflight int
	Close       func() error
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
	workerID string
	client   WorkerClient
	tokens   chan struct{}
	close    func() error
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
		pool.slots = append(pool.slots, &workerSlot{
			workerID: slot.WorkerID,
			client:   slot.Client,
			tokens:   make(chan struct{}, maxInflight),
			close:    slot.Close,
		})
	}
	return pool, nil
}

// NewGRPCPool dials each configured Python Worker once and reuses the
// generated gRPC ClientConn for all executions assigned to that Worker.
func NewGRPCPool(ctx context.Context, endpoints []string, maxInflight int) (*Pool, error) {
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
			WorkerID:    fmt.Sprintf("worker-%d", index+1),
			Client:      grpcWorkerClient{client: agentv1.NewAgentWorkerServiceClient(conn)},
			MaxInflight: maxInflight,
			Close:       conn.Close,
		})
	}
	return NewPool(slots)
}

type grpcWorkerClient struct {
	client agentv1.AgentWorkerServiceClient
}

func (c grpcWorkerClient) Execute(ctx context.Context, request *agentv1.ExecuteRunRequest) (EventStream, error) {
	return c.client.ExecuteRun(ctx, request)
}

func (c grpcWorkerClient) Cancel(ctx context.Context, request *agentv1.CancelRunRequest) (bool, error) {
	response, err := c.client.CancelRun(ctx, request)
	if err != nil {
		return false, err
	}
	return response.GetAccepted(), nil
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
	slot, err := p.acquire(workerID)
	if err != nil {
		return nil, err
	}
	return &Reservation{pool: p, slot: slot}, nil
}

func (r *Reservation) WorkerID() string { return r.slot.workerID }

func (r *Reservation) Execute(ctx context.Context, request *agentv1.ExecuteRunRequest) (ExecutionResult, error) {
	if request == nil {
		return ExecutionResult{}, fmt.Errorf("execute request is required")
	}

	cloned, ok := proto.Clone(request).(*agentv1.ExecuteRunRequest)
	if !ok {
		return ExecutionResult{}, fmt.Errorf("clone execute request")
	}
	cloned.WorkerId = r.slot.workerID
	stream, err := r.slot.client.Execute(ctx, cloned)
	if err != nil {
		return ExecutionResult{}, err
	}
	result := ExecutionResult{WorkerID: r.slot.workerID, Events: make([]*agentv1.ExecuteRunResponse, 0, 8)}
	for {
		event, err := stream.Recv()
		if errors.Is(err, io.EOF) {
			return result, nil
		}
		if err != nil {
			return ExecutionResult{}, err
		}
		if event == nil {
			return ExecutionResult{}, fmt.Errorf("agent worker returned nil event")
		}
		result.Events = append(result.Events, event)
	}
}

func (r *Reservation) Release() {
	if r == nil || r.released.Swap(true) {
		return
	}
	r.pool.release(r.slot)
}

func (p *Pool) acquire(workerID string) (*workerSlot, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.draining {
		return nil, ErrPoolDraining
	}
	if len(p.slots) == 0 {
		return nil, ErrNoWorker
	}
	start := int(p.next.Add(1) % uint64(len(p.slots)))
	for offset := 0; offset < len(p.slots); offset++ {
		slot := p.slots[(start+offset)%len(p.slots)]
		if workerID != "" && slot.workerID != workerID {
			continue
		}
		select {
		case slot.tokens <- struct{}{}:
			p.active.Add(1)
			return slot, nil
		default:
		}
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
			Meta:       &agentv1.RequestMeta{ContractVersion: "agent-execution.v1", RequestId: "cancel-" + dispatchID, CorrelationId: runID},
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

package agentpool

import (
	"context"
	"errors"
	"io"
	"sync"
	"testing"
	"time"

	agentv1 "github.com/dante26979-droid/prd-agent/contracts/gen/go/agent/v1"
)

type fakeStream struct {
	events []*agentv1.ExecuteRunResponse
	index  int
	block  <-chan struct{}
}

func (s *fakeStream) Recv() (*agentv1.ExecuteRunResponse, error) {
	if s.block != nil {
		<-s.block
		s.block = nil
	}
	if s.index == len(s.events) {
		return nil, io.EOF
	}
	event := s.events[s.index]
	s.index++
	return event, nil
}

type fakeClient struct {
	mu       sync.Mutex
	started  chan struct{}
	block    <-chan struct{}
	events   []*agentv1.ExecuteRunResponse
	requests int
}

type cancellableClient struct {
	fakeClient
	cancelled bool
}

func (c *cancellableClient) Cancel(context.Context, *agentv1.CancelRunRequest) (bool, error) {
	c.cancelled = true
	return true, nil
}

func (c *fakeClient) Execute(_ context.Context, _ *agentv1.ExecuteRunRequest) (EventStream, error) {
	c.mu.Lock()
	c.requests++
	if c.started != nil {
		select {
		case <-c.started:
		default:
			close(c.started)
		}
	}
	c.mu.Unlock()
	return &fakeStream{events: c.events, block: c.block}, nil
}

func TestPoolExecutesThroughWorkerSlot(t *testing.T) {
	client := &fakeClient{events: []*agentv1.ExecuteRunResponse{{EventType: "RUN_STARTED"}, {EventType: "RUN_COMPLETED"}}}
	pool, err := NewPool([]ClientSlot{{WorkerID: "worker-a", Client: client, MaxInflight: 1}})
	if err != nil {
		t.Fatal(err)
	}
	result, err := pool.Execute(context.Background(), &agentv1.ExecuteRunRequest{DispatchId: "dispatch-1", RunId: "run-1"})
	if err != nil {
		t.Fatal(err)
	}
	if result.WorkerID != "worker-a" || len(result.Events) != 2 || result.Events[1].EventType != "RUN_COMPLETED" {
		t.Fatalf("unexpected execution result: %+v", result)
	}
}

func TestPoolRejectsExecutionWhenWorkerIsAtCapacity(t *testing.T) {
	release := make(chan struct{})
	client := &fakeClient{started: make(chan struct{}), block: release, events: []*agentv1.ExecuteRunResponse{{EventType: "RUN_COMPLETED"}}}
	pool, err := NewPool([]ClientSlot{{WorkerID: "worker-a", Client: client, MaxInflight: 1}})
	if err != nil {
		t.Fatal(err)
	}
	firstDone := make(chan error, 1)
	go func() {
		_, err := pool.Execute(context.Background(), &agentv1.ExecuteRunRequest{DispatchId: "dispatch-1", RunId: "run-1"})
		firstDone <- err
	}()
	select {
	case <-client.started:
	case <-time.After(time.Second):
		t.Fatal("worker did not start")
	}
	if _, err := pool.Execute(context.Background(), &agentv1.ExecuteRunRequest{DispatchId: "dispatch-2", RunId: "run-2"}); !errors.Is(err, ErrPoolSaturated) {
		t.Fatalf("expected pool saturation, got %v", err)
	}
	close(release)
	if err := <-firstDone; err != nil {
		t.Fatal(err)
	}
}

func TestPoolDrainRejectsNewExecutionAndWaitsForActive(t *testing.T) {
	release := make(chan struct{})
	client := &fakeClient{started: make(chan struct{}), block: release, events: []*agentv1.ExecuteRunResponse{{EventType: "RUN_COMPLETED"}}}
	pool, err := NewPool([]ClientSlot{{WorkerID: "worker-a", Client: client, MaxInflight: 1}})
	if err != nil {
		t.Fatal(err)
	}
	firstDone := make(chan error, 1)
	go func() {
		_, err := pool.Execute(context.Background(), &agentv1.ExecuteRunRequest{DispatchId: "dispatch-1", RunId: "run-1"})
		firstDone <- err
	}()
	<-client.started
	drainDone := make(chan error, 1)
	go func() { drainDone <- pool.Drain(context.Background()) }()
	deadline := time.After(time.Second)
	for !pool.IsDraining() {
		select {
		case <-deadline:
			t.Fatal("pool did not enter draining state")
		default:
			time.Sleep(time.Millisecond)
		}
	}
	if _, err := pool.Execute(context.Background(), &agentv1.ExecuteRunRequest{DispatchId: "dispatch-2", RunId: "run-2"}); !errors.Is(err, ErrPoolDraining) {
		t.Fatalf("expected draining error, got %v", err)
	}
	close(release)
	if err := <-firstDone; err != nil {
		t.Fatal(err)
	}
	if err := <-drainDone; err != nil {
		t.Fatal(err)
	}
}

func TestPoolForwardsCancellationToSelectedWorker(t *testing.T) {
	client := &cancellableClient{fakeClient: fakeClient{events: []*agentv1.ExecuteRunResponse{{EventType: "RUN_COMPLETED"}}}}
	pool, err := NewPool([]ClientSlot{{WorkerID: "worker-a", Client: client, MaxInflight: 1}})
	if err != nil {
		t.Fatal(err)
	}
	if err := pool.Cancel(context.Background(), "worker-a", "dispatch-1", "run-1"); err != nil {
		t.Fatal(err)
	}
	if !client.cancelled {
		t.Fatal("worker did not receive cancellation")
	}
}

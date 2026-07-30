package agentpool

import (
	"context"
	"net"
	"sync/atomic"
	"testing"

	agentv1 "github.com/dante26979-droid/prd-agent/contracts/gen/go/agent/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/test/bufconn"
)

type bufconnWorker struct {
	agentv1.UnimplementedAgentWorkerServiceServer
	expectedAuthorization string
	acknowledged          atomic.Int32
}

func (w *bufconnWorker) ExecuteRun(_ *agentv1.ExecuteRunRequest, stream agentv1.AgentWorkerService_ExecuteRunServer) error {
	values := metadata.ValueFromIncomingContext(stream.Context(), "authorization")
	if len(values) != 1 || values[0] != "Bearer service-token-with-at-least-32-bytes" {
		return grpc.Errorf(grpc.Code(grpc.ErrServerStopped), "missing service authorization")
	}
	if err := stream.Send(&agentv1.ExecuteRunResponse{DispatchId: "dispatch-1", RunId: "run-1", EventSequence: 1, EventId: "event-1", EventType: "RUN_STARTED"}); err != nil {
		return err
	}
	return stream.Send(&agentv1.ExecuteRunResponse{DispatchId: "dispatch-1", RunId: "run-1", EventSequence: 2, EventId: "event-2", EventType: "RUN_COMPLETED", ResultType: "SUCCEEDED"})
}

func (w *bufconnWorker) AcknowledgeEvent(ctx context.Context, request *agentv1.AcknowledgeEventRequest) (*agentv1.AcknowledgeEventResponse, error) {
	values := metadata.ValueFromIncomingContext(ctx, "authorization")
	if len(values) != 1 || values[0] != w.expectedAuthorization {
		return nil, grpc.Errorf(grpc.Code(grpc.ErrServerStopped), "missing service authorization")
	}
	if request.EventId == "" || request.CommittedSequence != request.EventSequence {
		return &agentv1.AcknowledgeEventResponse{Accepted: false}, nil
	}
	w.acknowledged.Add(1)
	return &agentv1.AcknowledgeEventResponse{Accepted: true}, nil
}

func TestPoolUsesGeneratedGRPCClientForStreamingExecution(t *testing.T) {
	listener := bufconn.Listen(1024 * 1024)
	server := grpc.NewServer()
	worker := &bufconnWorker{expectedAuthorization: "Bearer service-token-with-at-least-32-bytes"}
	agentv1.RegisterAgentWorkerServiceServer(server, worker)
	go func() { _ = server.Serve(listener) }()
	defer server.Stop()

	conn, err := grpc.DialContext(context.Background(), "bufnet", grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) {
		return listener.Dial()
	}), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	pool, err := NewPool([]ClientSlot{{WorkerID: "worker-1", Client: grpcWorkerClient{client: agentv1.NewAgentWorkerServiceClient(conn), token: "service-token-with-at-least-32-bytes"}, MaxInflight: 1}})
	if err != nil {
		t.Fatal(err)
	}
	result, err := pool.Execute(context.Background(), &agentv1.ExecuteRunRequest{DispatchId: "dispatch-1", RunId: "run-1"})
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Events) != 2 || result.Events[1].ResultType != "SUCCEEDED" {
		t.Fatalf("unexpected generated gRPC result: %+v", result.Events)
	}
	if worker.acknowledged.Load() != 2 {
		t.Fatalf("expected an ACK after each committed event, got %d", worker.acknowledged.Load())
	}
}

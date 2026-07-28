package agentpool

import (
	"context"
	"net"
	"testing"

	agentv1 "github.com/dante26979-droid/prd-agent/contracts/gen/go/agent/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
)

type bufconnWorker struct {
	agentv1.UnimplementedAgentWorkerServiceServer
}

func (bufconnWorker) ExecuteRun(_ *agentv1.ExecuteRunRequest, stream agentv1.AgentWorkerService_ExecuteRunServer) error {
	if err := stream.Send(&agentv1.ExecuteRunResponse{DispatchId: "dispatch-1", RunId: "run-1", EventSequence: 1, EventId: "event-1", EventType: "RUN_STARTED"}); err != nil {
		return err
	}
	return stream.Send(&agentv1.ExecuteRunResponse{DispatchId: "dispatch-1", RunId: "run-1", EventSequence: 2, EventId: "event-2", EventType: "RUN_COMPLETED", ResultType: "SUCCEEDED"})
}

func TestPoolUsesGeneratedGRPCClientForStreamingExecution(t *testing.T) {
	listener := bufconn.Listen(1024 * 1024)
	server := grpc.NewServer()
	agentv1.RegisterAgentWorkerServiceServer(server, bufconnWorker{})
	go func() { _ = server.Serve(listener) }()
	defer server.Stop()

	conn, err := grpc.DialContext(context.Background(), "bufnet", grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) {
		return listener.Dial()
	}), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	pool, err := NewPool([]ClientSlot{{WorkerID: "worker-1", Client: grpcWorkerClient{client: agentv1.NewAgentWorkerServiceClient(conn)}, MaxInflight: 1}})
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
}

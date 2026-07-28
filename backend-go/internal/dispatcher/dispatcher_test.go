package dispatcher

import (
	"context"
	"errors"
	"io"
	"testing"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/agentpool"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	agentv1 "github.com/dante26979-droid/prd-agent/contracts/gen/go/agent/v1"
)

type stream struct {
	events []*agentv1.ExecuteRunResponse
	err    error
	index  int
}

func (s *stream) Recv() (*agentv1.ExecuteRunResponse, error) {
	if s.index < len(s.events) {
		item := s.events[s.index]
		s.index++
		return item, nil
	}
	if s.err != nil {
		err := s.err
		s.err = nil
		return nil, err
	}
	return nil, io.EOF
}

type worker struct {
	stream agentpool.EventStream
}

func (w worker) Execute(_ context.Context, request *agentv1.ExecuteRunRequest) (agentpool.EventStream, error) {
	if current, ok := w.stream.(*stream); ok {
		for _, event := range current.events {
			event.DispatchId = request.DispatchId
		}
	}
	return w.stream, nil
}

type replayWorker struct {
	runID string
}

func (w replayWorker) Execute(_ context.Context, request *agentv1.ExecuteRunRequest) (agentpool.EventStream, error) {
	return &stream{events: []*agentv1.ExecuteRunResponse{
		{DispatchId: request.DispatchId, RunId: w.runID, EventSequence: 1, EventId: request.DispatchId + "-1", EventType: "RUN_STARTED"},
		{DispatchId: request.DispatchId, RunId: w.runID, EventSequence: 2, EventId: request.DispatchId + "-2", EventType: "RUN_COMPLETED", ResultType: "SUCCEEDED"},
	}}, nil
}

func TestDispatcherCompletesRunAndPersistsStreamedAgentProgress(t *testing.T) {
	store := runcontrol.NewMemoryStore(runcontrol.QueuePolicy{MaxGlobalRunnable: 10, MaxRunnablePerOwner: 1})
	created, err := store.CreateTaskWithRun(context.Background(), "tenant", "alice", "write a PRD", "start")
	if err != nil {
		t.Fatal(err)
	}
	events := []*agentv1.ExecuteRunResponse{
		{DispatchId: "placeholder", RunId: created.Run.RunID, EventSequence: 1, EventId: "e-1", EventType: "RUN_STARTED"},
		{DispatchId: "placeholder", RunId: created.Run.RunID, EventSequence: 2, EventId: "e-2", EventType: "MODEL_ATTEMPT", ModelAttempt: &agentv1.ModelAttemptEvent{AttemptKey: "attempt-1", Operation: "draft", RequestHash: "hash-1", Status: "SUCCEEDED"}},
		{DispatchId: "placeholder", RunId: created.Run.RunID, EventSequence: 3, EventId: "e-3", EventType: "EVIDENCE_APPENDED", EvidenceItems: []*agentv1.EvidenceItem{{SourceType: "github", SourceId: "repo-1", Locator: "README.md:1", ExcerptHash: "excerpt-1"}}},
		{DispatchId: "placeholder", RunId: created.Run.RunID, EventSequence: 4, EventId: "e-4", EventType: "CHECKPOINT_SAVED", CheckpointSequence: 1, Checkpoint: []byte(`{"node":"outline"}`)},
		{DispatchId: "placeholder", RunId: created.Run.RunID, EventSequence: 5, EventId: "e-5", EventType: "DRAFT_SUBMITTED", DraftKey: "draft-1", ExpectedTaskVersion: 1, DraftPatch: []byte(`{"title":"PRD"}`)},
		{DispatchId: "placeholder", RunId: created.Run.RunID, EventSequence: 6, EventId: "e-6", EventType: "RUN_COMPLETED", ResultType: "SUCCEEDED"},
	}
	pool, err := agentpool.NewPool([]agentpool.ClientSlot{{WorkerID: "worker-a", Client: worker{stream: &stream{events: events}}, MaxInflight: 1}})
	if err != nil {
		t.Fatal(err)
	}
	dispatcher, err := New(store, pool, time.Minute, 10)
	if err != nil {
		t.Fatal(err)
	}
	report, err := dispatcher.DispatchOnce(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if report.Processed != 1 || report.Succeeded != 1 || report.Unknown != 0 {
		t.Fatalf("unexpected dispatch report: %+v", report)
	}
	runs, err := store.ListRuns(context.Background(), "tenant", "alice", created.Task.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	if len(runs) != 1 || runs[0].Status != runcontrol.RunSucceeded {
		t.Fatalf("run did not complete: %+v", runs)
	}
	task, err := store.GetTask(context.Background(), "tenant", "alice", created.Task.TaskID)
	if err != nil || task.Version != 2 {
		t.Fatalf("draft was not applied: %+v %v", task, err)
	}
	dispatches, err := store.ListDispatches(context.Background(), runcontrol.DispatchSucceeded, 10)
	if err != nil || len(dispatches) != 1 {
		t.Fatalf("dispatch was not persisted: %+v %v", dispatches, err)
	}
	projected, err := store.ListTaskEvents(context.Background(), "tenant", "alice", created.Task.TaskID, 1, 20)
	if err != nil || len(projected) != 6 || projected[len(projected)-1].EventType != "agent.RUN_COMPLETED" {
		t.Fatalf("agent events were not projected: %+v %v", projected, err)
	}
}

func TestDispatcherMarksInterruptedStreamUnknown(t *testing.T) {
	store := runcontrol.NewMemoryStore(runcontrol.QueuePolicy{MaxGlobalRunnable: 10, MaxRunnablePerOwner: 1})
	created, err := store.CreateTaskWithRun(context.Background(), "tenant", "alice", "write a PRD", "start")
	if err != nil {
		t.Fatal(err)
	}
	events := []*agentv1.ExecuteRunResponse{{DispatchId: "placeholder", RunId: created.Run.RunID, EventSequence: 1, EventId: "e-1", EventType: "RUN_STARTED"}}
	pool, err := agentpool.NewPool([]agentpool.ClientSlot{{WorkerID: "worker-a", Client: worker{stream: &stream{events: events, err: errors.New("stream reset")}}, MaxInflight: 1}})
	if err != nil {
		t.Fatal(err)
	}
	dispatcher, err := New(store, pool, time.Minute, 10)
	if err != nil {
		t.Fatal(err)
	}
	report, err := dispatcher.DispatchOnce(context.Background())
	if err != nil || report.Unknown != 1 {
		t.Fatalf("expected unknown dispatch, report=%+v err=%v", report, err)
	}
	unknown, err := store.ListDispatches(context.Background(), runcontrol.DispatchUnknown, 10)
	if err != nil || len(unknown) != 1 {
		t.Fatalf("unknown dispatch was not recorded: %+v %v", unknown, err)
	}
}

func TestDispatcherRecoversUnknownAfterLeaseExpiry(t *testing.T) {
	store := runcontrol.NewMemoryStore(runcontrol.QueuePolicy{MaxGlobalRunnable: 10, MaxRunnablePerOwner: 1})
	created, err := store.CreateTaskWithRun(context.Background(), "tenant", "alice", "write a PRD", "start")
	if err != nil {
		t.Fatal(err)
	}
	firstPool, err := agentpool.NewPool([]agentpool.ClientSlot{{WorkerID: "worker-a", Client: worker{stream: &stream{err: errors.New("stream reset")}}, MaxInflight: 1}})
	if err != nil {
		t.Fatal(err)
	}
	dispatcher, err := New(store, firstPool, 10*time.Millisecond, 10)
	if err != nil {
		t.Fatal(err)
	}
	if report, err := dispatcher.DispatchOnce(context.Background()); err != nil || report.Unknown != 1 {
		t.Fatalf("expected initial unknown dispatch: %+v %v", report, err)
	}
	time.Sleep(20 * time.Millisecond)
	recoveryPool, err := agentpool.NewPool([]agentpool.ClientSlot{{WorkerID: "worker-b", Client: replayWorker{runID: created.Run.RunID}, MaxInflight: 1}})
	if err != nil {
		t.Fatal(err)
	}
	recoveryDispatcher, err := New(store, recoveryPool, 10*time.Millisecond, 10)
	if err != nil {
		t.Fatal(err)
	}
	report, err := recoveryDispatcher.RecoverUnknown(context.Background(), 10)
	if err != nil || report.Succeeded != 1 {
		t.Fatalf("expected recovery success: %+v %v", report, err)
	}
	runs, err := store.ListRuns(context.Background(), "tenant", "alice", created.Task.TaskID)
	if err != nil || len(runs) != 1 || runs[0].Status != runcontrol.RunSucceeded {
		t.Fatalf("run was not recovered: %+v %v", runs, err)
	}
}

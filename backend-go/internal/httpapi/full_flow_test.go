package httpapi_test

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/agentpool"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/dispatcher"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/httpapi"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	agentv1 "github.com/dante26979-droid/prd-agent/contracts/gen/go/agent/v1"
)

type completeFlowStream struct {
	events []*agentv1.ExecuteRunResponse
	index  int
}

func (s *completeFlowStream) Recv() (*agentv1.ExecuteRunResponse, error) {
	if s.index >= len(s.events) {
		return nil, io.EOF
	}
	event := s.events[s.index]
	s.index++
	return event, nil
}

type completeFlowWorker struct{}

func (completeFlowWorker) Execute(_ context.Context, request *agentv1.ExecuteRunRequest) (agentpool.EventStream, error) {
	return &completeFlowStream{events: []*agentv1.ExecuteRunResponse{
		{
			DispatchId:    request.DispatchId,
			RunId:         request.RunId,
			EventSequence: 1,
			EventId:       request.DispatchId + "-1",
			EventType:     "RUN_STARTED",
		},
		{
			DispatchId:    request.DispatchId,
			RunId:         request.RunId,
			EventSequence: 2,
			EventId:       request.DispatchId + "-2",
			EventType:     "MODEL_ATTEMPT",
			ModelAttempt: &agentv1.ModelAttemptEvent{
				AttemptKey:  "attempt-1",
				Operation:   "generate_working_draft",
				Provider:    "deterministic",
				RequestHash: "sha256:request",
				Status:      "SUCCEEDED",
			},
		},
		{
			DispatchId:         request.DispatchId,
			RunId:              request.RunId,
			EventSequence:      3,
			EventId:            request.DispatchId + "-3",
			EventType:          "CHECKPOINT_SAVED",
			CheckpointSequence: 1,
			Checkpoint:         []byte(`{"stage":"DRAFTED"}`),
		},
		{
			DispatchId:          request.DispatchId,
			RunId:               request.RunId,
			EventSequence:       4,
			EventId:             request.DispatchId + "-4",
			EventType:           "DRAFT_SUBMITTED",
			DraftKey:            request.RunId + ":draft:1",
			ExpectedTaskVersion: 1,
			DraftPatch:          []byte(`{"markdown":"# PRD Working Draft"}`),
		},
		{
			DispatchId:    request.DispatchId,
			RunId:         request.RunId,
			EventSequence: 5,
			EventId:       request.DispatchId + "-5",
			EventType:     "RUN_COMPLETED",
			ResultType:    "SUCCEEDED",
		},
	}}, nil
}

func TestPublicAPIToAgentDispatcherCompleteFlow(t *testing.T) {
	store := runcontrol.NewMemoryStore(runcontrol.QueuePolicy{
		MaxGlobalRunnable:   10,
		MaxRunnablePerOwner: 1,
	})
	router := httpapi.NewRouter(store)

	createRequest := httptest.NewRequest(
		http.MethodPost,
		"/api/v1/tasks/from-message",
		strings.NewReader(`{"message":"为代码审查助手生成 PRD"}`),
	)
	createRequest.Header.Set("Content-Type", "application/json")
	createRequest.Header.Set("Idempotency-Key", "full-flow-1")
	createResponse := httptest.NewRecorder()
	router.ServeHTTP(createResponse, createRequest)
	if createResponse.Code != http.StatusCreated {
		t.Fatalf("create task: status=%d body=%s", createResponse.Code, createResponse.Body.String())
	}

	var created runcontrol.TaskWithRun
	if err := json.Unmarshal(createResponse.Body.Bytes(), &created); err != nil {
		t.Fatal(err)
	}

	pool, err := agentpool.NewPool([]agentpool.ClientSlot{{
		WorkerID:    "worker-1",
		Client:      completeFlowWorker{},
		MaxInflight: 1,
	}})
	if err != nil {
		t.Fatal(err)
	}
	directDispatcher, err := dispatcher.New(store, pool, time.Minute, 10)
	if err != nil {
		t.Fatal(err)
	}
	report, err := directDispatcher.DispatchOnce(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if report.Processed != 1 || report.Succeeded != 1 {
		t.Fatalf("unexpected dispatch report: %+v", report)
	}

	runsRequest := httptest.NewRequest(
		http.MethodGet,
		"/api/v1/tasks/"+created.Task.TaskID+"/runs",
		nil,
	)
	runsResponse := httptest.NewRecorder()
	router.ServeHTTP(runsResponse, runsRequest)
	if runsResponse.Code != http.StatusOK {
		t.Fatalf("list runs: status=%d body=%s", runsResponse.Code, runsResponse.Body.String())
	}
	var runs struct {
		Items []runcontrol.AgentRun `json:"items"`
	}
	if err := json.Unmarshal(runsResponse.Body.Bytes(), &runs); err != nil {
		t.Fatal(err)
	}
	if len(runs.Items) != 1 || runs.Items[0].Status != runcontrol.RunSucceeded {
		t.Fatalf("run did not succeed: %+v", runs.Items)
	}

	taskRequest := httptest.NewRequest(
		http.MethodGet,
		"/api/v1/tasks/"+created.Task.TaskID,
		nil,
	)
	taskResponse := httptest.NewRecorder()
	router.ServeHTTP(taskResponse, taskRequest)
	if taskResponse.Code != http.StatusOK || !strings.Contains(taskResponse.Body.String(), `"version":2`) {
		t.Fatalf("draft was not committed: status=%d body=%s", taskResponse.Code, taskResponse.Body.String())
	}

	eventsRequest := httptest.NewRequest(
		http.MethodGet,
		"/api/v1/tasks/"+created.Task.TaskID+"/events",
		nil,
	)
	eventsResponse := httptest.NewRecorder()
	router.ServeHTTP(eventsResponse, eventsRequest)
	events := eventsResponse.Body.String()
	for _, eventType := range []string{
		"agent.MODEL_ATTEMPT",
		"agent.CHECKPOINT_SAVED",
		"agent.DRAFT_SUBMITTED",
		"agent.RUN_COMPLETED",
	} {
		if !strings.Contains(events, "event: "+eventType) {
			t.Fatalf("SSE stream lacks %s: %s", eventType, events)
		}
	}
}

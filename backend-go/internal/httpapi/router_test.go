package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
)

type rejectingResolver struct{}

func (rejectingResolver) Resolve(*http.Request) (string, string, error) {
	return "", "", errors.New("token rejected")
}

func TestRouterRejectsRequestsWhenPrincipalCannotBeResolved(t *testing.T) {
	store := runcontrol.NewMemoryStore(runcontrol.QueuePolicy{MaxGlobalRunnable: 30, MaxRunnablePerOwner: 1})
	router := NewRouterWithPrincipalResolver(store, rejectingResolver{})
	req := httptest.NewRequest(http.MethodGet, "/api/v1/tasks", nil)
	response := httptest.NewRecorder()
	router.ServeHTTP(response, req)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", response.Code)
	}
}

func TestStartTaskIsIdempotentAndOwnerScoped(t *testing.T) {
	store := runcontrol.NewMemoryStore(runcontrol.QueuePolicy{MaxGlobalRunnable: 30, MaxRunnablePerOwner: 1})
	router := NewRouter(store)

	request := func(owner, message string) *httptest.ResponseRecorder {
		body := `{"message":"` + message + `"}`
		req := httptest.NewRequest(http.MethodPost, "/api/v1/tasks/from-message", strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Idempotency-Key", "start-1")
		req.Header.Set("X-User-ID", owner)
		recorder := httptest.NewRecorder()
		router.ServeHTTP(recorder, req)
		return recorder
	}

	first := request("alice", "需求 A")
	replay := request("alice", "需求 A")
	otherOwner := request("bob", "需求 A")
	if first.Code != http.StatusCreated || replay.Code != http.StatusCreated || otherOwner.Code != http.StatusCreated {
		t.Fatalf("unexpected statuses: %d %d %d", first.Code, replay.Code, otherOwner.Code)
	}
	var firstPayload, replayPayload map[string]any
	if err := json.Unmarshal(first.Body.Bytes(), &firstPayload); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(replay.Body.Bytes(), &replayPayload); err != nil {
		t.Fatal(err)
	}
	firstTask := firstPayload["task"].(map[string]any)["task_id"]
	replayTask := replayPayload["task"].(map[string]any)["task_id"]
	if firstTask != replayTask {
		t.Fatalf("idempotent replay created a different task: %v != %v", firstTask, replayTask)
	}
}

func TestCrossOwnerTaskReadReturnsNotFound(t *testing.T) {
	store := runcontrol.NewMemoryStore(runcontrol.QueuePolicy{MaxGlobalRunnable: 30, MaxRunnablePerOwner: 1})
	router := NewRouter(store)

	create := httptest.NewRequest(http.MethodPost, "/api/v1/tasks/from-message", strings.NewReader(`{"message":"需求 A"}`))
	create.Header.Set("Content-Type", "application/json")
	create.Header.Set("Idempotency-Key", "start-1")
	create.Header.Set("X-User-ID", "alice")
	created := httptest.NewRecorder()
	router.ServeHTTP(created, create)
	var payload map[string]any
	if err := json.Unmarshal(created.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	taskID := payload["task"].(map[string]any)["task_id"].(string)

	read := httptest.NewRequest(http.MethodGet, "/api/v1/tasks/"+taskID, nil)
	read.Header.Set("X-User-ID", "bob")
	response := httptest.NewRecorder()
	router.ServeHTTP(response, read)
	if response.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d", response.Code)
	}
}

func TestTaskEventsExposeSSECursorAndOwnerIsolation(t *testing.T) {
	store := runcontrol.NewMemoryStore(runcontrol.QueuePolicy{MaxGlobalRunnable: 30, MaxRunnablePerOwner: 1})
	router := NewRouter(store)

	created, err := store.CreateTaskWithRun(context.Background(), "tenant", "alice", "message", "start")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.AppendTaskEvent(context.Background(), "tenant", "alice", created.Task.TaskID, "run.queued", []byte(`{"run_id":"`+created.Run.RunID+`"}`)); err != nil {
		t.Fatal(err)
	}

	req := httptest.NewRequest(http.MethodGet, "/api/v1/tasks/"+created.Task.TaskID+"/events?after_sequence=1", nil)
	req.Header.Set("X-User-ID", "alice")
	req.Header.Set("X-Tenant-ID", "tenant")
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, req)
	if recorder.Code != http.StatusOK || recorder.Header().Get("Content-Type") != "text/event-stream" {
		t.Fatalf("unexpected SSE response: %d %q", recorder.Code, recorder.Header().Get("Content-Type"))
	}
	body := recorder.Body.String()
	if !strings.Contains(body, "id: 2\nevent: run.queued\n") || strings.Contains(body, "id: 1\n") {
		t.Fatalf("unexpected SSE body: %q", body)
	}

	reconnect := httptest.NewRequest(http.MethodGet, "/api/v1/tasks/"+created.Task.TaskID+"/events", nil)
	reconnect.Header.Set("X-User-ID", "alice")
	reconnect.Header.Set("X-Tenant-ID", "tenant")
	reconnect.Header.Set("Last-Event-ID", "1")
	reconnectRecorder := httptest.NewRecorder()
	router.ServeHTTP(reconnectRecorder, reconnect)
	if !strings.Contains(reconnectRecorder.Body.String(), "id: 2\nevent: run.queued\n") {
		t.Fatalf("Last-Event-ID did not resume from sequence 1: %q", reconnectRecorder.Body.String())
	}

	other := httptest.NewRequest(http.MethodGet, "/api/v1/tasks/"+created.Task.TaskID+"/events", nil)
	other.Header.Set("X-User-ID", "bob")
	other.Header.Set("X-Tenant-ID", "tenant")
	otherRecorder := httptest.NewRecorder()
	router.ServeHTTP(otherRecorder, other)
	if otherRecorder.Code != http.StatusNotFound {
		t.Fatalf("expected cross-owner 404, got %d", otherRecorder.Code)
	}
}

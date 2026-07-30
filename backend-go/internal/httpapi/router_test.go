package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

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

func TestMeRequiresAnAuthenticatedPrincipal(t *testing.T) {
	store := runcontrol.NewMemoryStore(runcontrol.QueuePolicy{MaxGlobalRunnable: 30, MaxRunnablePerOwner: 1})
	router := NewRouterWithPrincipalResolver(store, rejectingResolver{})
	req := httptest.NewRequest(http.MethodGet, "/api/v1/me", nil)
	response := httptest.NewRecorder()
	router.ServeHTTP(response, req)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d with body %s", response.Code, response.Body.String())
	}
}

func TestSecureRouterRejectsCrossOriginMutation(t *testing.T) {
	store := runcontrol.NewMemoryStore(runcontrol.QueuePolicy{MaxGlobalRunnable: 30, MaxRunnablePerOwner: 1})
	router := NewSecureRouter(store, DevPrincipalResolver{}, "https://prd.example.com")
	req := httptest.NewRequest(http.MethodPost, "/api/v1/tasks/from-message", strings.NewReader(`{"message":"需求"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Idempotency-Key", "origin-test")
	req.Header.Set("Origin", "https://evil.example")
	response := httptest.NewRecorder()
	router.ServeHTTP(response, req)
	if response.Code != http.StatusForbidden {
		t.Fatalf("expected 403, got %d with body %s", response.Code, response.Body.String())
	}

	req = httptest.NewRequest(http.MethodPost, "/api/v1/tasks/from-message", strings.NewReader(`{"message":"需求"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Idempotency-Key", "origin-test")
	req.Header.Set("Origin", "https://prd.example.com")
	req.Host = "prd.example.com"
	response = httptest.NewRecorder()
	router.ServeHTTP(response, req)
	if response.Code != http.StatusCreated {
		t.Fatalf("expected same-origin request to pass, got %d with body %s", response.Code, response.Body.String())
	}

	req = httptest.NewRequest(http.MethodPost, "/api/v1/tasks/from-message", strings.NewReader(`{"message":"需求"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Idempotency-Key", "host-test")
	req.Header.Set("Origin", "https://prd.example.com")
	req.Host = "evil.example"
	response = httptest.NewRecorder()
	router.ServeHTTP(response, req)
	if response.Code != http.StatusForbidden {
		t.Fatalf("expected mismatched host to be rejected, got %d with body %s", response.Code, response.Body.String())
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

func TestStartTaskReturnsRetryableBackpressureWhenWaitingQueueIsFull(t *testing.T) {
	store := runcontrol.NewMemoryStore(runcontrol.QueuePolicy{
		MaxGlobalRunnable:   1,
		MaxRunnablePerOwner: 1,
		MaxWaitingRuns:      1,
	})
	router := NewRouter(store)
	start := func(owner, key string) *httptest.ResponseRecorder {
		request := httptest.NewRequest(http.MethodPost, "/api/v1/tasks/from-message", strings.NewReader(`{"message":"需求"}`))
		request.Header.Set("Content-Type", "application/json")
		request.Header.Set("Idempotency-Key", key)
		request.Header.Set("X-User-ID", owner)
		recorder := httptest.NewRecorder()
		router.ServeHTTP(recorder, request)
		return recorder
	}
	if response := start("alice", "one"); response.Code != http.StatusCreated {
		t.Fatalf("first request failed: %d %s", response.Code, response.Body.String())
	}
	if response := start("bob", "two"); response.Code != http.StatusCreated {
		t.Fatalf("waiting request failed: %d %s", response.Code, response.Body.String())
	}
	response := start("carol", "three")
	if response.Code != http.StatusTooManyRequests {
		t.Fatalf("expected 429, got %d: %s", response.Code, response.Body.String())
	}
	if response.Header().Get("Retry-After") != "5" || !strings.Contains(response.Body.String(), `"retryable":true`) {
		t.Fatalf("missing retry contract: headers=%v body=%s", response.Header(), response.Body.String())
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

func TestAgentProductAPIsExposeDraftEvidenceAttemptsAndRetry(t *testing.T) {
	store := runcontrol.NewMemoryStore(runcontrol.QueuePolicy{
		MaxGlobalRunnable: 1, MaxRunnablePerOwner: 1, MaxWaitingRuns: 10,
	})
	router := NewRouter(store)
	created, err := store.CreateTaskWithRun(context.Background(), "tenant", "alice", "message", "start")
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	acquired, err := store.AcquireRun(context.Background(), created.Run.RunID, "worker", now, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	lease := runcontrol.LeaseContext{
		RunID: acquired.RunID, LeaseID: acquired.LeaseID, WorkerID: acquired.WorkerID,
		FencingToken: acquired.FencingToken, ExpiresAt: acquired.LeaseExpiresAt,
	}
	if _, err := store.RecordModelAttempt(context.Background(), lease, runcontrol.ModelAttempt{
		AttemptKey: "attempt-1", Operation: "draft", Provider: "deepseek",
		RequestHash: "sha256:request", Status: "SUCCEEDED",
		ResponseMetadataJSON: `{"request_id":"redacted"}`,
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := store.AppendEvidence(context.Background(), lease, []runcontrol.EvidenceItem{{
		SourceType: "github", SourceID: "commit:abc", Locator: "README.md#L1",
		ExcerptHash: "sha256:evidence", Excerpt: "private evidence body",
	}}); err != nil {
		t.Fatal(err)
	}
	if _, err := store.SubmitDraft(context.Background(), lease, "draft-1", 1, []byte(`{"markdown":"# Working PRD"}`)); err != nil {
		t.Fatal(err)
	}
	if _, err := store.CompleteRun(context.Background(), lease, runcontrol.RunSucceeded, now.Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	request := func(method, path, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("X-User-ID", "alice")
		req.Header.Set("X-Tenant-ID", "tenant")
		recorder := httptest.NewRecorder()
		router.ServeHTTP(recorder, req)
		return recorder
	}
	draft := request(http.MethodGet, "/api/v1/tasks/"+created.Task.TaskID+"/draft", "")
	if draft.Code != http.StatusOK || !strings.Contains(draft.Body.String(), `"markdown":"# Working PRD"`) {
		t.Fatalf("unexpected draft response: %d %s", draft.Code, draft.Body.String())
	}
	markdown := request(http.MethodGet, "/api/v1/tasks/"+created.Task.TaskID+"/draft.md", "")
	if markdown.Code != http.StatusOK || markdown.Body.String() != "# Working PRD" {
		t.Fatalf("unexpected markdown response: %d %q", markdown.Code, markdown.Body.String())
	}
	evidence := request(http.MethodGet, "/api/v1/tasks/"+created.Task.TaskID+"/evidence", "")
	if evidence.Code != http.StatusOK || !strings.Contains(evidence.Body.String(), `"locator":"README.md#L1"`) ||
		strings.Contains(evidence.Body.String(), "private evidence body") {
		t.Fatalf("unexpected evidence response: %d %s", evidence.Code, evidence.Body.String())
	}
	attempts := request(http.MethodGet, "/api/v1/tasks/"+created.Task.TaskID+"/attempts", "")
	if attempts.Code != http.StatusOK || !strings.Contains(attempts.Body.String(), `"status":"SUCCEEDED"`) ||
		strings.Contains(attempts.Body.String(), "request_id") {
		t.Fatalf("unexpected attempts response: %d %s", attempts.Code, attempts.Body.String())
	}
	previewRequest := httptest.NewRequest(http.MethodPost, "/api/v1/tasks/"+created.Task.TaskID+"/publish/feishu/preview", strings.NewReader(`{"expected_task_version":2}`))
	previewRequest.Header.Set("Content-Type", "application/json")
	previewRequest.Header.Set("Idempotency-Key", "preview-1")
	previewRequest.Header.Set("X-User-ID", "alice")
	previewRequest.Header.Set("X-Tenant-ID", "tenant")
	previewResponse := httptest.NewRecorder()
	router.ServeHTTP(previewResponse, previewRequest)
	if previewResponse.Code != http.StatusCreated {
		t.Fatalf("preview failed: %d %s", previewResponse.Code, previewResponse.Body.String())
	}
	var previewPayload struct {
		Preview runcontrol.PublishPreviewResult `json:"preview"`
	}
	if err := json.Unmarshal(previewResponse.Body.Bytes(), &previewPayload); err != nil {
		t.Fatal(err)
	}
	confirmRequest := httptest.NewRequest(
		http.MethodPost,
		"/api/v1/tasks/"+created.Task.TaskID+"/publish/feishu",
		strings.NewReader(fmt.Sprintf(
			`{"publish_id":%q,"confirmation_token":%q,"expected_task_version":2}`,
			previewPayload.Preview.PublishID,
			previewPayload.Preview.ConfirmationToken,
		)),
	)
	confirmRequest.Header.Set("Content-Type", "application/json")
	confirmRequest.Header.Set("Idempotency-Key", "publish-1")
	confirmRequest.Header.Set("X-User-ID", "alice")
	confirmRequest.Header.Set("X-Tenant-ID", "tenant")
	confirmResponse := httptest.NewRecorder()
	router.ServeHTTP(confirmResponse, confirmRequest)
	if confirmResponse.Code != http.StatusAccepted {
		t.Fatalf("confirm failed: %d %s", confirmResponse.Code, confirmResponse.Body.String())
	}
	jobs, err := store.ClaimPendingPublishes(context.Background(), "integration-1", 1, time.Now().UTC(), time.Minute)
	if err != nil || len(jobs) != 1 || jobs[0].Record.PublishID != previewPayload.Preview.PublishID {
		t.Fatalf("publish was not claimable: %+v %v", jobs, err)
	}
	if err := store.CompletePublish(context.Background(), "integration-1", jobs[0].Record.PublishID, runcontrol.PublishResult{
		Status: runcontrol.PublishSucceeded, SafeURL: "https://example.feishu.cn/wiki/fixed", ProviderRevision: "8",
	}, time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
	publishes := request(http.MethodGet, "/api/v1/tasks/"+created.Task.TaskID+"/publishes", "")
	if publishes.Code != http.StatusOK || !strings.Contains(publishes.Body.String(), `"status":"SUCCEEDED"`) ||
		!strings.Contains(publishes.Body.String(), `"safe_url":"https://example.feishu.cn/wiki/fixed"`) {
		t.Fatalf("unexpected publish history: %d %s", publishes.Code, publishes.Body.String())
	}
	retryRequest := func() *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodPost, "/api/v1/tasks/"+created.Task.TaskID+"/retry", strings.NewReader(`{"expected_task_version":2}`))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Idempotency-Key", "retry-1")
		req.Header.Set("X-User-ID", "alice")
		req.Header.Set("X-Tenant-ID", "tenant")
		recorder := httptest.NewRecorder()
		router.ServeHTTP(recorder, req)
		return recorder
	}
	firstRetry := retryRequest()
	replayedRetry := retryRequest()
	if firstRetry.Code != http.StatusCreated || replayedRetry.Code != http.StatusCreated {
		t.Fatalf("retry failed: first=%d %s replay=%d %s", firstRetry.Code, firstRetry.Body.String(), replayedRetry.Code, replayedRetry.Body.String())
	}
	var firstPayload, replayPayload struct {
		Run runcontrol.AgentRun `json:"run"`
	}
	if err := json.Unmarshal(firstRetry.Body.Bytes(), &firstPayload); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(replayedRetry.Body.Bytes(), &replayPayload); err != nil {
		t.Fatal(err)
	}
	if firstPayload.Run.RunID == "" || firstPayload.Run.RunID != replayPayload.Run.RunID {
		t.Fatalf("retry was not idempotent: %q != %q", firstPayload.Run.RunID, replayPayload.Run.RunID)
	}
}

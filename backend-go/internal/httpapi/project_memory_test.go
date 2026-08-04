package httpapi

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
)

func TestProjectMemoryHTTPGovernanceFlow(t *testing.T) {
	store := runcontrol.NewMemoryStore(runcontrol.QueuePolicy{MaxGlobalRunnable: 2, MaxRunnablePerOwner: 2, MaxWaitingRuns: 10})
	router := NewRouter(store)
	request := httptest.NewRequest(http.MethodPost, "/api/v1/tasks/from-message", bytes.NewBufferString(`{"message":"build a PRD"}`))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Idempotency-Key", "task-1")
	response := httptest.NewRecorder()
	router.ServeHTTP(response, request)
	if response.Code != http.StatusCreated {
		t.Fatalf("create task failed: %d %s", response.Code, response.Body.String())
	}

	response = httptest.NewRecorder()
	router.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/api/v1/memory-spaces", nil))
	if response.Code != http.StatusOK {
		t.Fatalf("list spaces failed: %d %s", response.Code, response.Body.String())
	}
	var spaces struct {
		Items []runcontrol.MemorySpace `json:"items"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &spaces); err != nil || len(spaces.Items) != 1 {
		t.Fatalf("default memory space missing: body=%s err=%v", response.Body.String(), err)
	}
	spaceID := spaces.Items[0].SpaceID

	request = httptest.NewRequest(http.MethodPost, "/api/v1/memory-spaces/"+spaceID+"/candidates", bytes.NewBufferString(`{"memory_type":"PROJECT_DECISION","subject":"runtime","predicate":"control_plane","value":"go","statement":"The control plane is implemented in Go.","authority_class":"DERIVED_PROPOSAL","tags":["architecture"]}`))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Idempotency-Key", "candidate-1")
	response = httptest.NewRecorder()
	router.ServeHTTP(response, request)
	if response.Code != http.StatusCreated {
		t.Fatalf("propose memory failed: %d %s", response.Code, response.Body.String())
	}
	var candidateBody struct {
		Candidate runcontrol.ProjectMemoryCandidate `json:"candidate"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &candidateBody); err != nil {
		t.Fatal(err)
	}
	confirmBody, _ := json.Marshal(map[string]string{"expected_hash": candidateBody.Candidate.RequestHash})
	request = httptest.NewRequest(http.MethodPost, "/api/v1/memory-spaces/"+spaceID+"/candidates/"+candidateBody.Candidate.CandidateID+"/confirm", bytes.NewReader(confirmBody))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Idempotency-Key", "confirm-1")
	response = httptest.NewRecorder()
	router.ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("confirm memory failed: %d %s", response.Code, response.Body.String())
	}

	response = httptest.NewRecorder()
	router.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/api/v1/memory-spaces/"+spaceID+"/records", nil))
	if response.Code != http.StatusOK || !bytes.Contains(response.Body.Bytes(), []byte(`"authority_class":"USER_CONFIRMED"`)) {
		t.Fatalf("confirmed memory was not listed: %d %s", response.Code, response.Body.String())
	}
}

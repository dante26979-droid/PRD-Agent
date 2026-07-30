package feishu

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

func TestClientOverwritesOnlyConfiguredWikiDocx(t *testing.T) {
	var mu sync.Mutex
	requests := make([]string, 0)
	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		mu.Lock()
		requests = append(requests, request.Method+" "+request.URL.String())
		mu.Unlock()
		writer.Header().Set("Content-Type", "application/json")
		switch {
		case request.URL.Path == "/open-apis/auth/v3/tenant_access_token/internal":
			_ = json.NewEncoder(writer).Encode(map[string]any{"code": 0, "tenant_access_token": "tenant-token"})
		case request.URL.Path == "/open-apis/wiki/v2/spaces/get_node":
			_ = json.NewEncoder(writer).Encode(map[string]any{"code": 0, "data": map[string]any{"node": map[string]any{"obj_type": "docx", "obj_token": "docx-fixed"}}})
		case strings.HasSuffix(request.URL.Path, "/children/batch_delete"):
			_ = json.NewEncoder(writer).Encode(map[string]any{"code": 0, "data": map[string]any{}})
		case strings.HasSuffix(request.URL.Path, "/children") && request.Method == http.MethodPost:
			var payload map[string]any
			if json.NewDecoder(request.Body).Decode(&payload) != nil || len(payload["children"].([]any)) != 2 {
				t.Fatal("invalid fixed Wiki block payload")
			}
			_ = json.NewEncoder(writer).Encode(map[string]any{"code": 0, "data": map[string]any{}})
		case strings.HasSuffix(request.URL.Path, "/children"):
			_ = json.NewEncoder(writer).Encode(map[string]any{"code": 0, "data": map[string]any{"items": []any{map[string]any{"block_id": "old"}}}})
		case strings.HasSuffix(request.URL.Path, "/documents/docx-fixed"):
			revision := 7
			if countMatching(requests, "/children", http.MethodPost) > 0 {
				revision = 8
			}
			_ = json.NewEncoder(writer).Encode(map[string]any{"code": 0, "data": map[string]any{"document": map[string]any{"revision_id": revision}}})
		default:
			t.Fatalf("unexpected request: %s %s", request.Method, request.URL)
		}
	}))
	defer server.Close()
	client, err := NewClient(Config{
		AppID: "app-id", AppSecret: "app-secret", DocumentHost: "example.feishu.cn",
		WikiNodeToken: "wiki-fixed", BaseURL: server.URL + "/open-apis",
	}, server.Client())
	if err != nil {
		t.Fatal(err)
	}
	result, err := client.Publish(context.Background(), []byte(`{"markdown":"# PRD\n\n正文"}`))
	if err != nil {
		t.Fatal(err)
	}
	if result.SafeURL != "https://example.feishu.cn/wiki/wiki-fixed" || result.ProviderRevision != "8" {
		t.Fatalf("unexpected publish result: %+v", result)
	}
	for _, request := range requests {
		if strings.Contains(request, "app-secret") || strings.Contains(request, "/docx/v1/documents?") {
			t.Fatalf("secret leaked or arbitrary document create used: %s", request)
		}
	}
}

func TestClientMarksAmbiguousMutationAsResultUnknown(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		writer.Header().Set("Content-Type", "application/json")
		switch {
		case strings.HasSuffix(request.URL.Path, "/tenant_access_token/internal"):
			_ = json.NewEncoder(writer).Encode(map[string]any{"code": 0, "tenant_access_token": "token"})
		case strings.Contains(request.URL.Path, "/wiki/"):
			_ = json.NewEncoder(writer).Encode(map[string]any{"code": 0, "data": map[string]any{"node": map[string]any{"obj_type": "docx", "obj_token": "docx-fixed"}}})
		case strings.HasSuffix(request.URL.Path, "/children") && request.Method == http.MethodPost:
			writer.WriteHeader(http.StatusServiceUnavailable)
			_ = json.NewEncoder(writer).Encode(map[string]any{"code": 1})
		case strings.HasSuffix(request.URL.Path, "/children"):
			_ = json.NewEncoder(writer).Encode(map[string]any{"code": 0, "data": map[string]any{"items": []any{}}})
		default:
			_ = json.NewEncoder(writer).Encode(map[string]any{"code": 0, "data": map[string]any{"document": map[string]any{"revision_id": 7}}})
		}
	}))
	defer server.Close()
	client, err := NewClient(Config{
		AppID: "app-id", AppSecret: "app-secret", DocumentHost: "example.feishu.cn",
		WikiNodeToken: "wiki-fixed", BaseURL: server.URL + "/open-apis",
	}, server.Client())
	if err != nil {
		t.Fatal(err)
	}
	_, err = client.Publish(context.Background(), []byte(`{"markdown":"# PRD"}`))
	var providerError *ProviderError
	if !errors.As(err, &providerError) || !providerError.ResultUnknown || providerError.Retryable {
		t.Fatalf("expected non-retryable ambiguous result, got %v", err)
	}
}

func countMatching(requests []string, suffix, method string) int {
	count := 0
	for _, request := range requests {
		if strings.HasPrefix(request, method+" ") && strings.Contains(request, suffix) {
			count++
		}
	}
	return count
}

package capability

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	agentv1 "github.com/dante26979-droid/prd-agent/contracts/gen/go/agent/v1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

type capabilityLeaseStore struct {
	input runcontrol.AgentRunInput
}

func (store capabilityLeaseStore) GetRunContext(context.Context, runcontrol.LeaseContext) (runcontrol.AgentRunInput, error) {
	return store.input, nil
}

func TestCapabilityServerRequiresIdentityAndRunBoundRepository(t *testing.T) {
	revision := strings.Repeat("c", 40)
	root := t.TempDir()
	snapshot := filepath.Join(root, revision)
	if err := os.MkdirAll(snapshot, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(snapshot, ".prd-agent-revision"), []byte(revision), 0o400); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(snapshot, "README.md"), []byte("Capability Gateway reads repository evidence\n"), 0o400); err != nil {
		t.Fatal(err)
	}
	manager, err := NewRepositoryManager(RepositoryConfig{
		Repository: "dante26979-droid/PRD-Agent",
		Root:       root,
	})
	if err != nil {
		t.Fatal(err)
	}
	bindingID := "github:dante26979-droid/PRD-Agent"
	server, err := NewServer(capabilityLeaseStore{input: runcontrol.AgentRunInput{
		Run:                 runcontrol.AgentRun{RunID: "run-1", TenantID: "default", OwnerID: "admin"},
		RepositoryBindingID: bindingID,
		RepositoryRevision:  revision,
	}}, manager, bindingID, strings.Repeat("s", 32))
	if err != nil {
		t.Fatal(err)
	}
	request := &agentv1.SearchRepositoryRequest{
		Capability: &agentv1.CapabilityLease{
			Lease: &agentv1.LeaseContext{
				RunId: "run-1", LeaseId: "lease-1", WorkerId: "worker-1",
				FencingToken: 1, ExpiresAt: time.Now().Add(time.Minute).UTC().Format(time.RFC3339Nano),
			},
			Meta: &agentv1.RequestMeta{RequestId: "request-1", CorrelationId: "run-1"},
		},
		BindingId: bindingID,
		Revision:  revision,
		Query:     "Gateway",
		Limit:     10,
	}

	if _, err := server.SearchRepository(context.Background(), request); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("missing service identity should fail: %v", err)
	}
	ctx := metadata.NewIncomingContext(context.Background(), metadata.Pairs(
		"authorization", "Bearer "+strings.Repeat("s", 32),
	))
	response, err := server.SearchRepository(ctx, request)
	if err != nil {
		t.Fatal(err)
	}
	if len(response.Hits) != 1 || response.Hits[0].Path != "README.md" {
		t.Fatalf("unexpected capability response: %+v", response)
	}
	request.Revision = strings.Repeat("d", 40)
	if _, err := server.SearchRepository(ctx, request); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("unbound revision should fail: %v", err)
	}
}

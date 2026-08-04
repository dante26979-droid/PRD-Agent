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
	"google.golang.org/grpc/metadata"
)

func TestCapabilityServerSearchesOnlyRunAssignedMemoryWatermark(t *testing.T) {
	ctx := context.Background()
	store := runcontrol.NewMemoryStore(runcontrol.QueuePolicy{MaxGlobalRunnable: 2, MaxRunnablePerOwner: 2, MaxWaitingRuns: 10})
	first, err := store.CreateTaskWithRun(ctx, "tenant", "owner", "first", "first")
	if err != nil {
		t.Fatal(err)
	}
	spaces, _ := store.ListMemorySpaces(ctx, "tenant", "owner")
	candidate, err := store.ProposeProjectMemory(ctx, runcontrol.ProposeMemoryCommand{TenantID: "tenant", OwnerID: "owner", SpaceID: spaces[0].SpaceID, MemoryType: runcontrol.MemoryProjectDecision, Subject: "runtime", Predicate: "control_plane", Value: "go", Statement: "The control plane is implemented in Go.", AuthorityClass: runcontrol.MemoryDerivedProposal, IdempotencyKey: "propose"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.ConfirmProjectMemoryCandidate(ctx, runcontrol.ReviewMemoryCandidateCommand{TenantID: "tenant", OwnerID: "owner", SpaceID: spaces[0].SpaceID, CandidateID: candidate.CandidateID, ExpectedHash: candidate.RequestHash, ActorRef: "user:owner", IdempotencyKey: "confirm"}); err != nil {
		t.Fatal(err)
	}
	second, err := store.CreateTaskWithRun(ctx, "tenant", "owner", "second", "second")
	if err != nil {
		t.Fatal(err)
	}
	run, err := store.AcquireRun(ctx, second.Run.RunID, "worker", time.Now().UTC(), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	revision := strings.Repeat("a", 40)
	snapshot := filepath.Join(root, revision)
	if err := os.MkdirAll(snapshot, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(snapshot, ".prd-agent-revision"), []byte(revision), 0o400); err != nil {
		t.Fatal(err)
	}
	manager, err := NewRepositoryManager(RepositoryConfig{Repository: "owner/repo", Root: root})
	if err != nil {
		t.Fatal(err)
	}
	token := strings.Repeat("s", 32)
	server, err := NewServer(store, manager, "github:owner/repo", token)
	if err != nil {
		t.Fatal(err)
	}
	request := &agentv1.SearchProjectMemoryRequest{Capability: &agentv1.CapabilityLease{Lease: &agentv1.LeaseContext{RunId: run.RunID, LeaseId: run.LeaseID, WorkerId: run.WorkerID, FencingToken: run.FencingToken, ExpiresAt: run.LeaseExpiresAt.Format(time.RFC3339Nano)}, Meta: &agentv1.RequestMeta{RequestId: "memory-request", CorrelationId: run.RunID}}, Query: "control plane", Operation: "plan_outline", Limit: 10}
	authorized := metadata.NewIncomingContext(ctx, metadata.Pairs("authorization", "Bearer "+token))
	result, err := server.SearchProjectMemory(authorized, request)
	if err != nil {
		t.Fatal(err)
	}
	if result.MemoryWatermark != 1 || len(result.Records) != 1 || result.Records[0].MemoryId == "" {
		t.Fatalf("unexpected project memory response: %+v", result)
	}
	_ = first
}

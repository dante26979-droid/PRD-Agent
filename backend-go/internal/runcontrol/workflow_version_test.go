package runcontrol

import (
	"context"
	"testing"
)

func TestMemoryStorePersistsSelectedWorkflowVersion(t *testing.T) {
	store := NewMemoryStore(QueuePolicy{DefaultWorkflowVersion: WorkflowVersionV4})
	created, err := store.CreateTaskWithRun(context.Background(), "tenant", "owner", "message", "idem")
	if err != nil {
		t.Fatal(err)
	}
	if created.Run.WorkflowVersion != WorkflowVersionV4 {
		t.Fatalf("workflow version = %q, want %q", created.Run.WorkflowVersion, WorkflowVersionV4)
	}
	replayed, err := store.CreateTaskWithRun(context.Background(), "tenant", "owner", "message", "idem")
	if err != nil {
		t.Fatal(err)
	}
	if replayed.Run.WorkflowVersion != created.Run.WorkflowVersion {
		t.Fatalf("idempotent replay changed workflow version: %q", replayed.Run.WorkflowVersion)
	}
}

func TestMemoryStoreDefaultsLegacyWorkflowVersion(t *testing.T) {
	store := NewMemoryStore(QueuePolicy{})
	created, err := store.CreateTaskWithRun(context.Background(), "tenant", "owner", "message", "idem")
	if err != nil {
		t.Fatal(err)
	}
	if created.Run.WorkflowVersion != WorkflowVersionV1 {
		t.Fatalf("workflow version = %q, want legacy v1", created.Run.WorkflowVersion)
	}
}

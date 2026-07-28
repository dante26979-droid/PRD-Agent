package storage

import (
	"context"
	"os"
	"strconv"
	"testing"
	"time"
)

func TestPostgresStoreCreatesTaskAndInitialAgentRun(t *testing.T) {
	dsn := os.Getenv("PRD_AGENT_TEST_DATABASE_DSN")
	if dsn == "" {
		t.Skip("PRD_AGENT_TEST_DATABASE_DSN is not configured")
	}
	store, err := NewPostgresStore(context.Background(), Config{
		DSN:                 dsn,
		MinConns:            1,
		MaxConns:            2,
		MaxGlobalRunnable:   10,
		MaxRunnablePerOwner: 10,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()

	suffix := strconv.FormatInt(time.Now().UnixNano(), 10)
	created, err := store.CreateTaskWithRun(
		context.Background(),
		"integration-"+suffix,
		"owner-"+suffix,
		"create a PRD",
		"idempotency-"+suffix,
	)
	if err != nil {
		t.Fatal(err)
	}
	if created.Task.TaskID == "" || created.Run.RunID == "" {
		t.Fatalf("missing durable identifiers: %+v", created)
	}
	if created.Run.TaskID != created.Task.TaskID {
		t.Fatalf("run does not belong to task: %+v", created)
	}
}

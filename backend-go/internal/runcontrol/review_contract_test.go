package runcontrol_test

import (
	"testing"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/testsupport/reviewcontract"
)

func TestMemoryStoreReviewWorkflowContract(t *testing.T) {
	reviewcontract.Run(t, func(t *testing.T) (runcontrol.AgentExecutionStore, func()) {
		store := runcontrol.NewMemoryStore(runcontrol.QueuePolicy{
			MaxGlobalRunnable: 10, MaxRunnablePerOwner: 10, MaxWaitingRuns: 20,
			DefaultWorkflowVersion:        runcontrol.WorkflowVersionV4,
			DefaultExecutionLedgerVersion: runcontrol.ExecutionLedgerVersionV1,
		})
		return store, func() {}
	})
}

package dispatcher

import (
	"strings"
	"testing"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
)

func TestInputToProtoCarriesPinnedRepositoryAuthority(t *testing.T) {
	revision := strings.Repeat("a", 40)
	value := inputToProto(runcontrol.AgentRunInput{
		Run: runcontrol.AgentRun{
			RunID:    "run-1",
			TenantID: "tenant-1",
			OwnerID:  "owner-1",
			TaskID:   "task-1",
		},
		RepositoryBindingID: "binding-1",
		RepositoryRevision:  revision,
	})

	if len(value.AllowedSourceAuthorities) != 1 {
		t.Fatalf("expected one source authority, got %d", len(value.AllowedSourceAuthorities))
	}
	authority := value.AllowedSourceAuthorities[0]
	if authority.SourceKind != "github" || authority.BindingId != "binding-1" || authority.SourceVersion != revision {
		t.Fatalf("unexpected source authority: %+v", authority)
	}
}

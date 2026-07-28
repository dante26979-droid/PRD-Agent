package config

import "testing"

func TestDirectAgentRPCIsTheDefaultDispatchMode(t *testing.T) {
	t.Setenv("PRD_AGENT_GO_AGENT_DISPATCH_MODE", "")

	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.AgentDispatchMode != "go_rpc_pool" {
		t.Fatalf("unexpected default Agent dispatch mode: %q", cfg.AgentDispatchMode)
	}
}

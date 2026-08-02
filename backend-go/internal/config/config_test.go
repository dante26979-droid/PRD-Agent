package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

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

func TestProductionConfigurationFailsClosedWithoutDatabase(t *testing.T) {
	setProductionBase(t)
	t.Setenv("PRD_AGENT_DATABASE_DSN", "")
	t.Setenv("PRD_AGENT_DATABASE_DSN_FILE", "")

	_, err := Load()
	if err == nil || !strings.Contains(err.Error(), "PRD_AGENT_DATABASE_DSN") {
		t.Fatalf("expected missing database error, got %v", err)
	}
}

func TestProductionConfigurationRejectsDirectSecretEnvironment(t *testing.T) {
	setProductionBase(t)
	t.Setenv("PRD_AGENT_DATABASE_DSN", "postgres://leaked-through-process-environment")

	_, err := Load()
	if err == nil || !strings.Contains(err.Error(), "only through PRD_AGENT_DATABASE_DSN_FILE") {
		t.Fatalf("expected direct secret environment rejection, got %v", err)
	}
}

func TestProductionConfigurationRejectsDevelopmentPrincipal(t *testing.T) {
	setProductionBase(t)
	t.Setenv("PRD_AGENT_GO_ALLOW_DEV_PRINCIPAL", "true")

	_, err := Load()
	if err == nil || !strings.Contains(err.Error(), "dev principal") {
		t.Fatalf("expected dev principal rejection, got %v", err)
	}
}

func TestProductionOIDCConfigurationRequiresIssuer(t *testing.T) {
	setProductionBase(t)
	t.Setenv("PRD_AGENT_OIDC_ISSUER", "")

	_, err := Load()
	if err == nil || !strings.Contains(err.Error(), "OIDC") {
		t.Fatalf("expected OIDC validation error, got %v", err)
	}
}

func TestProductionProxyAllowlistDoesNotRequireOIDC(t *testing.T) {
	setProductionBase(t)
	secretFile := filepath.Join(t.TempDir(), "proxy-secret")
	if err := os.WriteFile(secretFile, []byte(strings.Repeat("p", 32)), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PRD_AGENT_AUTH_MODE", "proxy_allowlist")
	t.Setenv("PRD_AGENT_OIDC_ISSUER", "")
	t.Setenv("PRD_AGENT_OIDC_AUDIENCE", "")
	t.Setenv("PRD_AGENT_PUBLIC_ORIGIN", "https://prd.example.com")
	t.Setenv("PRD_AGENT_PROXY_SECRET_FILE", secretFile)
	t.Setenv("PRD_AGENT_ALLOWED_USERS", "alice,bob")
	t.Setenv("PRD_AGENT_DEFAULT_TENANT", "default")

	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.AuthMode != "proxy_allowlist" || len(cfg.AllowedUsers) != 2 {
		t.Fatalf("unexpected proxy configuration: %+v", cfg)
	}
}

func TestProductionProxyAllowlistRejectsMissingSecurityBoundary(t *testing.T) {
	setProductionBase(t)
	t.Setenv("PRD_AGENT_AUTH_MODE", "proxy_allowlist")
	t.Setenv("PRD_AGENT_PUBLIC_ORIGIN", "http://prd.example.com")
	t.Setenv("PRD_AGENT_PROXY_SECRET", "short")
	t.Setenv("PRD_AGENT_ALLOWED_USERS", "")

	_, err := Load()
	if err == nil || !strings.Contains(err.Error(), "PRD_AGENT_PUBLIC_ORIGIN") {
		t.Fatalf("expected unsafe public origin error, got %v", err)
	}
}

func TestSecretFileReadFailureIsReported(t *testing.T) {
	t.Setenv("PRD_AGENT_DATABASE_DSN", "")
	t.Setenv("PRD_AGENT_DATABASE_DSN_FILE", "/path/that/does/not/exist")

	_, err := Load()
	if err == nil || !strings.Contains(err.Error(), "PRD_AGENT_DATABASE_DSN_FILE") {
		t.Fatalf("expected secret file read error, got %v", err)
	}
}

func TestInvalidDurationIsRejected(t *testing.T) {
	t.Setenv("PRD_AGENT_GO_LEASE_TTL_SECONDS", "not-a-number")

	_, err := Load()
	if err == nil || !strings.Contains(err.Error(), "PRD_AGENT_GO_LEASE_TTL_SECONDS") {
		t.Fatalf("expected invalid duration error, got %v", err)
	}
}

func TestRolloutPolicyLoadsOnlyWhenVersionIsConfigured(t *testing.T) {
	t.Setenv("PRD_AGENT_ROLLOUT_POLICY_VERSION", "rollout-policy.v1:test")
	t.Setenv("PRD_AGENT_V4_CANARY_BASIS_POINTS", "2500")
	t.Setenv("PRD_AGENT_V4_SHADOW", "true")
	t.Setenv("PRD_AGENT_V4_INTERNAL_IDENTITIES", "tenant-a:owner-a,tenant-b:owner-b")
	t.Setenv("PRD_AGENT_V4_EXPLICIT_TASK_IDS", "task-one")
	t.Setenv("PRD_AGENT_V4_EMERGENCY_DENY_IDENTITIES", "tenant-c:owner-c")

	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	policy := cfg.AgentRolloutPolicy
	if policy == nil || policy.PolicyVersion != "rollout-policy.v1:test" || policy.CanaryBasisPoints != 2500 || !policy.Shadow {
		t.Fatalf("unexpected rollout policy: %+v", policy)
	}
	if !policy.InternalOwners["tenant-a:owner-a"] || !policy.ExplicitV4Tasks["task-one"] || !policy.EmergencyDeny["tenant-c:owner-c"] {
		t.Fatalf("rollout identity sets were not loaded: %+v", policy)
	}
}

func TestRolloutPolicyRejectsOutOfRangeCanary(t *testing.T) {
	t.Setenv("PRD_AGENT_ROLLOUT_POLICY_VERSION", "rollout-policy.v1:test")
	t.Setenv("PRD_AGENT_V4_CANARY_BASIS_POINTS", "10001")

	_, err := Load()
	if err == nil || !strings.Contains(err.Error(), "cannot exceed 10000") {
		t.Fatalf("expected invalid canary error, got %v", err)
	}
}

func TestProductionAgentEndpointRequiresServiceTokenFile(t *testing.T) {
	setProductionBase(t)
	t.Setenv("PRD_AGENT_GO_AGENT_RPC_WORKER_ENDPOINTS", "python-agent:9100")
	t.Setenv("PRD_AGENT_AGENT_RPC_TOKEN", "")
	t.Setenv("PRD_AGENT_AGENT_RPC_TOKEN_FILE", "")

	_, err := Load()
	if err == nil || !strings.Contains(err.Error(), "PRD_AGENT_AGENT_RPC_TOKEN_FILE") {
		t.Fatalf("expected missing Agent RPC token error, got %v", err)
	}
}

func TestProductionConfigurationRequiresFullFixedRepositoryRevision(t *testing.T) {
	setProductionBase(t)
	t.Setenv("PRD_AGENT_FIXED_REPOSITORY_REVISION", "main")

	_, err := Load()
	if err == nil || !strings.Contains(err.Error(), "full commit SHA") {
		t.Fatalf("expected fixed repository revision error, got %v", err)
	}
}

func setProductionBase(t *testing.T) {
	t.Helper()
	secretDirectory := t.TempDir()
	confirmationFile := filepath.Join(secretDirectory, "export-confirmation-secret")
	if err := os.WriteFile(confirmationFile, []byte(strings.Repeat("c", 32)), 0o600); err != nil {
		t.Fatal(err)
	}
	databaseFile := filepath.Join(secretDirectory, "database-dsn")
	if err := os.WriteFile(databaseFile, []byte("postgres://db/prd_agent"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PRD_AGENT_ENVIRONMENT", "production")
	t.Setenv("PRD_AGENT_AUTH_MODE", "oidc")
	t.Setenv("PRD_AGENT_DATABASE_DSN", "")
	t.Setenv("PRD_AGENT_DATABASE_DSN_FILE", databaseFile)
	t.Setenv("PRD_AGENT_GO_ALLOW_DEV_PRINCIPAL", "false")
	t.Setenv("PRD_AGENT_OIDC_ISSUER", "https://issuer.example")
	t.Setenv("PRD_AGENT_OIDC_AUDIENCE", "prd-agent")
	t.Setenv("PRD_AGENT_EXPORT_CONFIRMATION_SECRET_FILE", confirmationFile)
	t.Setenv("PRD_AGENT_FIXED_REPOSITORY_BINDING_ID", "github:dante26979-droid/PRD-Agent")
	t.Setenv("PRD_AGENT_FIXED_REPOSITORY", "dante26979-droid/PRD-Agent")
	t.Setenv("PRD_AGENT_FIXED_REPOSITORY_REVISION", strings.Repeat("a", 40))
}

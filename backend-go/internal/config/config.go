package config

import (
	"fmt"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	HTTPAddress              string
	AgentDispatchMode        string
	AgentWorkerEndpoints     []string
	AgentRPCMaxInflight      int
	AgentDispatchBatchSize   int
	AgentRPCConnectTimeout   time.Duration
	AgentRPCExecuteTimeout   time.Duration
	AgentRPCToken            string
	DatabaseDSN              string
	BrokerURL                string
	Environment              string
	AuthMode                 string
	PublicOrigin             string
	ProxySecret              string
	AllowedUsers             []string
	DefaultTenant            string
	ExportConfirmationSecret string
	FeishuAppID              string
	FeishuAppSecret          string
	FeishuDocumentHost       string
	FeishuWikiNodeToken      string
	FeishuBaseURL            string
	FixedRepositoryBindingID string
	FixedRepository          string
	FixedRepositoryRevision  string
	OIDCIssuer               string
	OIDCAudience             string
	AllowDevPrincipal        bool
	MaxGlobalRunnable        int
	MaxRunnablePerOwner      int
	MaxWaitingRuns           int
	DatabasePoolMin          int32
	DatabasePoolMax          int32
	ShutdownTimeout          time.Duration
	MaintenanceInterval      time.Duration
	LeaseTTL                 time.Duration
}

func Load() (Config, error) {
	return LoadFor("api")
}

func LoadFor(role string) (Config, error) {
	switch role {
	case "api", "maintenance", "migrate", "integration", "capability":
	default:
		return Config{}, fmt.Errorf("unknown process role %q", role)
	}
	environment := strings.ToLower(strings.TrimSpace(env("PRD_AGENT_ENVIRONMENT", "local")))
	switch environment {
	case "local", "test", "staging", "production":
	default:
		return Config{}, fmt.Errorf("PRD_AGENT_ENVIRONMENT must be one of: local, test, staging, production")
	}
	global, err := positiveInt("PRD_AGENT_MAX_GLOBAL_RUNNABLE", 30)
	if err != nil {
		return Config{}, err
	}
	perOwner, err := positiveInt("PRD_AGENT_MAX_RUNNABLE_PER_OWNER", 1)
	if err != nil {
		return Config{}, err
	}
	maxWaiting, err := positiveInt("PRD_AGENT_MAX_WAITING_RUNS", 100)
	if err != nil {
		return Config{}, err
	}
	poolMin, err := positiveInt("PRD_AGENT_DB_POOL_MIN", 1)
	if err != nil {
		return Config{}, err
	}
	poolMax, err := positiveInt("PRD_AGENT_DB_POOL_MAX", 4)
	if err != nil {
		return Config{}, err
	}
	allowDevPrincipal, err := boolEnv(
		"PRD_AGENT_GO_ALLOW_DEV_PRINCIPAL",
		environment == "local" || environment == "test",
	)
	if err != nil {
		return Config{}, err
	}
	authMode := strings.ToLower(strings.TrimSpace(os.Getenv("PRD_AGENT_AUTH_MODE")))
	if authMode == "" {
		if allowDevPrincipal {
			authMode = "dev"
		} else {
			authMode = "oidc"
		}
	}
	switch authMode {
	case "dev", "oidc", "proxy_allowlist":
	default:
		return Config{}, fmt.Errorf("PRD_AGENT_AUTH_MODE must be one of: dev, oidc, proxy_allowlist")
	}
	if poolMin > poolMax {
		return Config{}, fmt.Errorf("PRD_AGENT_DB_POOL_MIN cannot exceed PRD_AGENT_DB_POOL_MAX")
	}
	rpcMaxInflight, err := positiveInt("PRD_AGENT_GO_AGENT_RPC_MAX_INFLIGHT_PER_WORKER", 1)
	if err != nil {
		return Config{}, err
	}
	dispatchBatch, err := positiveInt("PRD_AGENT_GO_AGENT_DISPATCH_BATCH_SIZE", 10)
	if err != nil {
		return Config{}, err
	}
	rpcConnectTimeout, err := positiveInt("PRD_AGENT_GO_AGENT_RPC_CONNECT_TIMEOUT_SECONDS", 5)
	if err != nil {
		return Config{}, err
	}
	rpcExecuteTimeout, err := positiveInt("PRD_AGENT_GO_AGENT_RPC_EXECUTE_TIMEOUT_SECONDS", 1800)
	if err != nil {
		return Config{}, err
	}
	maintenanceInterval, err := positiveInt("PRD_AGENT_GO_MAINTENANCE_INTERVAL_SECONDS", 5)
	if err != nil {
		return Config{}, err
	}
	leaseTTL, err := positiveInt("PRD_AGENT_GO_LEASE_TTL_SECONDS", 60)
	if err != nil {
		return Config{}, err
	}
	databaseDSN, err := secretEnv("PRD_AGENT_DATABASE_DSN")
	if err != nil {
		return Config{}, err
	}
	var proxySecret string
	if authMode == "proxy_allowlist" {
		proxySecret, err = secretEnv("PRD_AGENT_PROXY_SECRET")
		if err != nil {
			return Config{}, err
		}
	}
	exportConfirmationSecret, err := secretEnv("PRD_AGENT_EXPORT_CONFIRMATION_SECRET")
	if err != nil {
		return Config{}, err
	}
	var feishuAppID, feishuAppSecret, feishuWikiNodeToken string
	if role == "integration" {
		feishuAppID, err = secretEnv("PRD_AGENT_FEISHU_APP_ID")
		if err != nil {
			return Config{}, err
		}
		feishuAppSecret, err = secretEnv("PRD_AGENT_FEISHU_APP_SECRET")
		if err != nil {
			return Config{}, err
		}
		feishuWikiNodeToken, err = secretEnv("PRD_AGENT_FEISHU_WIKI_NODE_TOKEN")
		if err != nil {
			return Config{}, err
		}
	}
	endpoints := splitCSV(os.Getenv("PRD_AGENT_GO_AGENT_RPC_WORKER_ENDPOINTS"))
	var agentRPCToken string
	if len(endpoints) > 0 || role == "capability" {
		agentRPCToken, err = secretEnv("PRD_AGENT_AGENT_RPC_TOKEN")
		if err != nil {
			return Config{}, err
		}
	}

	cfg := Config{
		HTTPAddress:              env("PRD_AGENT_GO_HTTP_ADDRESS", ":8080"),
		AgentDispatchMode:        env("PRD_AGENT_GO_AGENT_DISPATCH_MODE", "go_rpc_pool"),
		AgentWorkerEndpoints:     endpoints,
		AgentRPCMaxInflight:      rpcMaxInflight,
		AgentDispatchBatchSize:   dispatchBatch,
		AgentRPCConnectTimeout:   time.Duration(rpcConnectTimeout) * time.Second,
		AgentRPCExecuteTimeout:   time.Duration(rpcExecuteTimeout) * time.Second,
		AgentRPCToken:            agentRPCToken,
		DatabaseDSN:              databaseDSN,
		BrokerURL:                env("PRD_AGENT_BROKER_URL", ""),
		Environment:              environment,
		AuthMode:                 authMode,
		PublicOrigin:             strings.TrimRight(env("PRD_AGENT_PUBLIC_ORIGIN", ""), "/"),
		ProxySecret:              proxySecret,
		AllowedUsers:             splitCSV(os.Getenv("PRD_AGENT_ALLOWED_USERS")),
		DefaultTenant:            env("PRD_AGENT_DEFAULT_TENANT", "default"),
		ExportConfirmationSecret: exportConfirmationSecret,
		FeishuAppID:              feishuAppID,
		FeishuAppSecret:          feishuAppSecret,
		FeishuDocumentHost:       env("PRD_AGENT_FEISHU_DOCUMENT_HOST", ""),
		FeishuWikiNodeToken:      feishuWikiNodeToken,
		FeishuBaseURL:            env("PRD_AGENT_FEISHU_BASE_URL", "https://open.feishu.cn/open-apis"),
		FixedRepositoryBindingID: strings.TrimSpace(os.Getenv("PRD_AGENT_FIXED_REPOSITORY_BINDING_ID")),
		FixedRepository:          strings.TrimSpace(os.Getenv("PRD_AGENT_FIXED_REPOSITORY")),
		FixedRepositoryRevision:  strings.ToLower(strings.TrimSpace(os.Getenv("PRD_AGENT_FIXED_REPOSITORY_REVISION"))),
		OIDCIssuer:               env("PRD_AGENT_OIDC_ISSUER", ""),
		OIDCAudience:             env("PRD_AGENT_OIDC_AUDIENCE", ""),
		AllowDevPrincipal:        allowDevPrincipal,
		MaxGlobalRunnable:        global,
		MaxRunnablePerOwner:      perOwner,
		MaxWaitingRuns:           maxWaiting,
		DatabasePoolMin:          int32(poolMin),
		DatabasePoolMax:          int32(poolMax),
		ShutdownTimeout:          10 * time.Second,
		MaintenanceInterval:      time.Duration(maintenanceInterval) * time.Second,
		LeaseTTL:                 time.Duration(leaseTTL) * time.Second,
	}
	if environment == "staging" || environment == "production" {
		if err := requireSecretFileOnly("PRD_AGENT_DATABASE_DSN", cfg.DatabaseDSN, environment); err != nil {
			return Config{}, err
		}
		if cfg.AllowDevPrincipal {
			return Config{}, fmt.Errorf("dev principal is forbidden in %s", environment)
		}
		if role == "api" {
			if cfg.AuthMode == "dev" {
				return Config{}, fmt.Errorf("dev auth mode is forbidden in %s", environment)
			}
			switch cfg.AuthMode {
			case "oidc":
				if cfg.OIDCIssuer == "" || cfg.OIDCAudience == "" {
					return Config{}, fmt.Errorf("OIDC issuer and audience are required in %s", environment)
				}
			case "proxy_allowlist":
				if err := validatePublicOrigin(cfg.PublicOrigin); err != nil {
					return Config{}, err
				}
				if len(cfg.ProxySecret) < 32 {
					return Config{}, fmt.Errorf("PRD_AGENT_PROXY_SECRET must contain at least 32 characters")
				}
				if err := requireSecretFileOnly("PRD_AGENT_PROXY_SECRET", cfg.ProxySecret, environment); err != nil {
					return Config{}, err
				}
				if len(cfg.AllowedUsers) == 0 {
					return Config{}, fmt.Errorf("PRD_AGENT_ALLOWED_USERS must contain at least one user")
				}
				if strings.TrimSpace(cfg.DefaultTenant) == "" {
					return Config{}, fmt.Errorf("PRD_AGENT_DEFAULT_TENANT is required")
				}
			}
			if err := requireSecretFileOnly("PRD_AGENT_EXPORT_CONFIRMATION_SECRET", cfg.ExportConfirmationSecret, environment); err != nil {
				return Config{}, err
			}
			if len(cfg.ExportConfirmationSecret) < 32 {
				return Config{}, fmt.Errorf("PRD_AGENT_EXPORT_CONFIRMATION_SECRET must contain at least 32 characters")
			}
		}
		if role == "integration" {
			for _, secret := range []struct {
				name  string
				value string
			}{
				{name: "PRD_AGENT_FEISHU_APP_ID", value: cfg.FeishuAppID},
				{name: "PRD_AGENT_FEISHU_APP_SECRET", value: cfg.FeishuAppSecret},
				{name: "PRD_AGENT_FEISHU_WIKI_NODE_TOKEN", value: cfg.FeishuWikiNodeToken},
			} {
				if err := requireSecretFileOnly(secret.name, secret.value, environment); err != nil {
					return Config{}, err
				}
			}
			if cfg.FeishuAppID == "" || cfg.FeishuAppSecret == "" || cfg.FeishuDocumentHost == "" || cfg.FeishuWikiNodeToken == "" {
				return Config{}, fmt.Errorf("complete fixed Wiki Feishu configuration is required in %s", environment)
			}
		}
		if role == "api" || role == "capability" || (role == "maintenance" && cfg.AgentDispatchMode == "go_rpc_pool") {
			if err := validateFixedRepository(cfg); err != nil {
				return Config{}, err
			}
		}
		if len(cfg.AgentWorkerEndpoints) > 0 || role == "capability" {
			if err := requireSecretFileOnly("PRD_AGENT_AGENT_RPC_TOKEN", cfg.AgentRPCToken, environment); err != nil {
				return Config{}, err
			}
			if len(cfg.AgentRPCToken) < 32 {
				return Config{}, fmt.Errorf("PRD_AGENT_AGENT_RPC_TOKEN must contain at least 32 characters")
			}
		}
	}
	return cfg, nil
}

func validateFixedRepository(cfg Config) error {
	if cfg.FixedRepositoryBindingID == "" {
		return fmt.Errorf("PRD_AGENT_FIXED_REPOSITORY_BINDING_ID is required")
	}
	parts := strings.Split(cfg.FixedRepository, "/")
	if len(parts) != 2 || strings.TrimSpace(parts[0]) == "" || strings.TrimSpace(parts[1]) == "" {
		return fmt.Errorf("PRD_AGENT_FIXED_REPOSITORY must use owner/name syntax")
	}
	if len(cfg.FixedRepositoryRevision) != 40 {
		return fmt.Errorf("PRD_AGENT_FIXED_REPOSITORY_REVISION must be a full commit SHA")
	}
	for _, value := range cfg.FixedRepositoryRevision {
		if !strings.ContainsRune("0123456789abcdef", value) {
			return fmt.Errorf("PRD_AGENT_FIXED_REPOSITORY_REVISION must be a lowercase hexadecimal commit SHA")
		}
	}
	return nil
}

func requireSecretFileOnly(name, value, environment string) error {
	if os.Getenv(name) != "" {
		return fmt.Errorf("%s must be provided only through %s_FILE in %s", name, name, environment)
	}
	if os.Getenv(name+"_FILE") == "" || value == "" {
		return fmt.Errorf("%s_FILE is required in %s", name, environment)
	}
	return nil
}

func validatePublicOrigin(value string) error {
	parsed, err := url.Parse(value)
	if err != nil || parsed.Scheme != "https" || parsed.Host == "" || parsed.User != nil ||
		parsed.RawQuery != "" || parsed.Fragment != "" || parsed.Path != "" {
		return fmt.Errorf("PRD_AGENT_PUBLIC_ORIGIN must be an HTTPS origin without path, query, credentials, or fragment")
	}
	return nil
}

func splitCSV(value string) []string {
	parts := strings.Split(value, ",")
	items := make([]string, 0, len(parts))
	for _, part := range parts {
		if trimmed := strings.TrimSpace(part); trimmed != "" {
			items = append(items, trimmed)
		}
	}
	return items
}

func env(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func secretEnv(name string) (string, error) {
	if value := os.Getenv(name); value != "" {
		return value, nil
	}
	if path := os.Getenv(name + "_FILE"); path != "" {
		value, err := os.ReadFile(path)
		if err != nil {
			return "", fmt.Errorf("%s_FILE cannot be read: %w", name, err)
		}
		secret := strings.TrimSpace(string(value))
		if secret == "" {
			return "", fmt.Errorf("%s_FILE is empty", name)
		}
		return secret, nil
	}
	return "", nil
}

func boolEnv(name string, fallback bool) (bool, error) {
	value := env(name, strconv.FormatBool(fallback))
	parsed, err := strconv.ParseBool(value)
	if err != nil {
		return false, fmt.Errorf("%s must be true or false", name)
	}
	return parsed, nil
}

func positiveInt(name string, fallback int) (int, error) {
	value := env(name, strconv.Itoa(fallback))
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed < 1 {
		return 0, fmt.Errorf("%s must be a positive integer", name)
	}
	return parsed, nil
}

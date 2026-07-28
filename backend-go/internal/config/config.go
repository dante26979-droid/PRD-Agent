package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	HTTPAddress            string
	AgentDispatchMode      string
	AgentWorkerEndpoints   []string
	AgentRPCMaxInflight    int
	AgentDispatchBatchSize int
	AgentRPCConnectTimeout time.Duration
	AgentRPCExecuteTimeout time.Duration
	DatabaseDSN            string
	BrokerURL              string
	Environment            string
	OIDCIssuer             string
	OIDCAudience           string
	AllowDevPrincipal      bool
	MaxGlobalRunnable      int
	MaxRunnablePerOwner    int
	DatabasePoolMin        int32
	DatabasePoolMax        int32
	ShutdownTimeout        time.Duration
	MaintenanceInterval    time.Duration
	LeaseTTL               time.Duration
}

func Load() (Config, error) {
	global, err := positiveInt("PRD_AGENT_MAX_GLOBAL_RUNNABLE", 30)
	if err != nil {
		return Config{}, err
	}
	perOwner, err := positiveInt("PRD_AGENT_MAX_RUNNABLE_PER_OWNER", 1)
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
	allowDevPrincipal, err := boolEnv("PRD_AGENT_GO_ALLOW_DEV_PRINCIPAL", env("PRD_AGENT_ENVIRONMENT", "local") != "production")
	if err != nil {
		return Config{}, err
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
	endpoints := splitCSV(os.Getenv("PRD_AGENT_GO_AGENT_RPC_WORKER_ENDPOINTS"))

	return Config{
		HTTPAddress:            env("PRD_AGENT_GO_HTTP_ADDRESS", ":8080"),
		AgentDispatchMode:      env("PRD_AGENT_GO_AGENT_DISPATCH_MODE", "go_rpc_pool"),
		AgentWorkerEndpoints:   endpoints,
		AgentRPCMaxInflight:    rpcMaxInflight,
		AgentDispatchBatchSize: dispatchBatch,
		AgentRPCConnectTimeout: time.Duration(durationEnv("PRD_AGENT_GO_AGENT_RPC_CONNECT_TIMEOUT_SECONDS", 5)) * time.Second,
		AgentRPCExecuteTimeout: time.Duration(durationEnv("PRD_AGENT_GO_AGENT_RPC_EXECUTE_TIMEOUT_SECONDS", 1800)) * time.Second,
		DatabaseDSN:            secretEnv("PRD_AGENT_DATABASE_DSN"),
		BrokerURL:              env("PRD_AGENT_BROKER_URL", ""),
		Environment:            env("PRD_AGENT_ENVIRONMENT", "local"),
		OIDCIssuer:             env("PRD_AGENT_OIDC_ISSUER", ""),
		OIDCAudience:           env("PRD_AGENT_OIDC_AUDIENCE", ""),
		AllowDevPrincipal:      allowDevPrincipal,
		MaxGlobalRunnable:      global,
		MaxRunnablePerOwner:    perOwner,
		DatabasePoolMin:        int32(poolMin),
		DatabasePoolMax:        int32(poolMax),
		ShutdownTimeout:        10 * time.Second,
		MaintenanceInterval:    time.Duration(durationEnv("PRD_AGENT_GO_MAINTENANCE_INTERVAL_SECONDS", 5)) * time.Second,
		LeaseTTL:               time.Duration(durationEnv("PRD_AGENT_GO_LEASE_TTL_SECONDS", 60)) * time.Second,
	}, nil
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

func secretEnv(name string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	if path := os.Getenv(name + "_FILE"); path != "" {
		value, err := os.ReadFile(path)
		if err == nil {
			return strings.TrimSpace(string(value))
		}
	}
	return ""
}

func durationEnv(name string, fallback int) int {
	value := env(name, strconv.Itoa(fallback))
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed < 1 {
		return fallback
	}
	return parsed
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

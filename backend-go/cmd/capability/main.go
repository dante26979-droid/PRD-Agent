package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/capability"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/config"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/storage"
	agentv1 "github.com/dante26979-droid/prd-agent/contracts/gen/go/agent/v1"
	"google.golang.org/grpc"
)

func main() {
	logger := slog.Default()
	if len(os.Args) == 3 && os.Args[1] == "healthcheck" {
		if err := healthcheck(os.Args[2]); err != nil {
			logger.Error("capability healthcheck failed", "error", err)
			os.Exit(1)
		}
		return
	}
	cfg, err := config.LoadFor("capability")
	if err != nil {
		logger.Error("load config", "error", err)
		os.Exit(1)
	}
	bindingID := strings.TrimSpace(os.Getenv("PRD_AGENT_FIXED_REPOSITORY_BINDING_ID"))
	repository := strings.TrimSpace(os.Getenv("PRD_AGENT_FIXED_REPOSITORY"))
	revision := strings.TrimSpace(os.Getenv("PRD_AGENT_FIXED_REPOSITORY_REVISION"))
	root := strings.TrimSpace(os.Getenv("PRD_AGENT_REPOSITORY_SNAPSHOT_ROOT"))
	if root == "" {
		root = "/repository"
	}
	address := strings.TrimSpace(os.Getenv("PRD_AGENT_CAPABILITY_GRPC_ADDRESS"))
	if address == "" {
		address = ":9200"
	}
	token, err := optionalSecret("PRD_AGENT_GITHUB_TOKEN")
	if err != nil {
		logger.Error("load GitHub token", "error", err)
		os.Exit(1)
	}
	store, err := storage.NewPostgresStore(context.Background(), storage.Config{
		DSN: cfg.DatabaseDSN, MinConns: 1, MaxConns: cfg.DatabasePoolMax,
		MaxGlobalRunnable: cfg.MaxGlobalRunnable, MaxRunnablePerOwner: cfg.MaxRunnablePerOwner,
		MaxWaitingRuns: cfg.MaxWaitingRuns,
	})
	if err != nil {
		logger.Error("connect database", "error", err)
		os.Exit(1)
	}
	defer store.Close()
	repositoryManager, err := capability.NewRepositoryManager(capability.RepositoryConfig{
		Repository: repository,
		Root:       root,
		Token:      token,
	})
	if err != nil {
		logger.Error("configure fixed GitHub repository", "error", err)
		os.Exit(1)
	}
	refreshContext, cancelRefresh := context.WithTimeout(context.Background(), 2*time.Minute)
	head, refreshErr := refreshRepositoryHead(
		refreshContext, store, repositoryManager, bindingID, repository, revision,
	)
	cancelRefresh()
	if refreshErr != nil && head.Revision == "" {
		logger.Error("GitHub refresh failed and no verified local repository copy is available",
			"repository", repository, "error", refreshErr)
		os.Exit(1)
	}
	if refreshErr != nil {
		logger.Warn("GitHub refresh failed; serving verified local repository copy",
			"repository", repository, "revision", head.Revision, "error", refreshErr)
	} else {
		logger.Info("refreshed GitHub default branch",
			"repository", repository, "branch", head.Branch, "revision", head.Revision)
	}
	server, err := capability.NewServer(store, repositoryManager, bindingID, cfg.AgentRPCToken)
	if err != nil {
		logger.Error("configure capability gateway", "error", err)
		os.Exit(1)
	}
	listener, err := net.Listen("tcp", address)
	if err != nil {
		logger.Error("listen", "address", address, "error", err)
		os.Exit(1)
	}
	grpcServer := grpc.NewServer(
		grpc.MaxRecvMsgSize(1<<20),
		grpc.MaxSendMsgSize(1<<20),
	)
	agentv1.RegisterCapabilityGatewayServiceServer(grpcServer, server)
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	go refreshLoop(ctx, logger, store, repositoryManager, bindingID, repository, revision, repositoryRefreshInterval())
	go func() {
		logger.Info("capability gateway listening",
			"address", address, "repository", repository,
			"branch", head.Branch, "revision", head.Revision)
		if err := grpcServer.Serve(listener); err != nil && !errors.Is(err, grpc.ErrServerStopped) {
			logger.Error("serve capability gateway", "error", err)
			stop()
		}
	}()
	<-ctx.Done()
	stopped := make(chan struct{})
	go func() {
		grpcServer.GracefulStop()
		close(stopped)
	}()
	select {
	case <-stopped:
	case <-time.After(cfg.ShutdownTimeout):
		grpcServer.Stop()
	}
}

func refreshRepositoryHead(
	ctx context.Context,
	store *storage.PostgresStore,
	manager *capability.RepositoryManager,
	bindingID, repository, fallbackRevision string,
) (capability.RepositoryHead, error) {
	attemptedAt := time.Now().UTC()
	head, err := manager.Refresh(ctx)
	if err != nil {
		localCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		local, localErr := manager.LocalHead(localCtx, fallbackRevision)
		if localErr != nil {
			return capability.RepositoryHead{}, fmt.Errorf("refresh repository and load local fallback: %w", err)
		}
		if recordErr := store.RecordRepositoryHead(localCtx, storage.RepositoryHead{
			BindingID:     bindingID,
			Repository:    repository,
			DefaultBranch: local.Branch,
			Revision:      local.Revision,
			LastSuccessAt: local.RefreshedAt,
			LastAttemptAt: attemptedAt,
		}); recordErr != nil {
			return capability.RepositoryHead{}, fmt.Errorf("publish local repository fallback HEAD: %w", recordErr)
		}
		_ = store.RecordRepositoryRefreshFailure(localCtx, bindingID, "FETCH_FAILED", attemptedAt)
		return local, err
	}
	if err := store.RecordRepositoryHead(ctx, storage.RepositoryHead{
		BindingID:     bindingID,
		Repository:    repository,
		DefaultBranch: head.Branch,
		Revision:      head.Revision,
		LastSuccessAt: head.RefreshedAt,
		LastAttemptAt: attemptedAt,
	}); err != nil {
		return capability.RepositoryHead{}, fmt.Errorf("publish repository HEAD: %w", err)
	}
	return head, nil
}

func refreshLoop(
	ctx context.Context,
	logger *slog.Logger,
	store *storage.PostgresStore,
	manager *capability.RepositoryManager,
	bindingID, repository, fallbackRevision string,
	interval time.Duration,
) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			refreshCtx, cancel := context.WithTimeout(ctx, min(interval, 2*time.Minute))
			head, err := refreshRepositoryHead(
				refreshCtx, store, manager, bindingID, repository, fallbackRevision,
			)
			cancel()
			if err != nil {
				logger.Warn("GitHub refresh failed; retaining verified local repository copy",
					"repository", repository, "revision", head.Revision, "error", err)
				continue
			}
			logger.Info("refreshed GitHub default branch",
				"repository", repository, "branch", head.Branch, "revision", head.Revision)
		}
	}
}

func repositoryRefreshInterval() time.Duration {
	const fallback = 30 * time.Second
	raw := strings.TrimSpace(os.Getenv("PRD_AGENT_REPOSITORY_REFRESH_INTERVAL_SECONDS"))
	if raw == "" {
		return fallback
	}
	seconds, err := strconv.Atoi(raw)
	if err != nil || seconds < 5 || seconds > 3600 {
		return fallback
	}
	return time.Duration(seconds) * time.Second
}

func healthcheck(address string) error {
	connection, err := net.DialTimeout("tcp", address, 3*time.Second)
	if err != nil {
		return err
	}
	return connection.Close()
}

func optionalSecret(name string) (string, error) {
	if path := strings.TrimSpace(os.Getenv(name + "_FILE")); path != "" {
		value, err := os.ReadFile(path)
		if err != nil {
			return "", fmt.Errorf("%s_FILE cannot be read: %w", name, err)
		}
		return strings.TrimSpace(string(value)), nil
	}
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		if os.Getenv("PRD_AGENT_ENVIRONMENT") == "production" {
			return "", fmt.Errorf("%s must use %s_FILE in production", name, name)
		}
		return value, nil
	}
	return "", nil
}

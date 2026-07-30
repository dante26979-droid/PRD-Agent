package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/agentpool"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/config"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/dispatcher"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/storage"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/transport"
)

func main() {
	logger := slog.Default()
	if len(os.Args) == 2 && os.Args[1] == "healthcheck" {
		if err := runHealthcheck(); err != nil {
			logger.Error("maintenance healthcheck failed", "error", err)
			os.Exit(1)
		}
		return
	}
	cfg, err := config.LoadFor("maintenance")
	if err != nil {
		logger.Error("load config", "error", err)
		os.Exit(1)
	}
	if cfg.DatabaseDSN == "" {
		logger.Error("database DSN is required for maintenance")
		os.Exit(1)
	}
	postgres, err := storage.NewPostgresStore(context.Background(), storage.Config{
		DSN: cfg.DatabaseDSN, MinConns: 1, MaxConns: cfg.DatabasePoolMax,
		MaxGlobalRunnable: cfg.MaxGlobalRunnable, MaxRunnablePerOwner: cfg.MaxRunnablePerOwner,
		MaxWaitingRuns:      cfg.MaxWaitingRuns,
		RepositoryBindingID: cfg.FixedRepositoryBindingID, RepositoryRevision: cfg.FixedRepositoryRevision,
	})
	if err != nil {
		logger.Error("connect database", "error", err)
		os.Exit(1)
	}
	defer postgres.Close()
	var publisher *transport.RedisStreamPublisher
	if cfg.BrokerURL != "" {
		publisher, err = transport.NewRedisStreamPublisher(cfg.BrokerURL)
		if err != nil {
			logger.Error("connect redis", "error", err)
			os.Exit(1)
		}
		defer publisher.Close()
	}
	var agentDispatcher *dispatcher.Dispatcher
	var pool *agentpool.Pool
	if cfg.AgentDispatchMode == "go_rpc_pool" {
		if len(cfg.AgentWorkerEndpoints) == 0 {
			logger.Error("agent worker endpoints are required for go_rpc_pool")
			os.Exit(1)
		}
		if _, ok := any(postgres).(runcontrol.DispatchStore); !ok {
			logger.Error("postgres store does not support dispatch store")
			os.Exit(1)
		}
		connectCtx, cancel := context.WithTimeout(context.Background(), cfg.AgentRPCConnectTimeout)
		pool, err = agentpool.NewGRPCPool(connectCtx, cfg.AgentWorkerEndpoints, cfg.AgentRPCMaxInflight, cfg.AgentRPCToken)
		cancel()
		if err != nil {
			logger.Error("connect agent worker pool", "error", err)
			os.Exit(1)
		}
		defer pool.Close()
		agentDispatcher, err = dispatcher.New(postgres, pool, cfg.LeaseTTL, cfg.AgentDispatchBatchSize)
		if err != nil {
			logger.Error("configure agent dispatcher", "error", err)
			os.Exit(1)
		}
		agentDispatcher.SetExecuteTimeout(cfg.AgentRPCExecuteTimeout)
		logger.Info("direct agent RPC dispatcher enabled", "workers", cfg.AgentWorkerEndpoints)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	logger.Info("go maintenance started", "interval", cfg.MaintenanceInterval)
	jobs := map[string]maintenanceJob{
		"admission": func(ctx context.Context) error {
			_, err := postgres.PromoteWaiting(ctx, time.Now().UTC())
			return err
		},
	}
	if publisher != nil {
		jobs["outbox"] = func(ctx context.Context) error {
			return publishOutbox(ctx, postgres, publisher, logger)
		}
	}
	if agentDispatcher != nil {
		jobs["agent-dispatch"] = func(ctx context.Context) error {
			report, err := agentDispatcher.DispatchOnce(ctx)
			if err == nil && (report.Processed > 0 || report.Saturated > 0) {
				logger.Info("direct agent dispatch cycle", "processed", report.Processed, "succeeded", report.Succeeded, "failed", report.Failed, "unknown", report.Unknown, "saturated", report.Saturated)
			}
			return err
		}
		jobs["agent-cancel"] = func(ctx context.Context) error {
			return agentDispatcher.CancelStopping(ctx, 10)
		}
		jobs["agent-recovery"] = func(ctx context.Context) error {
			report, err := agentDispatcher.RecoverUnknown(ctx, 10)
			if err == nil && report.Processed > 0 {
				logger.Info("direct agent recovery cycle", "processed", report.Processed, "succeeded", report.Succeeded, "failed", report.Failed, "unknown", report.Unknown)
			}
			return err
		}
	}
	runMaintenanceJobs(ctx, cfg.MaintenanceInterval, jobs, logger)
	if pool != nil {
		drainCtx, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
		defer cancel()
		if err := pool.Drain(drainCtx); err != nil {
			logger.Error("agent pool drain deadline exceeded", "error", err)
		}
	}
}

func runHealthcheck() error {
	cfg, err := config.LoadFor("maintenance")
	if err != nil {
		return err
	}
	if cfg.DatabaseDSN == "" {
		return errors.New("database DSN is required")
	}
	store, err := storage.NewPostgresStore(context.Background(), storage.Config{
		DSN: cfg.DatabaseDSN, MinConns: 1, MaxConns: 1,
		MaxGlobalRunnable: cfg.MaxGlobalRunnable, MaxRunnablePerOwner: cfg.MaxRunnablePerOwner,
		MaxWaitingRuns:      cfg.MaxWaitingRuns,
		RepositoryBindingID: cfg.FixedRepositoryBindingID, RepositoryRevision: cfg.FixedRepositoryRevision,
	})
	if err != nil {
		return err
	}
	defer store.Close()
	return store.Health(context.Background())
}

type maintenanceJob func(context.Context) error

func runMaintenanceJobs(ctx context.Context, interval time.Duration, jobs map[string]maintenanceJob, logger *slog.Logger) {
	var group sync.WaitGroup
	for name, job := range jobs {
		name, job := name, job
		group.Add(1)
		go func() {
			defer group.Done()
			runMaintenanceJob(ctx, interval, name, job, logger)
		}()
	}
	group.Wait()
}

func runMaintenanceJob(ctx context.Context, interval time.Duration, name string, job maintenanceJob, logger *slog.Logger) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		if err := invokeMaintenanceJob(ctx, job); err != nil && ctx.Err() == nil {
			logger.Error("maintenance job failed", "job", name, "error", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func invokeMaintenanceJob(ctx context.Context, job maintenanceJob) (err error) {
	defer func() {
		if recovered := recover(); recovered != nil {
			err = fmt.Errorf("panic recovered: %v", recovered)
		}
	}()
	return job(ctx)
}

func publishOutbox(ctx context.Context, store *storage.PostgresStore, publisher *transport.RedisStreamPublisher, logger *slog.Logger) error {
	messages, err := store.ClaimOutbox(ctx, 100, time.Now().UTC())
	if err != nil {
		return err
	}
	for _, message := range messages {
		if err := publisher.Publish(ctx, message); err != nil {
			next := time.Now().UTC().Add(backoff(message.Attempts))
			if markErr := store.MarkOutboxFailed(ctx, message.MessageID, next); markErr != nil {
				return errors.Join(err, markErr)
			}
			continue
		}
		if err := store.MarkOutboxPublished(ctx, message.MessageID, time.Now().UTC()); err != nil {
			return err
		}
		logger.Debug("published agent wake-up", "message_id", message.MessageID)
	}
	return nil
}

func backoff(attempts int) time.Duration {
	if attempts < 1 {
		attempts = 1
	}
	if attempts > 6 {
		attempts = 6
	}
	return time.Duration(1<<attempts) * time.Second
}

var _ runcontrol.Store = (*storage.PostgresStore)(nil)
var _ runcontrol.DispatchStore = (*storage.PostgresStore)(nil)

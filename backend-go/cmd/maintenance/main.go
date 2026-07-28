package main

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"os/signal"
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
	cfg, err := config.Load()
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
		pool, err = agentpool.NewGRPCPool(connectCtx, cfg.AgentWorkerEndpoints, cfg.AgentRPCMaxInflight)
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
	ticker := time.NewTicker(cfg.MaintenanceInterval)
	defer ticker.Stop()
	for {
		if err := reconcile(ctx, postgres, publisher, agentDispatcher, logger); err != nil {
			logger.Error("maintenance cycle failed", "error", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func reconcile(ctx context.Context, store *storage.PostgresStore, publisher *transport.RedisStreamPublisher, agentDispatcher *dispatcher.Dispatcher, logger *slog.Logger) error {
	if _, err := store.PromoteWaiting(ctx, time.Now().UTC()); err != nil {
		return err
	}
	if agentDispatcher != nil {
		if err := agentDispatcher.CancelStopping(ctx, 10); err != nil {
			logger.Error("direct agent cancellation failed", "error", err)
		}
		if report, err := agentDispatcher.DispatchOnce(ctx); err != nil {
			logger.Error("direct agent dispatch failed", "error", err)
		} else if report.Processed > 0 || report.Saturated > 0 {
			logger.Info("direct agent dispatch cycle", "processed", report.Processed, "succeeded", report.Succeeded, "failed", report.Failed, "unknown", report.Unknown, "saturated", report.Saturated)
		}
		if report, err := agentDispatcher.RecoverUnknown(ctx, 10); err != nil {
			logger.Error("direct agent recovery failed", "error", err)
		} else if report.Processed > 0 {
			logger.Info("direct agent recovery cycle", "processed", report.Processed, "succeeded", report.Succeeded, "failed", report.Failed, "unknown", report.Unknown)
		}
	}
	if publisher == nil {
		return nil
	}
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

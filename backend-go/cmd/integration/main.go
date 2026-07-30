package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/config"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/feishu"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/integration"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/storage"
)

func main() {
	logger := slog.Default()
	if len(os.Args) == 2 && os.Args[1] == "healthcheck" {
		if err := runHealthcheck(); err != nil {
			logger.Error("integration healthcheck failed", "error", err)
			os.Exit(1)
		}
		return
	}
	cfg, err := config.LoadFor("integration")
	if err != nil {
		logger.Error("load integration config", "error", err)
		os.Exit(1)
	}
	if cfg.DatabaseDSN == "" {
		logger.Error("database DSN is required for integration worker")
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
	client, err := feishu.NewClient(feishu.Config{
		AppID: cfg.FeishuAppID, AppSecret: cfg.FeishuAppSecret,
		DocumentHost: cfg.FeishuDocumentHost, WikiNodeToken: cfg.FeishuWikiNodeToken,
		BaseURL: cfg.FeishuBaseURL, Timeout: 15 * time.Second,
	}, &http.Client{Timeout: 15 * time.Second})
	if err != nil {
		logger.Error("configure fixed Wiki publisher", "error", err)
		os.Exit(1)
	}
	workerID := os.Getenv("PRD_AGENT_INTEGRATION_WORKER_ID")
	if workerID == "" {
		workerID = "integration-1"
	}
	worker, err := integration.NewWorker(store, client, workerID)
	if err != nil {
		logger.Error("configure integration worker", "error", err)
		os.Exit(1)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	ticker := time.NewTicker(cfg.MaintenanceInterval)
	defer ticker.Stop()
	for {
		if report, err := worker.ProcessOnce(ctx); err != nil && !errors.Is(err, context.Canceled) {
			logger.Error("process Feishu publish intent", "error", err)
		} else if report.Processed > 0 {
			logger.Info("processed Feishu publish intent", "report", report)
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func runHealthcheck() error {
	cfg, err := config.LoadFor("integration")
	if err != nil {
		return err
	}
	if cfg.DatabaseDSN == "" {
		return errors.New("database DSN is required")
	}
	store, err := storage.NewPostgresStore(context.Background(), storage.Config{
		DSN: cfg.DatabaseDSN, MinConns: 1, MaxConns: 1,
		MaxGlobalRunnable: cfg.MaxGlobalRunnable, MaxRunnablePerOwner: cfg.MaxRunnablePerOwner,
		MaxWaitingRuns: cfg.MaxWaitingRuns,
	})
	if err != nil {
		return err
	}
	defer store.Close()
	return store.Health(context.Background())
}

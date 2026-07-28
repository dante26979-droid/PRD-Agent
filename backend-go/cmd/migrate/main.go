package main

import (
	"context"
	"log/slog"
	"os"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/config"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/storage"
)

func main() {
	logger := slog.Default()
	cfg, err := config.Load()
	if err != nil {
		logger.Error("load config", "error", err)
		os.Exit(1)
	}
	if cfg.DatabaseDSN == "" {
		logger.Error("PRD_AGENT_DATABASE_DSN is required for migrations")
		os.Exit(1)
	}
	postgres, err := storage.NewPostgresStore(context.Background(), storage.Config{
		DSN: cfg.DatabaseDSN, MinConns: 1, MaxConns: 1,
	})
	if err != nil {
		logger.Error("connect database", "error", err)
		os.Exit(1)
	}
	defer postgres.Close()
	directory := os.Getenv("PRD_AGENT_GO_MIGRATIONS_DIR")
	if directory == "" {
		directory = "db/migrations"
	}
	if err := storage.ApplyMigrations(context.Background(), postgres.Pool(), directory); err != nil {
		logger.Error("apply migrations", "error", err)
		os.Exit(1)
	}
	logger.Info("migrations applied", "directory", directory)
}

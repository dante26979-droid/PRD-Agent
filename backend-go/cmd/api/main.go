package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/auth"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/config"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/httpapi"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/storage"
)

func main() {
	logger := slog.Default()
	cfg, err := config.Load()
	if err != nil {
		logger.Error("load config", "error", err)
		os.Exit(1)
	}

	var store runcontrol.Store
	if cfg.DatabaseDSN == "" {
		logger.Warn("database DSN is not configured; starting with memory store")
		store = runcontrol.NewMemoryStore(runcontrol.QueuePolicy{
			MaxGlobalRunnable:   cfg.MaxGlobalRunnable,
			MaxRunnablePerOwner: cfg.MaxRunnablePerOwner,
		})
	} else {
		postgres, err := storage.NewPostgresStore(context.Background(), storage.Config{
			DSN: cfg.DatabaseDSN, MinConns: cfg.DatabasePoolMin, MaxConns: cfg.DatabasePoolMax,
			MaxGlobalRunnable: cfg.MaxGlobalRunnable, MaxRunnablePerOwner: cfg.MaxRunnablePerOwner,
		})
		if err != nil {
			logger.Error("connect database", "error", err)
			os.Exit(1)
		}
		defer postgres.Close()
		store = postgres
	}
	var resolver httpapi.PrincipalResolver = httpapi.DevPrincipalResolver{}
	if !cfg.AllowDevPrincipal {
		if cfg.OIDCIssuer == "" || cfg.OIDCAudience == "" {
			logger.Error("OIDC issuer and audience are required when dev principal is disabled")
			os.Exit(1)
		}
		resolver, err = auth.NewOIDCResolver(context.Background(), cfg.OIDCIssuer, cfg.OIDCAudience)
		if err != nil {
			logger.Error("configure OIDC", "error", err)
			os.Exit(1)
		}
	}

	server := &http.Server{Addr: cfg.HTTPAddress, Handler: httpapi.NewRouterWithPrincipalResolver(store, resolver)}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	go func() {
		logger.Info("go control plane listening", "address", cfg.HTTPAddress)
		if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("serve", "error", err)
			stop()
		}
	}()
	<-ctx.Done()
	shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
	defer cancel()
	if err := server.Shutdown(shutdownCtx); err != nil {
		logger.Error("shutdown", "error", err)
	}
}

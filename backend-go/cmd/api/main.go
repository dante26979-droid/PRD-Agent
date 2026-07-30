package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/auth"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/config"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/httpapi"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/storage"
)

func main() {
	logger := slog.Default()
	if len(os.Args) == 3 && os.Args[1] == "healthcheck" {
		if err := runHealthcheck(os.Args[2]); err != nil {
			logger.Error("healthcheck failed", "error", err)
			os.Exit(1)
		}
		return
	}
	cfg, err := config.LoadFor("api")
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
			MaxWaitingRuns:      cfg.MaxWaitingRuns,
			ConfirmationSecret:  cfg.ExportConfirmationSecret,
		})
	} else {
		postgres, err := storage.NewPostgresStore(context.Background(), storage.Config{
			DSN: cfg.DatabaseDSN, MinConns: cfg.DatabasePoolMin, MaxConns: cfg.DatabasePoolMax,
			MaxGlobalRunnable: cfg.MaxGlobalRunnable, MaxRunnablePerOwner: cfg.MaxRunnablePerOwner,
			MaxWaitingRuns:      cfg.MaxWaitingRuns,
			ConfirmationSecret:  cfg.ExportConfirmationSecret,
			RepositoryBindingID: cfg.FixedRepositoryBindingID,
			RepositoryRevision:  cfg.FixedRepositoryRevision,
		})
		if err != nil {
			logger.Error("connect database", "error", err)
			os.Exit(1)
		}
		defer postgres.Close()
		store = postgres
	}
	var resolver httpapi.PrincipalResolver
	switch cfg.AuthMode {
	case "dev":
		resolver = httpapi.DevPrincipalResolver{}
	case "oidc":
		resolver, err = auth.NewOIDCResolver(context.Background(), cfg.OIDCIssuer, cfg.OIDCAudience)
		if err != nil {
			logger.Error("configure OIDC", "error", err)
			os.Exit(1)
		}
	case "proxy_allowlist":
		resolver, err = auth.NewProxyResolver(cfg.ProxySecret, cfg.DefaultTenant, cfg.AllowedUsers)
		if err != nil {
			logger.Error("configure proxy identity", "error", err)
			os.Exit(1)
		}
	default:
		logger.Error("unsupported authentication mode", "mode", cfg.AuthMode)
		os.Exit(1)
	}

	server := &http.Server{Addr: cfg.HTTPAddress, Handler: httpapi.NewSecureRouter(store, resolver, cfg.PublicOrigin)}
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

func runHealthcheck(endpoint string) error {
	client := &http.Client{Timeout: 3 * time.Second}
	return checkHealth(client, endpoint)
}

func checkHealth(client *http.Client, endpoint string) error {
	response, err := client.Get(endpoint)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("readiness endpoint returned %s", response.Status)
	}
	return nil
}

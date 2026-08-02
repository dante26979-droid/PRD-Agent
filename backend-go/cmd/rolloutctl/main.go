package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/config"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/storage"
)

func main() {
	if err := run(context.Background(), os.Args[1:]); err != nil {
		_ = json.NewEncoder(os.Stderr).Encode(map[string]string{"error": err.Error()})
		os.Exit(1)
	}
}

func run(ctx context.Context, args []string) error {
	if len(args) == 0 {
		return errors.New("usage: rolloutctl inspect|record-gate|transition|pause|resume|rollback|drain|readiness")
	}
	store, err := openStore(ctx)
	if err != nil {
		return err
	}
	defer store.Close()
	switch args[0] {
	case "inspect":
		state, err := store.InspectRolloutState(ctx)
		return encode(state, err)
	case "pause":
		return executeOperator(ctx, store, runcontrol.RolloutCommandPause, args[1:])
	case "resume":
		return executeOperator(ctx, store, runcontrol.RolloutCommandResume, args[1:])
	case "rollback":
		return executeOperator(ctx, store, runcontrol.RolloutCommandRollback, args[1:])
	case "record-gate":
		return executeGate(ctx, store, args[1:])
	case "transition":
		return executeTransition(ctx, store, args[1:])
	case "drain":
		return executeDrain(ctx, store, args[1:])
	case "readiness":
		return executeReadiness(ctx, store, args[1:])
	default:
		return fmt.Errorf("unknown command %q", args[0])
	}
}

func executeGate(ctx context.Context, store runcontrol.WorkflowRolloutControl, args []string) error {
	flags := flag.NewFlagSet("record-gate", flag.ContinueOnError)
	var manifestPath, reportPath string
	command := runcontrol.RolloutGateOperatorCommand{}
	flags.StringVar(&manifestPath, "manifest", "", "gate manifest JSON path")
	flags.StringVar(&reportPath, "report", "", "gate report JSON path")
	flags.StringVar(&command.CommandID, "idempotency-key", "", "stable command identity")
	flags.IntVar(&command.ExpectedVersion, "expected-version", 0, "expected rollout state version")
	flags.StringVar(&command.EvidenceHash, "evidence-hash", "", "hash of controlled evidence")
	flags.StringVar(&command.ActorRef, "actor-ref", "", "non-secret operator identity")
	flags.BoolVar(&command.Apply, "apply", false, "persist the validated command")
	if err := flags.Parse(args); err != nil {
		return err
	}
	manifestPayload, err := os.ReadFile(manifestPath)
	if err != nil {
		return fmt.Errorf("read manifest: %w", err)
	}
	if err := json.Unmarshal(manifestPayload, &command.Manifest); err != nil {
		return fmt.Errorf("decode manifest: %w", err)
	}
	reportPayload, err := os.ReadFile(reportPath)
	if err != nil {
		return fmt.Errorf("read report: %w", err)
	}
	if err := json.Unmarshal(reportPayload, &command.Report); err != nil {
		return fmt.Errorf("decode report: %w", err)
	}
	command.ManifestHash = contentHash(manifestPayload)
	command.Now = time.Now().UTC()
	result, err := store.ApplyGateCommand(ctx, command)
	return encode(result, err)
}

func executeTransition(ctx context.Context, store runcontrol.WorkflowRolloutControl, args []string) error {
	flags := flag.NewFlagSet("transition", flag.ContinueOnError)
	command := runcontrol.RolloutStageOperatorCommand{}
	var target string
	flags.StringVar(&target, "to", "", "target rollout stage")
	flags.StringVar(&command.CommandID, "idempotency-key", "", "stable command identity")
	flags.IntVar(&command.ExpectedVersion, "expected-version", 0, "expected rollout state version")
	flags.StringVar(&command.PolicyVersion, "policy-version", "", "policy to activate")
	flags.StringVar(&command.GateDecisionID, "gate-decision-id", "", "passed gate decision")
	flags.StringVar(&command.EvidenceHash, "evidence-hash", "", "hash of controlled evidence")
	flags.StringVar(&command.DrainID, "drain-id", "", "completed v1 drain required for LEGACY_RETIRED")
	flags.StringVar(&command.RemovalInventoryHash, "removal-inventory-hash", "", "hash-bound removed-symbol inventory")
	flags.StringVar(&command.ReaderObservationHash, "reader-observation-hash", "", "zero-hit reader observation evidence")
	flags.StringVar(&command.ActorRef, "actor-ref", "", "non-secret operator identity")
	flags.BoolVar(&command.Apply, "apply", false, "persist the validated command")
	if err := flags.Parse(args); err != nil {
		return err
	}
	command.Target, command.Now = runcontrol.RolloutStage(target), time.Now().UTC()
	result, err := store.ApplyStageCommand(ctx, command)
	return encode(result, err)
}

func executeOperator(ctx context.Context, store runcontrol.WorkflowRolloutControl, kind runcontrol.RolloutOperatorCommandKind, args []string) error {
	flags := flag.NewFlagSet(string(kind), flag.ContinueOnError)
	command := runcontrol.RolloutOperatorCommand{Kind: kind}
	flags.StringVar(&command.CommandID, "idempotency-key", "", "stable command identity")
	flags.IntVar(&command.ExpectedVersion, "expected-version", 0, "expected rollout state version")
	flags.StringVar(&command.ReasonCode, "reason-code", "", "low-cardinality reason code")
	flags.StringVar(&command.PolicyVersion, "policy-version", "", "safe rollback policy version")
	flags.StringVar(&command.GateDecisionID, "gate-decision-id", "", "passed gate decision for resume")
	flags.StringVar(&command.EvidenceHash, "evidence-hash", "", "hash of controlled evidence")
	flags.StringVar(&command.ActorRef, "actor-ref", "", "non-secret operator identity")
	flags.BoolVar(&command.Apply, "apply", false, "persist the validated command")
	if err := flags.Parse(args); err != nil {
		return err
	}
	command.Now = time.Now().UTC()
	result, err := store.ApplyRolloutCommand(ctx, command)
	return encode(result, err)
}

func executeDrain(ctx context.Context, store runcontrol.WorkflowRolloutControl, args []string) error {
	if len(args) == 0 {
		return errors.New("usage: rolloutctl drain begin|refresh")
	}
	switch args[0] {
	case "begin":
		flags := flag.NewFlagSet("drain begin", flag.ContinueOnError)
		var workflow, evidence string
		var apply bool
		flags.StringVar(&workflow, "workflow", "", "workflow to drain")
		flags.StringVar(&evidence, "evidence-hash", "", "drain evidence hash")
		flags.BoolVar(&apply, "apply", false, "persist drain record")
		if err := flags.Parse(args[1:]); err != nil {
			return err
		}
		if !apply {
			inventory, err := store.InspectLegacy(ctx, runcontrol.WorkflowVersion(workflow), time.Now().UTC())
			return encode(map[string]any{"applied": false, "inventory": inventory}, err)
		}
		record, err := store.BeginDrain(ctx, runcontrol.BeginDrainCommand{WorkflowVersion: runcontrol.WorkflowVersion(workflow), EvidenceHash: evidence, Now: time.Now().UTC()})
		return encode(record, err)
	case "refresh":
		flags := flag.NewFlagSet("drain refresh", flag.ContinueOnError)
		var drainID string
		flags.StringVar(&drainID, "drain-id", "", "durable drain identity")
		if err := flags.Parse(args[1:]); err != nil {
			return err
		}
		record, err := store.RefreshDrain(ctx, drainID, time.Now().UTC())
		return encode(record, err)
	default:
		return fmt.Errorf("unknown drain command %q", args[0])
	}
}

func executeReadiness(ctx context.Context, store runcontrol.WorkflowRolloutControl, args []string) error {
	if len(args) == 0 {
		return errors.New("usage: rolloutctl readiness render|record|verify")
	}
	if args[0] == "verify" {
		flags := flag.NewFlagSet("readiness verify", flag.ContinueOnError)
		var readinessID string
		flags.StringVar(&readinessID, "readiness-id", "", "durable readiness identity")
		if err := flags.Parse(args[1:]); err != nil {
			return err
		}
		result, err := store.VerifyReadiness(ctx, readinessID)
		return encode(result, err)
	}
	if args[0] != "render" && args[0] != "record" {
		return fmt.Errorf("unknown readiness command %q", args[0])
	}
	flags := flag.NewFlagSet("readiness "+args[0], flag.ContinueOnError)
	command := runcontrol.ReadinessOperatorCommand{Apply: args[0] == "record"}
	flags.StringVar(&command.CommandID, "idempotency-key", "", "stable command identity")
	flags.IntVar(&command.ExpectedVersion, "expected-version", 0, "expected rollout state version")
	flags.StringVar(&command.ActorRef, "actor-ref", "", "non-secret operator identity")
	flags.StringVar(&command.Input.CommitHash, "commit", "", "commit identity")
	flags.StringVar(&command.Input.MigrationSetHash, "migration-set-hash", "", "migration set hash")
	flags.StringVar(&command.Input.MigrationHead, "migration-head", "", "highest applied migration")
	flags.StringVar(&command.Input.ContractReportHash, "contract-report-hash", "", "contract report hash")
	flags.StringVar(&command.Input.PostgresReportHash, "postgres-report-hash", "", "PostgreSQL report hash")
	flags.StringVar(&command.Input.EvalManifestHash, "eval-manifest-hash", "", "evaluation manifest hash")
	flags.StringVar(&command.Input.ShadowPolicyHash, "shadow-policy-hash", "", "shadow policy hash")
	flags.StringVar(&command.Input.GateDecisionID, "gate-decision-id", "", "passed gate decision")
	if err := flags.Parse(args[1:]); err != nil {
		return err
	}
	command.Now = time.Now().UTC()
	result, err := store.ApplyReadinessCommand(ctx, command)
	return encode(result, err)
}

func contentHash(payload []byte) string {
	digest := sha256.Sum256(payload)
	return "sha256:" + hex.EncodeToString(digest[:])
}

func openStore(ctx context.Context) (*storage.PostgresStore, error) {
	cfg, err := config.LoadFor("maintenance")
	if err != nil {
		return nil, err
	}
	if cfg.DatabaseDSN == "" {
		return nil, errors.New("database DSN is required")
	}
	return storage.NewPostgresStore(ctx, storage.Config{
		DSN: cfg.DatabaseDSN, MinConns: 1, MaxConns: cfg.DatabasePoolMax,
		MaxGlobalRunnable: cfg.MaxGlobalRunnable, MaxRunnablePerOwner: cfg.MaxRunnablePerOwner,
		MaxWaitingRuns: cfg.MaxWaitingRuns, DefaultWorkflowVersion: runcontrol.WorkflowVersion(cfg.DefaultWorkflowVersion),
		RolloutPolicy: cfg.AgentRolloutPolicy,
	})
}

func encode(value any, err error) error {
	if err != nil {
		return err
	}
	return json.NewEncoder(os.Stdout).Encode(value)
}

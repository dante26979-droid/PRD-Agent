package storage

import (
	"context"
	"encoding/json"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/id"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/jackc/pgx/v5"
)

func (s *PostgresStore) RecordGateDecision(ctx context.Context, command runcontrol.RecordGateDecisionCommand) (runcontrol.GateDecisionRecord, error) {
	if command.ManifestHash == "" || command.EvidenceHash == "" || command.Report.ReportHash == "" || command.Report.ConfigHash == "" {
		return runcontrol.GateDecisionRecord{}, runcontrol.ErrInvalidPayload
	}
	decision := runcontrol.EvaluateRolloutGate(command.Manifest, command.Report)
	decisionID, err := id.New("rollout-gate")
	if err != nil {
		return runcontrol.GateDecisionRecord{}, err
	}
	if command.Now.IsZero() {
		command.Now = time.Now().UTC()
	}
	failedCodes, _ := json.Marshal(decision.FailedGateCodes)
	decisionValue := "FAILED"
	if decision.Passed {
		decisionValue = "PASSED"
	}
	_, err = s.pool.Exec(ctx, `INSERT INTO go_rollout_gate_decisions (decision_id,candidate_version,baseline_version,report_hash,gate_manifest_hash,decision,failed_gate_codes,created_at,dataset_hash,config_hash,gate_manifest_version,evidence_hash) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)`, decisionID, command.Report.Candidate, command.Report.Baseline, command.Report.ReportHash, command.ManifestHash, decisionValue, failedCodes, command.Now, command.Report.DatasetHash, command.Report.ConfigHash, command.Manifest.SchemaVersion, command.EvidenceHash)
	if err != nil {
		return runcontrol.GateDecisionRecord{}, err
	}
	return runcontrol.GateDecisionRecord{DecisionID: decisionID, Candidate: command.Report.Candidate, Baseline: command.Report.Baseline, DatasetHash: command.Report.DatasetHash, ConfigHash: command.Report.ConfigHash, ReportHash: command.Report.ReportHash, ManifestHash: command.ManifestHash, EvidenceHash: command.EvidenceHash, Decision: decision, CreatedAt: command.Now}, nil
}

func (s *PostgresStore) AuthorizeRolloutTransition(ctx context.Context, command runcontrol.RolloutTransitionCommand) (runcontrol.RolloutStageState, error) {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.RolloutStageState{}, err
	}
	defer tx.Rollback(ctx)
	now := time.Now().UTC()
	if _, err := tx.Exec(ctx, `INSERT INTO go_rollout_stage_state (singleton_key,stage,policy_version,evidence_hash,version,updated_at) VALUES ('agent-runtime','LOCAL_ONLY','','',1,$1) ON CONFLICT (singleton_key) DO NOTHING`, now); err != nil {
		return runcontrol.RolloutStageState{}, err
	}
	var current runcontrol.RolloutStageState
	if err := tx.QueryRow(ctx, `SELECT stage,policy_version,COALESCE(last_gate_decision_id,''),evidence_hash,version,updated_at,paused,pause_reason_code,COALESCE(rollback_policy_version,'') FROM go_rollout_stage_state WHERE singleton_key='agent-runtime' FOR UPDATE`).Scan(&current.Stage, &current.PolicyVersion, &current.LastGateDecisionID, &current.EvidenceHash, &current.Version, &current.UpdatedAt, &current.Paused, &current.PauseReasonCode, &current.RollbackPolicyVersion); err != nil {
		return runcontrol.RolloutStageState{}, err
	}
	if current.Version != command.ExpectedVersion {
		return runcontrol.RolloutStageState{}, runcontrol.ErrTaskVersionConflict
	}
	var gateDecision, evidenceHash string
	if err := tx.QueryRow(ctx, `SELECT decision,evidence_hash FROM go_rollout_gate_decisions WHERE decision_id=$1`, command.GateDecisionID).Scan(&gateDecision, &evidenceHash); err != nil {
		return runcontrol.RolloutStageState{}, mapNotFound(err)
	}
	if gateDecision != "PASSED" || evidenceHash != command.EvidenceHash || current.Paused || !runcontrol.ValidRolloutTransition(current.Stage, command.Target) {
		return runcontrol.RolloutStageState{}, runcontrol.ErrInvalidRunStatus
	}
	if command.PolicyVersion == "" {
		return runcontrol.RolloutStageState{}, runcontrol.ErrInvalidPayload
	}
	var policyExists bool
	if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM go_rollout_policies WHERE policy_version=$1)`, command.PolicyVersion).Scan(&policyExists); err != nil {
		return runcontrol.RolloutStageState{}, err
	}
	if !policyExists {
		return runcontrol.RolloutStageState{}, runcontrol.ErrNotFound
	}
	if _, err := tx.Exec(ctx, `UPDATE go_rollout_policies SET active=FALSE WHERE active=TRUE`); err != nil {
		return runcontrol.RolloutStageState{}, err
	}
	if _, err := tx.Exec(ctx, `UPDATE go_rollout_policies SET active=TRUE WHERE policy_version=$1`, command.PolicyVersion); err != nil {
		return runcontrol.RolloutStageState{}, err
	}
	current.Stage, current.PolicyVersion, current.LastGateDecisionID = command.Target, command.PolicyVersion, command.GateDecisionID
	current.EvidenceHash, current.Version, current.UpdatedAt = command.EvidenceHash, current.Version+1, now
	if _, err := tx.Exec(ctx, `UPDATE go_rollout_stage_state SET stage=$1,policy_version=$2,last_gate_decision_id=$3,evidence_hash=$4,version=$5,updated_at=$6 WHERE singleton_key='agent-runtime'`, current.Stage, current.PolicyVersion, current.LastGateDecisionID, current.EvidenceHash, current.Version, current.UpdatedAt); err != nil {
		return runcontrol.RolloutStageState{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.RolloutStageState{}, err
	}
	return current, nil
}

func (s *PostgresStore) InspectLegacy(ctx context.Context, workflow runcontrol.WorkflowVersion, now time.Time) (runcontrol.LegacyInventory, error) {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{AccessMode: pgx.ReadOnly})
	if err != nil {
		return runcontrol.LegacyInventory{}, err
	}
	defer tx.Rollback(ctx)
	value, err := inspectLegacyTx(ctx, tx, workflow, now)
	if err != nil {
		return runcontrol.LegacyInventory{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.LegacyInventory{}, err
	}
	return value, nil
}

func (s *PostgresStore) BeginDrain(ctx context.Context, command runcontrol.BeginDrainCommand) (runcontrol.DrainRecord, error) {
	if command.WorkflowVersion != runcontrol.WorkflowVersionV1 || command.EvidenceHash == "" {
		return runcontrol.DrainRecord{}, runcontrol.ErrInvalidPayload
	}
	if command.Now.IsZero() {
		command.Now = time.Now().UTC()
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.DrainRecord{}, err
	}
	defer tx.Rollback(ctx)
	inventory, err := inspectLegacyTx(ctx, tx, command.WorkflowVersion, command.Now)
	if err != nil {
		return runcontrol.DrainRecord{}, err
	}
	inventoryID, err := persistInventoryTx(ctx, tx, inventory, command.Now)
	if err != nil {
		return runcontrol.DrainRecord{}, err
	}
	drainID, err := id.New("rollout-drain")
	if err != nil {
		return runcontrol.DrainRecord{}, err
	}
	status := runcontrol.DrainBlocked
	var completedAt *time.Time
	if inventory.Empty() {
		status = runcontrol.DrainCompleted
		completed := command.Now
		completedAt = &completed
	}
	evidence, _ := json.Marshal(map[string]string{"evidence_hash": command.EvidenceHash})
	_, err = tx.Exec(ctx, `INSERT INTO go_rollout_drains (drain_id,workflow_version,status,active_run_count,active_dispatch_count,snapshot_count,evidence,started_at,completed_at,inventory_id,non_terminal_ledger_count,unpublished_outbox_count,reader_hit_count) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)`, drainID, command.WorkflowVersion, status, inventory.ActiveRunCount, inventory.ActiveDispatchCount, inventory.SnapshotCount, evidence, command.Now, completedAt, inventoryID, inventory.NonTerminalLedgerCount, inventory.UnpublishedOutboxCount, inventory.ReaderHitCount)
	if err != nil {
		return runcontrol.DrainRecord{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.DrainRecord{}, err
	}
	return runcontrol.DrainRecord{DrainID: drainID, WorkflowVersion: command.WorkflowVersion, Status: status, EvidenceHash: command.EvidenceHash, Inventory: inventory, StartedAt: command.Now, CompletedAt: completedAt}, nil
}

func (s *PostgresStore) RefreshDrain(ctx context.Context, drainID string, now time.Time) (runcontrol.DrainRecord, error) {
	if now.IsZero() {
		now = time.Now().UTC()
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.DrainRecord{}, err
	}
	defer tx.Rollback(ctx)
	var record runcontrol.DrainRecord
	var evidence []byte
	if err := tx.QueryRow(ctx, `SELECT workflow_version,status,evidence,started_at,completed_at FROM go_rollout_drains WHERE drain_id=$1 FOR UPDATE`, drainID).Scan(&record.WorkflowVersion, &record.Status, &evidence, &record.StartedAt, &record.CompletedAt); err != nil {
		return runcontrol.DrainRecord{}, mapNotFound(err)
	}
	record.DrainID = drainID
	var evidenceValue map[string]string
	_ = json.Unmarshal(evidence, &evidenceValue)
	record.EvidenceHash = evidenceValue["evidence_hash"]
	if record.Status == runcontrol.DrainCompleted {
		return record, tx.Commit(ctx)
	}
	inventory, err := inspectLegacyTx(ctx, tx, record.WorkflowVersion, now)
	if err != nil {
		return runcontrol.DrainRecord{}, err
	}
	inventoryID, err := persistInventoryTx(ctx, tx, inventory, now)
	if err != nil {
		return runcontrol.DrainRecord{}, err
	}
	record.Inventory = inventory
	if inventory.Empty() {
		record.Status = runcontrol.DrainCompleted
		completed := now
		record.CompletedAt = &completed
	} else {
		record.Status = runcontrol.DrainBlocked
	}
	_, err = tx.Exec(ctx, `UPDATE go_rollout_drains SET status=$2,active_run_count=$3,active_dispatch_count=$4,snapshot_count=$5,completed_at=$6,inventory_id=$7,non_terminal_ledger_count=$8,unpublished_outbox_count=$9,reader_hit_count=$10 WHERE drain_id=$1`, drainID, record.Status, inventory.ActiveRunCount, inventory.ActiveDispatchCount, inventory.SnapshotCount, record.CompletedAt, inventoryID, inventory.NonTerminalLedgerCount, inventory.UnpublishedOutboxCount, inventory.ReaderHitCount)
	if err != nil {
		return runcontrol.DrainRecord{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.DrainRecord{}, err
	}
	return record, nil
}

func inspectLegacyTx(ctx context.Context, tx pgx.Tx, workflow runcontrol.WorkflowVersion, now time.Time) (runcontrol.LegacyInventory, error) {
	if workflow != runcontrol.WorkflowVersionV1 {
		return runcontrol.LegacyInventory{}, runcontrol.ErrInvalidPayload
	}
	value := runcontrol.LegacyInventory{WorkflowVersion: workflow, ObservedAt: now}
	queries := []struct {
		target *int64
		sql    string
		args   []any
	}{
		{&value.ActiveRunCount, `SELECT COUNT(*) FROM go_agent_runs WHERE workflow_version=$1 AND status NOT IN ('SUCCEEDED','FAILED','STOPPED')`, []any{workflow}},
		{&value.ActiveDispatchCount, `SELECT COUNT(*) FROM go_agent_dispatches d JOIN go_agent_runs r ON r.run_id=d.run_id WHERE r.workflow_version=$1 AND d.status IN ('STARTED','RUNNING')`, []any{workflow}},
		{&value.UnpublishedOutboxCount, `SELECT COUNT(*) FROM go_outbox_messages o JOIN go_agent_runs r ON r.run_id=o.aggregate_id WHERE r.workflow_version=$1 AND o.published_at IS NULL`, []any{workflow}},
		{&value.NonTerminalLedgerCount, `SELECT COUNT(*) FROM go_run_ledger_entries l JOIN go_agent_runs r ON r.run_id=l.run_id WHERE r.workflow_version=$1 AND l.status IN ('RESERVED','CALL_STARTED','OUTCOME_UNKNOWN')`, []any{workflow}},
		{&value.SnapshotCount, `SELECT COUNT(*) FROM go_run_checkpoints c JOIN go_agent_runs r ON r.run_id=c.run_id WHERE r.workflow_version=$1 AND (r.status NOT IN ('SUCCEEDED','FAILED','STOPPED') OR c.created_at >= $2)`, []any{workflow, now.Add(-30 * 24 * time.Hour)}},
	}
	for _, query := range queries {
		if err := tx.QueryRow(ctx, query.sql, query.args...).Scan(query.target); err != nil {
			return runcontrol.LegacyInventory{}, err
		}
	}
	if err := tx.QueryRow(ctx, `SELECT COALESCE(SUM(hit_count),0) FROM go_legacy_reader_observations WHERE reader_kind LIKE 'v1%' AND observation_window && tstzrange($1,$2,'[)')`, now.Add(-24*time.Hour), now).Scan(&value.ReaderHitCount); err != nil {
		return runcontrol.LegacyInventory{}, err
	}
	payload, _ := json.Marshal(map[string]any{
		"workflow_version":          value.WorkflowVersion,
		"active_run_count":          value.ActiveRunCount,
		"active_dispatch_count":     value.ActiveDispatchCount,
		"unpublished_outbox_count":  value.UnpublishedOutboxCount,
		"non_terminal_ledger_count": value.NonTerminalLedgerCount,
		"snapshot_count":            value.SnapshotCount,
		"reader_hit_count":          value.ReaderHitCount,
	})
	value.InventoryHash = confirmationRequestHash(string(payload))
	return value, nil
}

func persistInventoryTx(ctx context.Context, tx pgx.Tx, inventory runcontrol.LegacyInventory, now time.Time) (string, error) {
	inventoryID, err := id.New("legacy-inventory")
	if err != nil {
		return "", err
	}
	counts, _ := json.Marshal(inventory)
	_, err = tx.Exec(ctx, `INSERT INTO go_legacy_inventory_snapshots (inventory_id,workflow_version,counts_json,inventory_hash,observed_from,observed_until,created_at) VALUES ($1,$2,$3,$4,$5,$5,$5)`, inventoryID, inventory.WorkflowVersion, counts, inventory.InventoryHash, now)
	return inventoryID, err
}

var _ runcontrol.WorkflowRolloutControl = (*PostgresStore)(nil)

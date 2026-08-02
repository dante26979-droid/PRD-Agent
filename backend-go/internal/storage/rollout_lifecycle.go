package storage

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/id"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/jackc/pgx/v5"
)

func (s *PostgresStore) ApplyGateCommand(ctx context.Context, command runcontrol.RolloutGateOperatorCommand) (runcontrol.RolloutGateOperatorResult, error) {
	requestHash, err := command.RequestHash()
	if err != nil {
		return runcontrol.RolloutGateOperatorResult{}, err
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.RolloutGateOperatorResult{}, err
	}
	defer tx.Rollback(ctx)
	var existingHash string
	var existingPayload []byte
	err = tx.QueryRow(ctx, `SELECT request_hash,result_json FROM go_rollout_commands WHERE command_id=$1`, command.CommandID).Scan(&existingHash, &existingPayload)
	if err == nil {
		if existingHash != requestHash {
			return runcontrol.RolloutGateOperatorResult{}, runcontrol.ErrInvalidIdempotency
		}
		var result runcontrol.RolloutGateOperatorResult
		if err := json.Unmarshal(existingPayload, &result); err != nil {
			return runcontrol.RolloutGateOperatorResult{}, err
		}
		result.Replayed = true
		return result, tx.Commit(ctx)
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.RolloutGateOperatorResult{}, err
	}
	state, err := lockRolloutState(ctx, tx, true)
	if err != nil {
		return runcontrol.RolloutGateOperatorResult{}, err
	}
	if state.Version != command.ExpectedVersion {
		return runcontrol.RolloutGateOperatorResult{}, runcontrol.ErrTaskVersionConflict
	}
	if command.Now.IsZero() {
		command.Now = time.Now().UTC()
	}
	decisionID, err := id.New("rollout-gate")
	if err != nil {
		return runcontrol.RolloutGateOperatorResult{}, err
	}
	decision := runcontrol.EvaluateRolloutGate(command.Manifest, command.Report)
	record := runcontrol.GateDecisionRecord{
		DecisionID: decisionID, Candidate: command.Report.Candidate, Baseline: command.Report.Baseline,
		DatasetHash: command.Report.DatasetHash, ConfigHash: command.Report.ConfigHash,
		ReportHash: command.Report.ReportHash, ManifestHash: command.ManifestHash,
		EvidenceHash: command.EvidenceHash, Decision: decision, CreatedAt: command.Now,
	}
	result := runcontrol.RolloutGateOperatorResult{CommandID: command.CommandID, RequestHash: requestHash, Applied: command.Apply, Record: record, State: state}
	if !command.Apply {
		return result, nil
	}
	failedCodes, _ := json.Marshal(decision.FailedGateCodes)
	decisionValue := "FAILED"
	if decision.Passed {
		decisionValue = "PASSED"
	}
	if _, err := tx.Exec(ctx, `INSERT INTO go_rollout_gate_decisions (decision_id,candidate_version,baseline_version,report_hash,gate_manifest_hash,decision,failed_gate_codes,created_at,dataset_hash,config_hash,gate_manifest_version,evidence_hash) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)`, record.DecisionID, record.Candidate, record.Baseline, record.ReportHash, record.ManifestHash, decisionValue, failedCodes, record.CreatedAt, record.DatasetHash, record.ConfigHash, command.Manifest.SchemaVersion, record.EvidenceHash); err != nil {
		return runcontrol.RolloutGateOperatorResult{}, err
	}
	if err := insertLifecycleCommand(ctx, tx, command.CommandID, runcontrol.RolloutCommandRecordGate, requestHash, command.ExpectedVersion, state.Version, command.EvidenceHash, command.ActorRef, result, command.Now); err != nil {
		return runcontrol.RolloutGateOperatorResult{}, err
	}
	return result, tx.Commit(ctx)
}

func (s *PostgresStore) ApplyStageCommand(ctx context.Context, command runcontrol.RolloutStageOperatorCommand) (runcontrol.RolloutStageOperatorResult, error) {
	requestHash, err := command.RequestHash()
	if err != nil {
		return runcontrol.RolloutStageOperatorResult{}, err
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.RolloutStageOperatorResult{}, err
	}
	defer tx.Rollback(ctx)
	var existingHash string
	var existingPayload []byte
	err = tx.QueryRow(ctx, `SELECT request_hash,result_json FROM go_rollout_commands WHERE command_id=$1`, command.CommandID).Scan(&existingHash, &existingPayload)
	if err == nil {
		if existingHash != requestHash {
			return runcontrol.RolloutStageOperatorResult{}, runcontrol.ErrInvalidIdempotency
		}
		var result runcontrol.RolloutStageOperatorResult
		if err := json.Unmarshal(existingPayload, &result); err != nil {
			return runcontrol.RolloutStageOperatorResult{}, err
		}
		result.Replayed = true
		return result, tx.Commit(ctx)
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.RolloutStageOperatorResult{}, err
	}
	current, err := lockRolloutState(ctx, tx, true)
	if err != nil {
		return runcontrol.RolloutStageOperatorResult{}, err
	}
	if current.Version != command.ExpectedVersion {
		return runcontrol.RolloutStageOperatorResult{}, runcontrol.ErrTaskVersionConflict
	}
	var decision, evidenceHash string
	if err := tx.QueryRow(ctx, `SELECT decision,evidence_hash FROM go_rollout_gate_decisions WHERE decision_id=$1`, command.GateDecisionID).Scan(&decision, &evidenceHash); err != nil {
		return runcontrol.RolloutStageOperatorResult{}, mapNotFound(err)
	}
	if current.Paused || decision != "PASSED" || evidenceHash != command.EvidenceHash || !runcontrol.ValidRolloutTransition(current.Stage, command.Target) {
		return runcontrol.RolloutStageOperatorResult{}, runcontrol.ErrInvalidRunStatus
	}
	var policyExists bool
	if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM go_rollout_policies WHERE policy_version=$1)`, command.PolicyVersion).Scan(&policyExists); err != nil {
		return runcontrol.RolloutStageOperatorResult{}, err
	}
	if !policyExists {
		return runcontrol.RolloutStageOperatorResult{}, runcontrol.ErrNotFound
	}
	if command.Target == runcontrol.RolloutLegacyRetired {
		var status string
		var activeRuns, activeDispatches, snapshots, nonTerminalLedger, unpublishedOutbox, readerHits int64
		if err := tx.QueryRow(ctx, `SELECT status,active_run_count,active_dispatch_count,snapshot_count,non_terminal_ledger_count,unpublished_outbox_count,reader_hit_count FROM go_rollout_drains WHERE drain_id=$1`, command.DrainID).Scan(
			&status, &activeRuns, &activeDispatches, &snapshots, &nonTerminalLedger, &unpublishedOutbox, &readerHits,
		); err != nil {
			return runcontrol.RolloutStageOperatorResult{}, mapNotFound(err)
		}
		if status != string(runcontrol.DrainCompleted) || activeRuns+activeDispatches+snapshots+nonTerminalLedger+unpublishedOutbox+readerHits != 0 {
			return runcontrol.RolloutStageOperatorResult{}, runcontrol.ErrInvalidRunStatus
		}
		var readerProof bool
		if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM go_legacy_reader_observations WHERE evidence_hash=$1 AND hit_count=0)`, command.ReaderObservationHash).Scan(&readerProof); err != nil {
			return runcontrol.RolloutStageOperatorResult{}, err
		}
		if !readerProof {
			return runcontrol.RolloutStageOperatorResult{}, runcontrol.ErrInvalidRunStatus
		}
	}
	if command.Now.IsZero() {
		command.Now = time.Now().UTC()
	}
	next := current
	next.Stage, next.PolicyVersion, next.LastGateDecisionID = command.Target, command.PolicyVersion, command.GateDecisionID
	next.EvidenceHash, next.Version, next.UpdatedAt = command.EvidenceHash, current.Version+1, command.Now
	result := runcontrol.RolloutStageOperatorResult{CommandID: command.CommandID, RequestHash: requestHash, Applied: command.Apply, State: next}
	if !command.Apply {
		return result, nil
	}
	if _, err := tx.Exec(ctx, `UPDATE go_rollout_policies SET active=FALSE WHERE active=TRUE`); err != nil {
		return runcontrol.RolloutStageOperatorResult{}, err
	}
	if _, err := tx.Exec(ctx, `UPDATE go_rollout_policies SET active=TRUE WHERE policy_version=$1`, command.PolicyVersion); err != nil {
		return runcontrol.RolloutStageOperatorResult{}, err
	}
	if _, err := tx.Exec(ctx, `UPDATE go_rollout_stage_state SET stage=$1,policy_version=$2,last_gate_decision_id=$3,evidence_hash=$4,version=$5,updated_at=$6 WHERE singleton_key='agent-runtime'`, next.Stage, next.PolicyVersion, next.LastGateDecisionID, next.EvidenceHash, next.Version, next.UpdatedAt); err != nil {
		return runcontrol.RolloutStageOperatorResult{}, err
	}
	if err := insertLifecycleCommand(ctx, tx, command.CommandID, runcontrol.RolloutCommandTransition, requestHash, command.ExpectedVersion, next.Version, command.EvidenceHash, command.ActorRef, result, command.Now); err != nil {
		return runcontrol.RolloutStageOperatorResult{}, err
	}
	return result, tx.Commit(ctx)
}

func (s *PostgresStore) ApplyReadinessCommand(ctx context.Context, command runcontrol.ReadinessOperatorCommand) (runcontrol.ReadinessOperatorResult, error) {
	requestHash, err := command.RequestHash()
	if err != nil {
		return runcontrol.ReadinessOperatorResult{}, err
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.ReadinessOperatorResult{}, err
	}
	defer tx.Rollback(ctx)
	var existingHash string
	var existingPayload []byte
	err = tx.QueryRow(ctx, `SELECT request_hash,result_json FROM go_rollout_commands WHERE command_id=$1`, command.CommandID).Scan(&existingHash, &existingPayload)
	if err == nil {
		if existingHash != requestHash {
			return runcontrol.ReadinessOperatorResult{}, runcontrol.ErrInvalidIdempotency
		}
		var result runcontrol.ReadinessOperatorResult
		if err := json.Unmarshal(existingPayload, &result); err != nil {
			return runcontrol.ReadinessOperatorResult{}, err
		}
		result.Replayed = true
		return result, tx.Commit(ctx)
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.ReadinessOperatorResult{}, err
	}
	state, err := lockRolloutState(ctx, tx, true)
	if err != nil {
		return runcontrol.ReadinessOperatorResult{}, err
	}
	if state.Version != command.ExpectedVersion {
		return runcontrol.ReadinessOperatorResult{}, runcontrol.ErrTaskVersionConflict
	}
	activePolicy := ""
	if err := tx.QueryRow(ctx, `SELECT COALESCE((SELECT policy_version FROM go_rollout_policies WHERE active=TRUE),'')`).Scan(&activePolicy); err != nil {
		return runcontrol.ReadinessOperatorResult{}, err
	}
	var gate *runcontrol.GateDecisionRecord
	if command.Input.GateDecisionID != "" {
		value, err := loadGateDecision(ctx, tx, command.Input.GateDecisionID)
		if err == nil {
			gate = &value
		} else if !errors.Is(err, pgx.ErrNoRows) {
			return runcontrol.ReadinessOperatorResult{}, err
		}
	}
	record, err := runcontrol.BuildReadinessRecord(command.Input, state, activePolicy, gate, command.Now)
	if err != nil {
		return runcontrol.ReadinessOperatorResult{}, err
	}
	result := runcontrol.ReadinessOperatorResult{CommandID: command.CommandID, RequestHash: requestHash, Applied: command.Apply, Record: record}
	if !command.Apply {
		return result, nil
	}
	blocking, _ := json.Marshal(record.BlockingGateCodes)
	if _, err := tx.Exec(ctx, `INSERT INTO go_rollout_readiness_records (readiness_id,schema_version,candidate_version,baseline_version,commit_hash,migration_set_hash,migration_head,contract_report_hash,postgres_report_hash,eval_manifest_hash,shadow_policy_hash,gate_decision_id,rollout_state_version,blocking_gate_codes,record_hash,status,created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,NULLIF($12,''),$13,$14,$15,$16,$17)`, record.ReadinessID, record.SchemaVersion, record.Candidate, record.Baseline, record.CommitHash, record.MigrationSetHash, record.MigrationHead, record.ContractReportHash, record.PostgresReportHash, record.EvalManifestHash, record.ShadowPolicyHash, record.GateDecisionID, record.RolloutStateVersion, blocking, record.RecordHash, record.Status, record.CreatedAt); err != nil {
		return runcontrol.ReadinessOperatorResult{}, err
	}
	if err := insertLifecycleCommand(ctx, tx, command.CommandID, runcontrol.RolloutCommandRecordReadiness, requestHash, command.ExpectedVersion, state.Version, record.RecordHash, command.ActorRef, result, record.CreatedAt); err != nil {
		return runcontrol.ReadinessOperatorResult{}, err
	}
	return result, tx.Commit(ctx)
}

func (s *PostgresStore) VerifyReadiness(ctx context.Context, readinessID string) (runcontrol.ReadinessVerification, error) {
	if readinessID == "" {
		return runcontrol.ReadinessVerification{}, runcontrol.ErrInvalidPayload
	}
	var record runcontrol.ReadinessRecord
	var blockers []byte
	if err := s.pool.QueryRow(ctx, `SELECT schema_version,candidate_version,baseline_version,commit_hash,migration_set_hash,migration_head,contract_report_hash,postgres_report_hash,eval_manifest_hash,shadow_policy_hash,COALESCE(gate_decision_id,''),rollout_state_version,blocking_gate_codes,record_hash,status,created_at FROM go_rollout_readiness_records WHERE readiness_id=$1`, readinessID).Scan(
		&record.SchemaVersion, &record.Candidate, &record.Baseline, &record.CommitHash, &record.MigrationSetHash, &record.MigrationHead,
		&record.ContractReportHash, &record.PostgresReportHash, &record.EvalManifestHash, &record.ShadowPolicyHash,
		&record.GateDecisionID, &record.RolloutStateVersion, &blockers, &record.RecordHash, &record.Status, &record.CreatedAt,
	); err != nil {
		return runcontrol.ReadinessVerification{}, mapNotFound(err)
	}
	record.ReadinessID = readinessID
	if err := json.Unmarshal(blockers, &record.BlockingGateCodes); err != nil {
		return runcontrol.ReadinessVerification{}, err
	}
	hash, err := runcontrol.ReadinessRecordHash(record)
	if err != nil {
		return runcontrol.ReadinessVerification{}, err
	}
	state, err := s.InspectRolloutState(ctx)
	if err != nil {
		return runcontrol.ReadinessVerification{}, err
	}
	return runcontrol.ReadinessVerification{ReadinessID: readinessID, Status: record.Status, RecordHash: record.RecordHash, Verified: hash == record.RecordHash, StateStale: state.Version != record.RolloutStateVersion}, nil
}

func loadGateDecision(ctx context.Context, query rowQuerier, decisionID string) (runcontrol.GateDecisionRecord, error) {
	var record runcontrol.GateDecisionRecord
	var decision string
	var failed []byte
	err := query.QueryRow(ctx, `SELECT candidate_version,baseline_version,dataset_hash,config_hash,report_hash,gate_manifest_hash,evidence_hash,decision,failed_gate_codes,created_at FROM go_rollout_gate_decisions WHERE decision_id=$1`, decisionID).Scan(
		&record.Candidate, &record.Baseline, &record.DatasetHash, &record.ConfigHash, &record.ReportHash,
		&record.ManifestHash, &record.EvidenceHash, &decision, &failed, &record.CreatedAt,
	)
	if err != nil {
		return runcontrol.GateDecisionRecord{}, err
	}
	record.DecisionID, record.Decision.Passed = decisionID, decision == "PASSED"
	if err := json.Unmarshal(failed, &record.Decision.FailedGateCodes); err != nil {
		return runcontrol.GateDecisionRecord{}, err
	}
	return record, nil
}

func insertLifecycleCommand(ctx context.Context, tx pgx.Tx, commandID string, kind runcontrol.RolloutOperatorCommandKind, requestHash string, expectedVersion, resultVersion int, evidenceHash, actorRef string, result any, now time.Time) error {
	payload, err := json.Marshal(result)
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `INSERT INTO go_rollout_commands (command_id,command_kind,request_hash,expected_state_version,result_state_version,evidence_hash,actor_ref,result_json,created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)`, commandID, kind, requestHash, expectedVersion, resultVersion, evidenceHash, actorRef, payload, now)
	return err
}

var _ runcontrol.WorkflowRolloutControl = (*PostgresStore)(nil)

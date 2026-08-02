package storage

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/id"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/jackc/pgx/v5"
)

func (s *PostgresStore) initializeBudgetState(ctx context.Context, tx pgx.Tx, runID string, now time.Time) error {
	if s.defaultExecutionLedgerVersion == "" {
		return nil
	}
	policy, err := json.Marshal(s.defaultRunBudget)
	if err != nil {
		return err
	}
	empty := []byte(`{}`)
	_, err = tx.Exec(ctx, `INSERT INTO go_run_budget_state (run_id,policy_hash,policy_json,reserved_json,consumed_json,overage_json,started_at,updated_at) VALUES ($1,$2,$3,$4,$4,$4,$5,$5)`, runID, hashBytes(policy), policy, empty, now)
	return err
}

func loadLedgerContext(ctx context.Context, tx pgx.Tx, input *runcontrol.AgentRunInput) error {
	input.ExecutionLedgerVersion = string(input.Run.ExecutionLedgerVersion)
	var policyJSON, consumedJSON []byte
	if err := tx.QueryRow(ctx, `SELECT policy_json,consumed_json FROM go_run_budget_state WHERE run_id=$1`, input.Run.RunID).Scan(&policyJSON, &consumedJSON); err != nil {
		return err
	}
	if err := json.Unmarshal(policyJSON, &input.RunBudget); err != nil {
		return err
	}
	if err := json.Unmarshal(consumedJSON, &input.ConsumedBudget); err != nil {
		return err
	}
	rows, err := tx.Query(ctx, `SELECT entry_id,run_id,operation_key,entry_kind,operation,request_hash,status,reservation_json,consumption_json,COALESCE(output_artifact_key,''),COALESCE(output_artifact_hash,''),evidence_refs,COALESCE(error_category,''),retryable,created_at,call_started_at,completed_at,updated_at FROM go_run_ledger_entries WHERE run_id=$1 ORDER BY created_at,entry_id`, input.Run.RunID)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		entry, err := scanLedgerEntry(rows)
		if err != nil {
			return err
		}
		input.LedgerEntries = append(input.LedgerEntries, entry)
	}
	return rows.Err()
}

func (s *PostgresStore) ReserveLedgerEntry(ctx context.Context, lease runcontrol.LeaseContext, entry runcontrol.LedgerEntry) (runcontrol.LedgerEntry, error) {
	if err := validateLedgerEntry(entry); err != nil {
		return runcontrol.LedgerEntry{}, err
	}
	if entry.EntryKind == runcontrol.LedgerEntryLocalTransition {
		return runcontrol.LedgerEntry{}, runcontrol.ErrLedgerConflict
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.LedgerEntry{}, err
	}
	defer tx.Rollback(ctx)
	if err := assertLedgerLease(ctx, tx, lease); err != nil {
		return runcontrol.LedgerEntry{}, err
	}
	if entry.EntryKind == runcontrol.LedgerEntryModel || entry.EntryKind == runcontrol.LedgerEntryCapability {
		var evaluationMode runcontrol.EvaluationMode
		if err := tx.QueryRow(ctx, `SELECT evaluation_mode FROM go_agent_rollout_assignments WHERE run_id=$1`, lease.RunID).Scan(&evaluationMode); err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return runcontrol.LedgerEntry{}, err
		} else if evaluationMode == runcontrol.EvaluationShadow {
			return runcontrol.LedgerEntry{}, fmt.Errorf("%w: SHADOW_REMOTE_EFFECT_FORBIDDEN", runcontrol.ErrNoRemoteEffects)
		}
	}
	existing, err := selectLedgerEntry(ctx, tx, lease.RunID, entry.OperationKey, true)
	if err == nil {
		if !sameLedgerIdentity(existing, entry) {
			return runcontrol.LedgerEntry{}, runcontrol.ErrInvalidIdempotency
		}
		if err := tx.Commit(ctx); err != nil {
			return runcontrol.LedgerEntry{}, err
		}
		return existing, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.LedgerEntry{}, err
	}
	policy, reserved, consumed, err := lockBudget(ctx, tx, lease.RunID)
	if err != nil {
		return runcontrol.LedgerEntry{}, err
	}
	now := time.Now().UTC()
	if exhausted, err := elapsedBudgetExhausted(ctx, tx, lease.RunID, policy, now); err != nil {
		return runcontrol.LedgerEntry{}, err
	} else if exhausted {
		return runcontrol.LedgerEntry{}, runcontrol.ErrBudgetExhausted
	}
	if !budgetAllows(policy, addBudget(reserved, consumed), entry.Reservation) {
		return runcontrol.LedgerEntry{}, runcontrol.ErrBudgetExhausted
	}
	entryID, err := id.New("ledger")
	if err != nil {
		return runcontrol.LedgerEntry{}, err
	}
	reservationJSON, _ := json.Marshal(entry.Reservation)
	emptyJSON := []byte(`{}`)
	if _, err := tx.Exec(ctx, `INSERT INTO go_run_ledger_entries (entry_id,run_id,operation_key,entry_kind,operation,request_hash,status,reservation_json,consumption_json,created_at,updated_at) VALUES ($1,$2,$3,$4,$5,$6,'RESERVED',$7,$8,$9,$9)`, entryID, lease.RunID, entry.OperationKey, entry.EntryKind, entry.Operation, entry.RequestHash, reservationJSON, emptyJSON, now); err != nil {
		return runcontrol.LedgerEntry{}, err
	}
	reserved = addBudget(reserved, entry.Reservation)
	if err := updateBudget(ctx, tx, lease.RunID, reserved, consumed, policy, now); err != nil {
		return runcontrol.LedgerEntry{}, err
	}
	if entry.EntryKind == runcontrol.LedgerEntryModel {
		attemptID, err := id.New("attempt")
		if err != nil {
			return runcontrol.LedgerEntry{}, err
		}
		if _, err := tx.Exec(ctx, `INSERT INTO go_model_attempts (attempt_id,run_id,attempt_key,operation,request_hash,status,response_metadata_json,token_usage_json,created_at) VALUES ($1,$2,$3,$4,$5,'PLANNED','{}','{}',$6) ON CONFLICT (run_id,attempt_key) DO NOTHING`, attemptID, lease.RunID, entry.OperationKey, entry.Operation, entry.RequestHash, now); err != nil {
			return runcontrol.LedgerEntry{}, err
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.LedgerEntry{}, err
	}
	entry.EntryID, entry.RunID, entry.Status, entry.CreatedAt, entry.UpdatedAt = entryID, lease.RunID, runcontrol.LedgerReserved, now, now
	return entry, nil
}

func (s *PostgresStore) MarkLedgerCallStarted(ctx context.Context, lease runcontrol.LeaseContext, operationKey, requestHash string) (runcontrol.LedgerEntry, error) {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.LedgerEntry{}, err
	}
	defer tx.Rollback(ctx)
	if err := assertLedgerLease(ctx, tx, lease); err != nil {
		return runcontrol.LedgerEntry{}, err
	}
	entry, err := selectLedgerEntry(ctx, tx, lease.RunID, operationKey, true)
	if err != nil {
		return runcontrol.LedgerEntry{}, mapNotFound(err)
	}
	if entry.RequestHash != requestHash {
		return runcontrol.LedgerEntry{}, runcontrol.ErrInvalidIdempotency
	}
	if entry.Status != runcontrol.LedgerReserved && entry.Status != runcontrol.LedgerCallStarted {
		return runcontrol.LedgerEntry{}, runcontrol.ErrLedgerConflict
	}
	if entry.Status == runcontrol.LedgerReserved {
		now := time.Now().UTC()
		if _, err := tx.Exec(ctx, `UPDATE go_run_ledger_entries SET status='CALL_STARTED',call_started_at=$3,updated_at=$3 WHERE run_id=$1 AND operation_key=$2`, lease.RunID, operationKey, now); err != nil {
			return runcontrol.LedgerEntry{}, err
		}
		entry.Status, entry.CallStartedAt, entry.UpdatedAt = runcontrol.LedgerCallStarted, &now, now
	}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.LedgerEntry{}, err
	}
	return entry, nil
}

func (s *PostgresStore) FinishLedgerEntry(ctx context.Context, lease runcontrol.LeaseContext, result runcontrol.LedgerFinish) (runcontrol.LedgerEntry, error) {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.LedgerEntry{}, err
	}
	defer tx.Rollback(ctx)
	if err := assertLedgerLease(ctx, tx, lease); err != nil {
		return runcontrol.LedgerEntry{}, err
	}
	entry, err := selectLedgerEntry(ctx, tx, lease.RunID, result.OperationKey, true)
	if err != nil {
		return runcontrol.LedgerEntry{}, mapNotFound(err)
	}
	if entry.RequestHash != result.RequestHash {
		return runcontrol.LedgerEntry{}, runcontrol.ErrInvalidIdempotency
	}
	if entry.Status == runcontrol.LedgerSucceeded || entry.Status == runcontrol.LedgerFailed || entry.Status == runcontrol.LedgerOutcomeUnknown {
		if !sameLedgerFinish(entry, result) {
			return runcontrol.LedgerEntry{}, runcontrol.ErrLedgerConflict
		}
		if err := tx.Commit(ctx); err != nil {
			return runcontrol.LedgerEntry{}, err
		}
		return entry, nil
	}
	if entry.Status != runcontrol.LedgerCallStarted || (result.Status != runcontrol.LedgerSucceeded && result.Status != runcontrol.LedgerFailed && result.Status != runcontrol.LedgerOutcomeUnknown) {
		return runcontrol.LedgerEntry{}, runcontrol.ErrLedgerConflict
	}
	if result.Status == runcontrol.LedgerSucceeded && result.OutputArtifactKey == "" {
		return runcontrol.LedgerEntry{}, runcontrol.ErrLedgerConflict
	}
	if result.Status == runcontrol.LedgerSucceeded && result.OutputArtifactKey != "" {
		var requestHash, contentHash string
		if err := tx.QueryRow(ctx, `SELECT request_hash,content_hash FROM go_run_artifacts WHERE run_id=$1 AND artifact_key=$2 FOR SHARE`, lease.RunID, result.OutputArtifactKey).Scan(&requestHash, &contentHash); err != nil {
			return runcontrol.LedgerEntry{}, mapNotFound(err)
		}
		if requestHash != result.RequestHash || contentHash != result.OutputArtifactHash {
			return runcontrol.LedgerEntry{}, runcontrol.ErrLedgerConflict
		}
	}
	if result.Status == runcontrol.LedgerSucceeded && len(result.EvidenceRefs) > 0 {
		rows, err := tx.Query(ctx, `SELECT source_type,source_id,locator,excerpt_hash FROM go_evidence WHERE run_id=$1`, lease.RunID)
		if err != nil {
			return runcontrol.LedgerEntry{}, err
		}
		durable := make(map[string]struct{})
		for rows.Next() {
			var item runcontrol.EvidenceItem
			if err := rows.Scan(&item.SourceType, &item.SourceID, &item.Locator, &item.ExcerptHash); err != nil {
				rows.Close()
				return runcontrol.LedgerEntry{}, err
			}
			durable[runcontrol.EvidenceReference(item)] = struct{}{}
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return runcontrol.LedgerEntry{}, err
		}
		rows.Close()
		for _, ref := range result.EvidenceRefs {
			if _, ok := durable[ref]; !ok {
				return runcontrol.LedgerEntry{}, runcontrol.ErrLedgerConflict
			}
		}
	}
	policy, reserved, consumed, err := lockBudget(ctx, tx, lease.RunID)
	if err != nil {
		return runcontrol.LedgerEntry{}, err
	}
	reserved = subtractBudgetFloor(reserved, entry.Reservation)
	consumed = addBudget(consumed, result.Consumption)
	now := time.Now().UTC()
	consumptionJSON, _ := json.Marshal(result.Consumption)
	evidenceRefs := result.EvidenceRefs
	if evidenceRefs == nil {
		evidenceRefs = []string{}
	}
	if _, err := tx.Exec(ctx, `UPDATE go_run_ledger_entries SET status=$3,consumption_json=$4,output_artifact_key=NULLIF($5,''),output_artifact_hash=NULLIF($6,''),evidence_refs=$7,error_category=NULLIF($8,''),retryable=$9,completed_at=$10,updated_at=$10 WHERE run_id=$1 AND operation_key=$2`, lease.RunID, result.OperationKey, result.Status, consumptionJSON, result.OutputArtifactKey, result.OutputArtifactHash, evidenceRefs, result.ErrorCategory, result.Retryable, now); err != nil {
		return runcontrol.LedgerEntry{}, err
	}
	if err := updateBudget(ctx, tx, lease.RunID, reserved, consumed, policy, now); err != nil {
		return runcontrol.LedgerEntry{}, err
	}
	if entry.EntryKind == runcontrol.LedgerEntryModel {
		if _, err := tx.Exec(ctx, `UPDATE go_model_attempts SET status=$3,error_category=NULLIF($4,'') WHERE run_id=$1 AND attempt_key=$2`, lease.RunID, entry.OperationKey, result.Status, result.ErrorCategory); err != nil {
			return runcontrol.LedgerEntry{}, err
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.LedgerEntry{}, err
	}
	entry.Status, entry.Consumption, entry.OutputArtifactKey, entry.OutputArtifactHash = result.Status, result.Consumption, result.OutputArtifactKey, result.OutputArtifactHash
	entry.EvidenceRefs, entry.ErrorCategory, entry.Retryable, entry.CompletedAt, entry.UpdatedAt = append([]string(nil), result.EvidenceRefs...), result.ErrorCategory, result.Retryable, &now, now
	return entry, nil
}

func (s *PostgresStore) ConsumeLocalLedgerEntry(ctx context.Context, lease runcontrol.LeaseContext, entry runcontrol.LedgerEntry) (runcontrol.LedgerEntry, error) {
	if err := validateLedgerEntry(entry); err != nil || entry.EntryKind != runcontrol.LedgerEntryLocalTransition {
		if err != nil {
			return runcontrol.LedgerEntry{}, err
		}
		return runcontrol.LedgerEntry{}, runcontrol.ErrLedgerConflict
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.LedgerEntry{}, err
	}
	defer tx.Rollback(ctx)
	if err := assertLedgerLease(ctx, tx, lease); err != nil {
		return runcontrol.LedgerEntry{}, err
	}
	existing, err := selectLedgerEntry(ctx, tx, lease.RunID, entry.OperationKey, true)
	if err == nil {
		if !sameLedgerIdentity(existing, entry) {
			return runcontrol.LedgerEntry{}, runcontrol.ErrInvalidIdempotency
		}
		if err := tx.Commit(ctx); err != nil {
			return runcontrol.LedgerEntry{}, err
		}
		return existing, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.LedgerEntry{}, err
	}
	policy, reserved, consumed, err := lockBudget(ctx, tx, lease.RunID)
	if err != nil {
		return runcontrol.LedgerEntry{}, err
	}
	now := time.Now().UTC()
	if exhausted, err := elapsedBudgetExhausted(ctx, tx, lease.RunID, policy, now); err != nil {
		return runcontrol.LedgerEntry{}, err
	} else if exhausted {
		return runcontrol.LedgerEntry{}, runcontrol.ErrBudgetExhausted
	}
	if !budgetAllows(policy, consumed, entry.Consumption) {
		return runcontrol.LedgerEntry{}, runcontrol.ErrBudgetExhausted
	}
	entryID, err := id.New("ledger")
	if err != nil {
		return runcontrol.LedgerEntry{}, err
	}
	empty, _ := json.Marshal(runcontrol.BudgetDelta{})
	consumption, _ := json.Marshal(entry.Consumption)
	if _, err := tx.Exec(ctx, `INSERT INTO go_run_ledger_entries (entry_id,run_id,operation_key,entry_kind,operation,request_hash,status,reservation_json,consumption_json,created_at,completed_at,updated_at) VALUES ($1,$2,$3,$4,$5,$6,'SUCCEEDED',$7,$8,$9,$9,$9)`, entryID, lease.RunID, entry.OperationKey, entry.EntryKind, entry.Operation, entry.RequestHash, empty, consumption, now); err != nil {
		return runcontrol.LedgerEntry{}, err
	}
	consumed = addBudget(consumed, entry.Consumption)
	if err := updateBudget(ctx, tx, lease.RunID, reserved, consumed, policy, now); err != nil {
		return runcontrol.LedgerEntry{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.LedgerEntry{}, err
	}
	entry.EntryID, entry.RunID, entry.Status, entry.CreatedAt, entry.CompletedAt, entry.UpdatedAt = entryID, lease.RunID, runcontrol.LedgerSucceeded, now, &now, now
	return entry, nil
}

func assertLedgerLease(ctx context.Context, tx pgx.Tx, lease runcontrol.LeaseContext) error {
	if err := assertLease(ctx, tx, lease, time.Now().UTC()); err != nil {
		return err
	}
	var version string
	if err := tx.QueryRow(ctx, `SELECT COALESCE(execution_ledger_version,'') FROM go_agent_runs WHERE run_id=$1 FOR SHARE`, lease.RunID).Scan(&version); err != nil {
		return err
	}
	if version != string(runcontrol.ExecutionLedgerVersionV1) {
		return runcontrol.ErrLedgerConflict
	}
	return nil
}

func selectLedgerEntry(ctx context.Context, tx pgx.Tx, runID, operationKey string, lock bool) (runcontrol.LedgerEntry, error) {
	query := `SELECT entry_id,run_id,operation_key,entry_kind,operation,request_hash,status,reservation_json,consumption_json,COALESCE(output_artifact_key,''),COALESCE(output_artifact_hash,''),evidence_refs,COALESCE(error_category,''),retryable,created_at,call_started_at,completed_at,updated_at FROM go_run_ledger_entries WHERE run_id=$1 AND operation_key=$2`
	if lock {
		query += ` FOR UPDATE`
	}
	return scanLedgerEntry(tx.QueryRow(ctx, query, runID, operationKey))
}

type ledgerScanner interface{ Scan(dest ...any) error }

func scanLedgerEntry(row ledgerScanner) (runcontrol.LedgerEntry, error) {
	var entry runcontrol.LedgerEntry
	var reservation, consumption []byte
	err := row.Scan(&entry.EntryID, &entry.RunID, &entry.OperationKey, &entry.EntryKind, &entry.Operation, &entry.RequestHash, &entry.Status, &reservation, &consumption, &entry.OutputArtifactKey, &entry.OutputArtifactHash, &entry.EvidenceRefs, &entry.ErrorCategory, &entry.Retryable, &entry.CreatedAt, &entry.CallStartedAt, &entry.CompletedAt, &entry.UpdatedAt)
	if err != nil {
		return entry, err
	}
	if err := json.Unmarshal(reservation, &entry.Reservation); err != nil {
		return entry, err
	}
	if err := json.Unmarshal(consumption, &entry.Consumption); err != nil {
		return entry, err
	}
	return entry, nil
}

func lockBudget(ctx context.Context, tx pgx.Tx, runID string) (runcontrol.RunBudget, runcontrol.BudgetDelta, runcontrol.BudgetDelta, error) {
	var policyJSON, reservedJSON, consumedJSON []byte
	if err := tx.QueryRow(ctx, `SELECT policy_json,reserved_json,consumed_json FROM go_run_budget_state WHERE run_id=$1 FOR UPDATE`, runID).Scan(&policyJSON, &reservedJSON, &consumedJSON); err != nil {
		return runcontrol.RunBudget{}, runcontrol.BudgetDelta{}, runcontrol.BudgetDelta{}, err
	}
	var policy runcontrol.RunBudget
	var reserved, consumed runcontrol.BudgetDelta
	if err := json.Unmarshal(policyJSON, &policy); err != nil {
		return policy, reserved, consumed, err
	}
	if err := json.Unmarshal(reservedJSON, &reserved); err != nil {
		return policy, reserved, consumed, err
	}
	if err := json.Unmarshal(consumedJSON, &consumed); err != nil {
		return policy, reserved, consumed, err
	}
	return policy, reserved, consumed, nil
}

func updateBudget(ctx context.Context, tx pgx.Tx, runID string, reserved, consumed runcontrol.BudgetDelta, policy runcontrol.RunBudget, now time.Time) error {
	reservedJSON, _ := json.Marshal(reserved)
	consumedJSON, _ := json.Marshal(consumed)
	overageJSON, _ := json.Marshal(budgetOverage(policy, consumed))
	_, err := tx.Exec(ctx, `UPDATE go_run_budget_state SET reserved_json=$2,consumed_json=$3,overage_json=$4,updated_at=$5 WHERE run_id=$1`, runID, reservedJSON, consumedJSON, overageJSON, now)
	return err
}

func elapsedBudgetExhausted(ctx context.Context, tx pgx.Tx, runID string, policy runcontrol.RunBudget, now time.Time) (bool, error) {
	var startedAt time.Time
	if err := tx.QueryRow(ctx, `SELECT started_at FROM go_run_budget_state WHERE run_id=$1`, runID).Scan(&startedAt); err != nil {
		return false, err
	}
	return now.Sub(startedAt).Milliseconds() > policy.MaxElapsedMS, nil
}

func validateLedgerEntry(entry runcontrol.LedgerEntry) error {
	if entry.OperationKey == "" || entry.Operation == "" || entry.RequestHash == "" || (entry.EntryKind != runcontrol.LedgerEntryModel && entry.EntryKind != runcontrol.LedgerEntryCapability && entry.EntryKind != runcontrol.LedgerEntryLocalTransition) {
		return runcontrol.ErrInvalidPayload
	}
	return nil
}

func sameLedgerIdentity(left, right runcontrol.LedgerEntry) bool {
	return left.OperationKey == right.OperationKey && left.EntryKind == right.EntryKind && left.Operation == right.Operation && left.RequestHash == right.RequestHash
}
func sameLedgerFinish(entry runcontrol.LedgerEntry, result runcontrol.LedgerFinish) bool {
	return entry.Status == result.Status && entry.Consumption == result.Consumption && entry.OutputArtifactKey == result.OutputArtifactKey && entry.OutputArtifactHash == result.OutputArtifactHash && entry.ErrorCategory == result.ErrorCategory && entry.Retryable == result.Retryable && strings.Join(entry.EvidenceRefs, "\x00") == strings.Join(result.EvidenceRefs, "\x00")
}
func addBudget(a, b runcontrol.BudgetDelta) runcontrol.BudgetDelta {
	return runcontrol.BudgetDelta{ModelAttempts: a.ModelAttempts + b.ModelAttempts, ToolCalls: a.ToolCalls + b.ToolCalls, Iterations: a.Iterations + b.Iterations, Replans: a.Replans + b.Replans, Supplements: a.Supplements + b.Supplements, QualityRepairs: a.QualityRepairs + b.QualityRepairs, InputTokens: a.InputTokens + b.InputTokens, OutputTokens: a.OutputTokens + b.OutputTokens, ElapsedMS: a.ElapsedMS + b.ElapsedMS}
}
func subtractBudgetFloor(a, b runcontrol.BudgetDelta) runcontrol.BudgetDelta {
	value := runcontrol.BudgetDelta{ModelAttempts: a.ModelAttempts - b.ModelAttempts, ToolCalls: a.ToolCalls - b.ToolCalls, Iterations: a.Iterations - b.Iterations, Replans: a.Replans - b.Replans, Supplements: a.Supplements - b.Supplements, QualityRepairs: a.QualityRepairs - b.QualityRepairs, InputTokens: a.InputTokens - b.InputTokens, OutputTokens: a.OutputTokens - b.OutputTokens, ElapsedMS: a.ElapsedMS - b.ElapsedMS}
	if value.ModelAttempts < 0 {
		value.ModelAttempts = 0
	}
	if value.ToolCalls < 0 {
		value.ToolCalls = 0
	}
	if value.Iterations < 0 {
		value.Iterations = 0
	}
	if value.Replans < 0 {
		value.Replans = 0
	}
	if value.Supplements < 0 {
		value.Supplements = 0
	}
	if value.QualityRepairs < 0 {
		value.QualityRepairs = 0
	}
	if value.InputTokens < 0 {
		value.InputTokens = 0
	}
	if value.OutputTokens < 0 {
		value.OutputTokens = 0
	}
	if value.ElapsedMS < 0 {
		value.ElapsedMS = 0
	}
	return value
}
func budgetAllows(l runcontrol.RunBudget, u, r runcontrol.BudgetDelta) bool {
	n := addBudget(u, r)
	return r.ModelAttempts >= 0 && r.ToolCalls >= 0 && r.Iterations >= 0 && r.Replans >= 0 && r.Supplements >= 0 && r.QualityRepairs >= 0 && r.InputTokens >= 0 && r.OutputTokens >= 0 && r.ElapsedMS >= 0 && n.ModelAttempts <= l.MaxModelAttempts && n.ToolCalls <= l.MaxToolCalls && n.Iterations <= l.MaxIterations && n.Replans <= l.MaxReplans && n.Supplements <= l.MaxSupplements && n.QualityRepairs <= l.MaxQualityRepairs && n.InputTokens <= l.MaxInputTokens && n.OutputTokens <= l.MaxOutputTokens && n.ElapsedMS <= l.MaxElapsedMS
}
func budgetOverage(l runcontrol.RunBudget, c runcontrol.BudgetDelta) runcontrol.BudgetDelta {
	max := func(v int64) int64 {
		if v > 0 {
			return v
		}
		return 0
	}
	return runcontrol.BudgetDelta{ModelAttempts: max(c.ModelAttempts - l.MaxModelAttempts), ToolCalls: max(c.ToolCalls - l.MaxToolCalls), Iterations: max(c.Iterations - l.MaxIterations), Replans: max(c.Replans - l.MaxReplans), Supplements: max(c.Supplements - l.MaxSupplements), QualityRepairs: max(c.QualityRepairs - l.MaxQualityRepairs), InputTokens: max(c.InputTokens - l.MaxInputTokens), OutputTokens: max(c.OutputTokens - l.MaxOutputTokens), ElapsedMS: max(c.ElapsedMS - l.MaxElapsedMS)}
}

package storage

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"strings"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/id"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/jackc/pgx/v5"
)

type rowQuerier interface {
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

func (s *PostgresStore) GetRunContext(ctx context.Context, lease runcontrol.LeaseContext) (runcontrol.AgentRunInput, error) {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.AgentRunInput{}, err
	}
	defer tx.Rollback(ctx)
	if err := assertLease(ctx, tx, lease, time.Now().UTC()); err != nil {
		return runcontrol.AgentRunInput{}, err
	}
	var run runcontrol.AgentRun
	if err := scanAgentRun(tx.QueryRow(ctx, `SELECT run_id, task_id, tenant_id, owner_id, status, queue_slot_acquired, attempt_count, COALESCE(lease_id,''), COALESCE(worker_id,''), fencing_token, COALESCE(lease_expires_at,'epoch'::timestamptz), created_at, updated_at FROM go_agent_runs WHERE run_id=$1`, lease.RunID), &run); err != nil {
		return runcontrol.AgentRunInput{}, mapNotFound(err)
	}
	var message string
	var taskVersion int
	if err := tx.QueryRow(ctx, `SELECT message, version FROM go_control_tasks WHERE task_id=$1`, run.TaskID).Scan(&message, &taskVersion); err != nil {
		return runcontrol.AgentRunInput{}, mapNotFound(err)
	}
	input := runcontrol.AgentRunInput{Run: run, TaskMessage: message, WorkflowVersion: "agent-runtime.v1", TaskVersion: taskVersion}
	var checkpoint []byte
	if err := tx.QueryRow(ctx, `SELECT checkpoint_blob FROM go_run_checkpoints WHERE run_id=$1 ORDER BY sequence DESC LIMIT 1`, lease.RunID).Scan(&checkpoint); err == nil {
		input.Checkpoint = append([]byte(nil), checkpoint...)
		if err := tx.QueryRow(ctx, `SELECT sequence FROM go_run_checkpoints WHERE run_id=$1 ORDER BY sequence DESC LIMIT 1`, lease.RunID).Scan(&input.CheckpointSequence); err != nil {
			return runcontrol.AgentRunInput{}, err
		}
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.AgentRunInput{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.AgentRunInput{}, err
	}
	return input, nil
}

func (s *PostgresStore) RecordModelAttempt(ctx context.Context, lease runcontrol.LeaseContext, attempt runcontrol.ModelAttempt) (string, error) {
	if err := validateAttempt(attempt); err != nil {
		return "", err
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return "", err
	}
	defer tx.Rollback(ctx)
	if err := assertLease(ctx, tx, lease, time.Now().UTC()); err != nil {
		return "", err
	}
	attemptID, err := id.New("attempt")
	if err != nil {
		return "", err
	}
	result, err := tx.Exec(ctx, `INSERT INTO go_model_attempts (attempt_id, run_id, attempt_key, operation, prompt_version, provider, request_hash, status, response_metadata_json, token_usage_json, error_category, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,COALESCE(NULLIF($9,''),'{}')::jsonb,COALESCE(NULLIF($10,''),'{}')::jsonb,NULLIF($11,''),$12) ON CONFLICT (run_id, attempt_key) DO NOTHING`, attemptID, lease.RunID, attempt.AttemptKey, attempt.Operation, attempt.PromptVersion, attempt.Provider, attempt.RequestHash, attempt.Status, attempt.ResponseMetadataJSON, attempt.TokenUsageJSON, attempt.ErrorCategory, time.Now().UTC())
	if err != nil {
		return "", err
	}
	if result.RowsAffected() == 0 {
		var existingID, existingHash string
		if err := tx.QueryRow(ctx, `SELECT attempt_id, request_hash FROM go_model_attempts WHERE run_id=$1 AND attempt_key=$2`, lease.RunID, attempt.AttemptKey).Scan(&existingID, &existingHash); err != nil {
			return "", err
		}
		if existingHash != attempt.RequestHash {
			return "", runcontrol.ErrInvalidIdempotency
		}
		if err := tx.Commit(ctx); err != nil {
			return "", err
		}
		return existingID, nil
	}
	if err := tx.Commit(ctx); err != nil {
		return "", err
	}
	return attemptID, nil
}

func (s *PostgresStore) AppendEvidence(ctx context.Context, lease runcontrol.LeaseContext, items []runcontrol.EvidenceItem) (int, error) {
	for _, item := range items {
		if item.SourceType == "" || item.SourceID == "" || item.Locator == "" || item.ExcerptHash == "" {
			return 0, runcontrol.ErrInvalidPayload
		}
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return 0, err
	}
	defer tx.Rollback(ctx)
	if err := assertLease(ctx, tx, lease, time.Now().UTC()); err != nil {
		return 0, err
	}
	accepted := 0
	for _, item := range items {
		evidenceID, err := id.New("evidence")
		if err != nil {
			return 0, err
		}
		result, err := tx.Exec(ctx, `INSERT INTO go_evidence (evidence_id, run_id, source_type, source_id, locator, excerpt_hash, excerpt, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (run_id, source_type, source_id, locator, excerpt_hash) DO NOTHING`, evidenceID, lease.RunID, item.SourceType, item.SourceID, item.Locator, item.ExcerptHash, item.Excerpt, time.Now().UTC())
		if err != nil {
			return 0, err
		}
		accepted += int(result.RowsAffected())
	}
	if err := tx.Commit(ctx); err != nil {
		return 0, err
	}
	return accepted, nil
}

func (s *PostgresStore) SaveCheckpoint(ctx context.Context, lease runcontrol.LeaseContext, sequence int64, checkpoint []byte) (runcontrol.CheckpointReceipt, error) {
	if sequence <= 0 {
		return runcontrol.CheckpointReceipt{}, runcontrol.ErrInvalidPayload
	}
	if len(checkpoint) > runcontrol.MaxCheckpointBytes {
		return runcontrol.CheckpointReceipt{}, runcontrol.ErrPayloadTooLarge
	}
	contentHash := hashBytes(checkpoint)
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.CheckpointReceipt{}, err
	}
	defer tx.Rollback(ctx)
	if err := assertLease(ctx, tx, lease, time.Now().UTC()); err != nil {
		return runcontrol.CheckpointReceipt{}, err
	}
	var currentSequence int64
	var currentHash string
	err = tx.QueryRow(ctx, `SELECT sequence, content_hash FROM go_run_checkpoints WHERE run_id=$1 ORDER BY sequence DESC LIMIT 1 FOR UPDATE`, lease.RunID).Scan(&currentSequence, &currentHash)
	if errors.Is(err, pgx.ErrNoRows) {
		currentSequence = 0
	} else if err != nil {
		return runcontrol.CheckpointReceipt{}, err
	}
	if sequence < currentSequence || (sequence == currentSequence && currentSequence > 0 && currentHash != contentHash) {
		return runcontrol.CheckpointReceipt{}, runcontrol.ErrCheckpointConflict
	}
	if sequence == currentSequence {
		if err := tx.Commit(ctx); err != nil {
			return runcontrol.CheckpointReceipt{}, err
		}
		return runcontrol.CheckpointReceipt{Sequence: sequence, ContentHash: contentHash}, nil
	}
	if _, err := tx.Exec(ctx, `INSERT INTO go_run_checkpoints (run_id, sequence, checkpoint_blob, content_hash, created_at) VALUES ($1,$2,$3,$4,$5)`, lease.RunID, sequence, checkpoint, contentHash, time.Now().UTC()); err != nil {
		return runcontrol.CheckpointReceipt{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.CheckpointReceipt{}, err
	}
	return runcontrol.CheckpointReceipt{Sequence: sequence, ContentHash: contentHash}, nil
}

func (s *PostgresStore) SubmitDraft(ctx context.Context, lease runcontrol.LeaseContext, draftKey string, expectedTaskVersion int, patch []byte) (runcontrol.DraftReceipt, error) {
	if draftKey == "" || expectedTaskVersion < 1 {
		return runcontrol.DraftReceipt{}, runcontrol.ErrInvalidPayload
	}
	if len(patch) > runcontrol.MaxDraftPatchBytes {
		return runcontrol.DraftReceipt{}, runcontrol.ErrPayloadTooLarge
	}
	patchHash := hashBytes(patch)
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.DraftReceipt{}, err
	}
	defer tx.Rollback(ctx)
	if err := assertLease(ctx, tx, lease, time.Now().UTC()); err != nil {
		return runcontrol.DraftReceipt{}, err
	}
	var taskID string
	if err := tx.QueryRow(ctx, `SELECT task_id FROM go_agent_runs WHERE run_id=$1`, lease.RunID).Scan(&taskID); err != nil {
		return runcontrol.DraftReceipt{}, mapNotFound(err)
	}
	var existingHash string
	var existingVersion int
	err = tx.QueryRow(ctx, `SELECT patch_hash, task_version FROM go_working_draft_versions WHERE task_id=$1 AND draft_key=$2`, taskID, draftKey).Scan(&existingHash, &existingVersion)
	if err == nil {
		if existingHash != patchHash {
			return runcontrol.DraftReceipt{}, runcontrol.ErrInvalidIdempotency
		}
		if err := tx.Commit(ctx); err != nil {
			return runcontrol.DraftReceipt{}, err
		}
		return runcontrol.DraftReceipt{TaskVersion: existingVersion}, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.DraftReceipt{}, err
	}
	var newVersion int
	if err := tx.QueryRow(ctx, `UPDATE go_control_tasks SET version=version+1, updated_at=$3 WHERE task_id=$1 AND version=$2 RETURNING version`, taskID, expectedTaskVersion, time.Now().UTC()).Scan(&newVersion); errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.DraftReceipt{}, runcontrol.ErrTaskVersionConflict
	} else if err != nil {
		return runcontrol.DraftReceipt{}, err
	}
	draftID, err := id.New("draft")
	if err != nil {
		return runcontrol.DraftReceipt{}, err
	}
	if _, err := tx.Exec(ctx, `INSERT INTO go_working_draft_versions (draft_id, task_id, run_id, draft_key, patch, patch_hash, task_version, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`, draftID, taskID, lease.RunID, draftKey, patch, patchHash, newVersion, time.Now().UTC()); err != nil {
		return runcontrol.DraftReceipt{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.DraftReceipt{}, err
	}
	return runcontrol.DraftReceipt{TaskVersion: newVersion}, nil
}

func assertLease(ctx context.Context, query rowQuerier, lease runcontrol.LeaseContext, now time.Time) error {
	var valid bool
	if err := query.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM go_agent_runs WHERE run_id=$1 AND status='RUNNING' AND lease_id=$2 AND worker_id=$3 AND fencing_token=$4 AND lease_expires_at>$5)`, lease.RunID, lease.LeaseID, lease.WorkerID, lease.FencingToken, now).Scan(&valid); err != nil {
		return err
	}
	if !valid {
		return runcontrol.ErrLeaseLost
	}
	return nil
}

func scanAgentRun(row pgx.Row, run *runcontrol.AgentRun) error {
	return row.Scan(&run.RunID, &run.TaskID, &run.TenantID, &run.OwnerID, &run.Status, &run.QueueSlotAcquired, &run.AttemptCount, &run.LeaseID, &run.WorkerID, &run.FencingToken, &run.LeaseExpiresAt, &run.CreatedAt, &run.UpdatedAt)
}

func mapNotFound(err error) error {
	if errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.ErrNotFound
	}
	return err
}

func validateAttempt(attempt runcontrol.ModelAttempt) error {
	if attempt.AttemptKey == "" || attempt.Operation == "" || attempt.RequestHash == "" {
		return runcontrol.ErrInvalidPayload
	}
	for _, raw := range []string{attempt.ResponseMetadataJSON, attempt.TokenUsageJSON} {
		if raw != "" && !json.Valid([]byte(raw)) {
			return runcontrol.ErrInvalidPayload
		}
	}
	metadata := strings.ToLower(attempt.ResponseMetadataJSON + "\x00" + attempt.TokenUsageJSON)
	for _, marker := range []string{"authorization", "api_key", "access_token", "jwt"} {
		if strings.Contains(metadata, marker) {
			return runcontrol.ErrSensitivePayload
		}
	}
	return nil
}

func hashBytes(value []byte) string {
	digest := sha256.Sum256(value)
	return hex.EncodeToString(digest[:])
}

var _ runcontrol.AgentExecutionStore = (*PostgresStore)(nil)

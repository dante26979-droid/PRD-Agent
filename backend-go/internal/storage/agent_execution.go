package storage

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/id"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/jackc/pgx/v5"
)

type rowQuerier interface {
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

func (s *PostgresStore) GetRun(ctx context.Context, runID string) (runcontrol.AgentRun, error) {
	var run runcontrol.AgentRun
	err := scanAgentRun(s.pool.QueryRow(ctx, `SELECT run_id, task_id, tenant_id, owner_id, workflow_version, COALESCE(execution_ledger_version,''), status, queue_slot_acquired, attempt_count, COALESCE(lease_id,''), COALESCE(worker_id,''), fencing_token, COALESCE(lease_expires_at,'epoch'::timestamptz), created_at, updated_at FROM go_agent_runs WHERE run_id=$1`, runID), &run)
	return run, mapNotFound(err)
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
	if err := scanAgentRun(tx.QueryRow(ctx, `SELECT run_id, task_id, tenant_id, owner_id, workflow_version, COALESCE(execution_ledger_version,''), status, queue_slot_acquired, attempt_count, COALESCE(lease_id,''), COALESCE(worker_id,''), fencing_token, COALESCE(lease_expires_at,'epoch'::timestamptz), created_at, updated_at FROM go_agent_runs WHERE run_id=$1`, lease.RunID), &run); err != nil {
		return runcontrol.AgentRunInput{}, mapNotFound(err)
	}
	var message string
	var taskVersion int
	var repositoryBindingID, repositoryRevision string
	if err := tx.QueryRow(ctx, `SELECT message, version, COALESCE(repository_binding_id,''), COALESCE(repository_revision,'') FROM go_control_tasks WHERE task_id=$1`, run.TaskID).Scan(&message, &taskVersion, &repositoryBindingID, &repositoryRevision); err != nil {
		return runcontrol.AgentRunInput{}, mapNotFound(err)
	}
	input := runcontrol.AgentRunInput{
		Run: run, TaskMessage: message, WorkflowVersion: string(run.WorkflowVersion), TaskVersion: taskVersion,
		RepositoryBindingID: repositoryBindingID, RepositoryRevision: repositoryRevision,
	}
	var assignment runcontrol.RolloutAssignment
	if err := tx.QueryRow(ctx, `SELECT authoritative_workflow_version,evaluation_mode,COALESCE(shadow_workflow_version,''),policy_version,assignment_hash FROM go_agent_rollout_assignments WHERE run_id=$1`, lease.RunID).Scan(
		&assignment.AuthoritativeWorkflowVersion, &assignment.EvaluationMode,
		&assignment.ShadowWorkflowVersion, &assignment.PolicyVersion, &assignment.AssignmentHash,
	); err == nil {
		input.EvaluationMode = assignment.EvaluationMode
		input.AuthoritativeWorkflow = assignment.AuthoritativeWorkflowVersion
		input.ShadowWorkflow = assignment.ShadowWorkflowVersion
		input.CandidatePolicyVersion = assignment.PolicyVersion
		input.AssignmentHash = assignment.AssignmentHash
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.AgentRunInput{}, err
	}
	var memoryAssignment runcontrol.RunMemoryAssignment
	if err := tx.QueryRow(ctx, `SELECT space_id,memory_watermark,policy_version,access_scope_hash,assignment_hash,created_at FROM go_run_memory_assignments WHERE run_id=$1`, lease.RunID).Scan(
		&memoryAssignment.SpaceID, &memoryAssignment.MemoryWatermark, &memoryAssignment.PolicyVersion,
		&memoryAssignment.AccessScopeHash, &memoryAssignment.AssignmentHash, &memoryAssignment.CreatedAt,
	); err == nil {
		input.MemorySpaceID = memoryAssignment.SpaceID
		input.MemoryWatermark = memoryAssignment.MemoryWatermark
		input.MemoryPolicyVersion = memoryAssignment.PolicyVersion
		input.MemoryAccessScopeHash = memoryAssignment.AccessScopeHash
		input.MemoryAssignmentHash = memoryAssignment.AssignmentHash
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.AgentRunInput{}, err
	}
	var checkpoint []byte
	if err := tx.QueryRow(ctx, `SELECT checkpoint_blob,sequence,content_hash FROM go_run_checkpoints WHERE run_id=$1 ORDER BY sequence DESC LIMIT 1`, lease.RunID).Scan(&checkpoint, &input.CheckpointSequence, &input.ResumeSummary.CheckpointContentHash); err == nil {
		input.Checkpoint = append([]byte(nil), checkpoint...)
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.AgentRunInput{}, err
	}
	evidenceRows, err := tx.Query(ctx, `SELECT source_type, source_id, locator, excerpt_hash, excerpt FROM go_evidence WHERE run_id=$1 ORDER BY created_at, evidence_id`, lease.RunID)
	if err != nil {
		return runcontrol.AgentRunInput{}, err
	}
	for evidenceRows.Next() {
		var item runcontrol.EvidenceItem
		if err := evidenceRows.Scan(&item.SourceType, &item.SourceID, &item.Locator, &item.ExcerptHash, &item.Excerpt); err != nil {
			evidenceRows.Close()
			return runcontrol.AgentRunInput{}, err
		}
		input.ResumeEvidence = append(input.ResumeEvidence, item)
		input.ResumeSummary.EvidenceRefs = append(input.ResumeSummary.EvidenceRefs, runcontrol.EvidenceReference(item))
	}
	if err := evidenceRows.Err(); err != nil {
		evidenceRows.Close()
		return runcontrol.AgentRunInput{}, err
	}
	evidenceRows.Close()
	input.ResumeSummary.EvidenceCount = int64(len(input.ResumeEvidence))
	artifactRows, err := tx.Query(ctx, `SELECT artifact_key, artifact_type, generation, request_hash, content_hash, content FROM go_run_artifacts WHERE run_id=$1 AND expires_at>$2 ORDER BY artifact_key`, lease.RunID, time.Now().UTC())
	if err != nil {
		return runcontrol.AgentRunInput{}, err
	}
	for artifactRows.Next() {
		var item runcontrol.RunArtifact
		if err := artifactRows.Scan(&item.ArtifactKey, &item.ArtifactType, &item.Generation, &item.RequestHash, &item.ContentHash, &item.Content); err != nil {
			artifactRows.Close()
			return runcontrol.AgentRunInput{}, err
		}
		input.ResumeArtifacts = append(input.ResumeArtifacts, item)
		input.ResumeSummary.Artifacts = append(input.ResumeSummary.Artifacts, runcontrol.RunArtifactIdentity{ArtifactKey: item.ArtifactKey, ArtifactType: item.ArtifactType, Generation: item.Generation, RequestHash: item.RequestHash, ContentHash: item.ContentHash})
	}
	if err := artifactRows.Err(); err != nil {
		artifactRows.Close()
		return runcontrol.AgentRunInput{}, err
	}
	artifactRows.Close()
	input.ResumeSummary.ArtifactCount = int64(len(input.ResumeArtifacts))
	if err := tx.QueryRow(ctx, `SELECT COUNT(*) FROM go_model_attempts WHERE run_id=$1 AND status<>'PLANNED'`, lease.RunID).Scan(&input.ResumeSummary.TerminalModelAttemptCount); err != nil {
		return runcontrol.AgentRunInput{}, err
	}
	var resume runcontrol.SubmittedDraftReceipt
	if err := tx.QueryRow(ctx, `SELECT draft_key, patch_hash, task_version, patch FROM go_working_draft_versions WHERE run_id=$1 ORDER BY task_version DESC LIMIT 1`, lease.RunID).Scan(&resume.DraftKey, &resume.ContentHash, &resume.TaskVersion, &resume.Content); err == nil {
		input.ResumeDraft = &resume
		copy := resume
		input.SubmittedDraft = &copy
		if run.WorkflowVersion == runcontrol.WorkflowVersionV4 {
			input.TaskVersion = resume.TaskVersion - 1
		}
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.AgentRunInput{}, err
	}
	err = tx.QueryRow(ctx, `SELECT base_draft_id,base_draft_hash,reopened_unit_keys,immutable_unit_keys,user_feedback FROM go_run_revision_scopes WHERE run_id=$1`, lease.RunID).Scan(
		&input.RevisionScope.BaseDraftID, &input.RevisionScope.BaseDraftHash,
		&input.RevisionScope.ReopenedUnitKeys, &input.RevisionScope.ImmutableUnitKeys,
		&input.RevisionScope.UserFeedback,
	)
	if err == nil {
		if err := tx.QueryRow(ctx, `SELECT draft_key,patch_hash,task_version,patch FROM go_working_draft_versions WHERE draft_id=$1`, input.RevisionScope.BaseDraftID).Scan(
			&resume.DraftKey, &resume.ContentHash, &resume.TaskVersion, &resume.Content,
		); err != nil {
			return runcontrol.AgentRunInput{}, err
		}
		input.ResumeDraft = &resume
		copy := resume
		input.BaseDraft = &copy
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.AgentRunInput{}, err
	}
	if run.ExecutionLedgerVersion != "" {
		if err := loadLedgerContext(ctx, tx, &input); err != nil {
			return runcontrol.AgentRunInput{}, err
		}
	}
	if unitScope, err := loadRunUnitScope(ctx, tx, lease.RunID); err == nil {
		input.RunPurpose = unitScope.Purpose
		input.UnitScope = unitScope
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.AgentRunInput{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.AgentRunInput{}, err
	}
	return input, nil
}

func (s *PostgresStore) SaveRunArtifact(ctx context.Context, lease runcontrol.LeaseContext, artifact runcontrol.RunArtifact) (runcontrol.RunArtifactReceipt, error) {
	if artifact.ArtifactKey == "" || artifact.ArtifactType == "" || artifact.Generation < 0 || artifact.RequestHash == "" || len(artifact.Content) == 0 {
		return runcontrol.RunArtifactReceipt{}, runcontrol.ErrInvalidPayload
	}
	if len(artifact.Content) > runcontrol.MaxRunArtifactBytes {
		return runcontrol.RunArtifactReceipt{}, runcontrol.ErrPayloadTooLarge
	}
	contentHash := hashBytes(artifact.Content)
	if artifact.ContentHash != "" && artifact.ContentHash != contentHash {
		return runcontrol.RunArtifactReceipt{}, runcontrol.ErrInvalidPayload
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.RunArtifactReceipt{}, err
	}
	defer tx.Rollback(ctx)
	if err := assertLease(ctx, tx, lease, time.Now().UTC()); err != nil {
		return runcontrol.RunArtifactReceipt{}, err
	}
	if artifact.ArtifactType == "SHADOW_EVALUATION" {
		var assignment runcontrol.RolloutAssignment
		if err := tx.QueryRow(ctx, `SELECT authoritative_workflow_version,evaluation_mode,COALESCE(shadow_workflow_version,''),cohort,policy_version,assignment_reason,assignment_hash FROM go_agent_rollout_assignments WHERE run_id=$1`, lease.RunID).Scan(
			&assignment.AuthoritativeWorkflowVersion, &assignment.EvaluationMode, &assignment.ShadowWorkflowVersion,
			&assignment.Cohort, &assignment.PolicyVersion, &assignment.AssignmentReason, &assignment.AssignmentHash,
		); err != nil {
			return runcontrol.RunArtifactReceipt{}, mapNotFound(err)
		}
		trace, err := runcontrol.ParseShadowEvaluationArtifact(lease.RunID, artifact, assignment)
		if err != nil {
			return runcontrol.RunArtifactReceipt{}, err
		}
		persisted := runcontrol.PersistedAuthoritativeResult{RunID: lease.RunID}
		if err := tx.QueryRow(ctx, `SELECT task_id FROM go_agent_runs WHERE run_id=$1`, lease.RunID).Scan(&persisted.TaskID); err != nil {
			return runcontrol.RunArtifactReceipt{}, mapNotFound(err)
		}
		if trace.OutputKey != "" {
			if err := tx.QueryRow(ctx, `SELECT output_key,output_kind,content_hash FROM go_run_outputs WHERE run_id=$1 AND output_key=$2`, lease.RunID, trace.OutputKey).Scan(
				&persisted.OutputKey, &persisted.OutputKind, &persisted.OutputContentHash,
			); err != nil {
				if errors.Is(err, pgx.ErrNoRows) {
					return runcontrol.RunArtifactReceipt{}, fmt.Errorf("%w: shadow output was not persisted", runcontrol.ErrInvalidPayload)
				}
				return runcontrol.RunArtifactReceipt{}, err
			}
		} else {
			if err := tx.QueryRow(ctx, `SELECT draft_key,patch_hash FROM go_working_draft_versions WHERE run_id=$1 AND draft_key=$2`, lease.RunID, trace.DraftKey).Scan(
				&persisted.DraftKey, &persisted.DraftContentHash,
			); err != nil {
				if errors.Is(err, pgx.ErrNoRows) {
					return runcontrol.RunArtifactReceipt{}, fmt.Errorf("%w: shadow draft was not persisted", runcontrol.ErrInvalidPayload)
				}
				return runcontrol.RunArtifactReceipt{}, err
			}
		}
		if err := runcontrol.ValidatePersistedAuthoritativeTrace(trace, persisted); err != nil {
			return runcontrol.RunArtifactReceipt{}, err
		}
	}
	var existingRequestHash, existingContentHash string
	err = tx.QueryRow(ctx, `SELECT request_hash, content_hash FROM go_run_artifacts WHERE run_id=$1 AND artifact_key=$2 FOR UPDATE`, lease.RunID, artifact.ArtifactKey).Scan(&existingRequestHash, &existingContentHash)
	if err == nil {
		if existingRequestHash != artifact.RequestHash || existingContentHash != contentHash {
			return runcontrol.RunArtifactReceipt{}, runcontrol.ErrInvalidIdempotency
		}
		if err := tx.Commit(ctx); err != nil {
			return runcontrol.RunArtifactReceipt{}, err
		}
		return runcontrol.RunArtifactReceipt{ArtifactKey: artifact.ArtifactKey, ContentHash: contentHash}, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.RunArtifactReceipt{}, err
	}
	now := time.Now().UTC()
	if _, err := tx.Exec(ctx, `INSERT INTO go_run_artifacts (run_id, artifact_key, artifact_type, generation, request_hash, content, content_hash, created_at, expires_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)`, lease.RunID, artifact.ArtifactKey, artifact.ArtifactType, artifact.Generation, artifact.RequestHash, artifact.Content, contentHash, now, now.Add(72*time.Hour)); err != nil {
		return runcontrol.RunArtifactReceipt{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.RunArtifactReceipt{}, err
	}
	return runcontrol.RunArtifactReceipt{ArtifactKey: artifact.ArtifactKey, ContentHash: contentHash}, nil
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
		var existingID, existingHash, existingStatus string
		if err := tx.QueryRow(ctx, `SELECT attempt_id, request_hash, status FROM go_model_attempts WHERE run_id=$1 AND attempt_key=$2 FOR UPDATE`, lease.RunID, attempt.AttemptKey).Scan(&existingID, &existingHash, &existingStatus); err != nil {
			return "", err
		}
		if existingHash != attempt.RequestHash {
			return "", runcontrol.ErrInvalidIdempotency
		}
		if existingStatus != "PLANNED" && attempt.Status != existingStatus {
			if attempt.Status == "PLANNED" {
				if err := tx.Commit(ctx); err != nil {
					return "", err
				}
				return existingID, nil
			}
			return "", runcontrol.ErrInvalidIdempotency
		}
		if _, err := tx.Exec(ctx, `UPDATE go_model_attempts SET status=$2, response_metadata_json=COALESCE(NULLIF($3,''),'{}')::jsonb, token_usage_json=COALESCE(NULLIF($4,''),'{}')::jsonb, error_category=NULLIF($5,'') WHERE attempt_id=$1`, existingID, attempt.Status, attempt.ResponseMetadataJSON, attempt.TokenUsageJSON, attempt.ErrorCategory); err != nil {
			return "", err
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

func (s *PostgresStore) HasModelAttempts(ctx context.Context, runID string) (bool, error) {
	var exists bool
	if err := s.pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM go_model_attempts WHERE run_id=$1)`, runID).Scan(&exists); err != nil {
		return false, err
	}
	return exists, nil
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
	var taskID, tenantID, ownerID string
	if err := tx.QueryRow(ctx, `SELECT task_id,tenant_id,owner_id FROM go_agent_runs WHERE run_id=$1`, lease.RunID).Scan(&taskID, &tenantID, &ownerID); err != nil {
		return runcontrol.DraftReceipt{}, mapNotFound(err)
	}
	candidates, _, err := runcontrol.ParseConfirmationCandidates(patch, taskID, lease.RunID)
	if err != nil {
		return runcontrol.DraftReceipt{}, err
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
	now := time.Now().UTC()
	if _, err := tx.Exec(ctx, `INSERT INTO go_working_draft_versions (draft_id, task_id, run_id, draft_key, patch, patch_hash, task_version, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`, draftID, taskID, lease.RunID, draftKey, patch, patchHash, newVersion, now); err != nil {
		return runcontrol.DraftReceipt{}, err
	}
	var baseDraftID string
	var immutableKeys []string
	err = tx.QueryRow(ctx, `SELECT base_draft_id,immutable_unit_keys FROM go_run_revision_scopes WHERE run_id=$1`, lease.RunID).Scan(&baseDraftID, &immutableKeys)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.DraftReceipt{}, err
	}
	for _, candidate := range candidates {
		var unitID string
		err := tx.QueryRow(ctx, `SELECT unit_id FROM go_confirmation_units WHERE task_id=$1 AND unit_key=$2`, taskID, candidate.UnitKey).Scan(&unitID)
		if errors.Is(err, pgx.ErrNoRows) {
			unitID, err = id.New("unit")
			if err != nil {
				return runcontrol.DraftReceipt{}, err
			}
			if _, err := tx.Exec(ctx, `INSERT INTO go_confirmation_units (unit_id,task_id,unit_key,created_at) VALUES ($1,$2,$3,$4)`, unitID, taskID, candidate.UnitKey, now); err != nil {
				return runcontrol.DraftReceipt{}, err
			}
		} else if err != nil {
			return runcontrol.DraftReceipt{}, err
		}
		versionID, err := id.New("unit-version")
		if err != nil {
			return runcontrol.DraftReceipt{}, err
		}
		payload, err := json.Marshal(candidate)
		if err != nil {
			return runcontrol.DraftReceipt{}, err
		}
		if _, err := tx.Exec(ctx, `INSERT INTO go_confirmation_unit_versions (unit_version_id,unit_id,draft_id,content_hash,title,ordinal,payload,created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`, versionID, unitID, draftID, candidate.ContentHash, candidate.Title, candidate.Ordinal, payload, now); err != nil {
			return runcontrol.DraftReceipt{}, err
		}
		if containsString(immutableKeys, candidate.UnitKey) {
			var confirmed bool
			if err := tx.QueryRow(ctx, `
				SELECT EXISTS(
					SELECT 1 FROM go_confirmation_unit_versions prior
					JOIN go_confirmation_units u ON u.unit_id=prior.unit_id
					WHERE prior.draft_id=$1 AND u.unit_key=$2 AND prior.content_hash=$3
					  AND COALESCE((SELECT decision FROM go_confirmation_decisions d WHERE d.unit_version_id=prior.unit_version_id ORDER BY d.created_at DESC,d.decision_id DESC LIMIT 1),'PENDING')='CONFIRMED'
				)`, baseDraftID, candidate.UnitKey, candidate.ContentHash).Scan(&confirmed); err != nil {
				return runcontrol.DraftReceipt{}, err
			}
			if !confirmed {
				return runcontrol.DraftReceipt{}, runcontrol.ErrInvalidPayload
			}
			decisionID, err := id.New("decision")
			if err != nil {
				return runcontrol.DraftReceipt{}, err
			}
			key := "system-carry-" + lease.RunID + "-" + candidate.UnitKey
			requestHash := hashBytes([]byte(key + "\x00" + candidate.ContentHash))
			if _, err := tx.Exec(ctx, `INSERT INTO go_confirmation_decisions (decision_id,unit_version_id,tenant_id,owner_id,decision,feedback,idempotency_key,request_hash,expected_task_version,resulting_task_version,created_at) VALUES ($1,$2,$3,$4,'CONFIRMED','',$5,$6,$7,$8,$9)`, decisionID, versionID, tenantID, ownerID, key, requestHash, expectedTaskVersion, newVersion, now); err != nil {
				return runcontrol.DraftReceipt{}, err
			}
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.DraftReceipt{}, err
	}
	return runcontrol.DraftReceipt{TaskVersion: newVersion}, nil
}

func containsString(items []string, value string) bool {
	for _, item := range items {
		if item == value {
			return true
		}
	}
	return false
}

func (s *PostgresStore) GetLatestDraft(ctx context.Context, tenantID, ownerID, taskID string) (runcontrol.WorkingDraft, error) {
	var draft runcontrol.WorkingDraft
	err := s.pool.QueryRow(ctx, `
		SELECT d.draft_id, d.task_id, d.run_id, d.draft_key, d.patch, d.patch_hash,
		       d.task_version, d.created_at
		  FROM go_working_draft_versions d
		  JOIN go_control_tasks t ON t.task_id=d.task_id
		 WHERE d.task_id=$1 AND t.tenant_id=$2 AND t.owner_id=$3
		 ORDER BY d.task_version DESC, d.created_at DESC
		 LIMIT 1`,
		taskID, tenantID, ownerID,
	).Scan(
		&draft.DraftID, &draft.TaskID, &draft.RunID, &draft.DraftKey, &draft.Content,
		&draft.ContentHash, &draft.TaskVersion, &draft.CreatedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		var owned bool
		if checkErr := s.pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM go_control_tasks WHERE task_id=$1 AND tenant_id=$2 AND owner_id=$3)`, taskID, tenantID, ownerID).Scan(&owned); checkErr != nil {
			return runcontrol.WorkingDraft{}, checkErr
		}
		return runcontrol.WorkingDraft{}, runcontrol.ErrNotFound
	}
	return draft, err
}

func (s *PostgresStore) ListEvidence(ctx context.Context, tenantID, ownerID, taskID string, limit int) ([]runcontrol.EvidenceRecord, error) {
	if limit < 1 {
		limit = 100
	}
	rows, err := s.pool.Query(ctx, `
		SELECT e.evidence_id, e.run_id, e.source_type, e.source_id, e.locator,
		       e.excerpt_hash, e.excerpt, e.created_at
		  FROM go_evidence e
		  JOIN go_agent_runs r ON r.run_id=e.run_id
		  JOIN go_control_tasks t ON t.task_id=r.task_id
		 WHERE t.task_id=$1 AND t.tenant_id=$2 AND t.owner_id=$3
		 ORDER BY e.created_at, e.evidence_id
		 LIMIT $4`, taskID, tenantID, ownerID, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]runcontrol.EvidenceRecord, 0)
	for rows.Next() {
		var item runcontrol.EvidenceRecord
		if err := rows.Scan(
			&item.EvidenceID, &item.RunID, &item.SourceType, &item.SourceID,
			&item.Locator, &item.ExcerptHash, &item.Excerpt, &item.CreatedAt,
		); err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if len(items) == 0 {
		if _, err := s.GetTask(ctx, tenantID, ownerID, taskID); err != nil {
			return nil, err
		}
	}
	return items, nil
}

func (s *PostgresStore) ListModelAttempts(ctx context.Context, tenantID, ownerID, taskID string, limit int) ([]runcontrol.ModelAttemptRecord, error) {
	if limit < 1 {
		limit = 100
	}
	rows, err := s.pool.Query(ctx, `
		SELECT a.attempt_id, a.run_id, a.attempt_key, a.operation, COALESCE(a.prompt_version,''),
		       COALESCE(a.provider,''), a.request_hash, a.status, COALESCE(a.error_category,''),
		       a.created_at
		  FROM go_model_attempts a
		  JOIN go_agent_runs r ON r.run_id=a.run_id
		  JOIN go_control_tasks t ON t.task_id=r.task_id
		 WHERE t.task_id=$1 AND t.tenant_id=$2 AND t.owner_id=$3
		 ORDER BY a.created_at, a.attempt_id
		 LIMIT $4`, taskID, tenantID, ownerID, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]runcontrol.ModelAttemptRecord, 0)
	for rows.Next() {
		var item runcontrol.ModelAttemptRecord
		if err := rows.Scan(
			&item.AttemptID, &item.RunID, &item.AttemptKey, &item.Operation,
			&item.PromptVersion, &item.Provider, &item.RequestHash, &item.Status,
			&item.ErrorCategory, &item.CreatedAt,
		); err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if len(items) == 0 {
		if _, err := s.GetTask(ctx, tenantID, ownerID, taskID); err != nil {
			return nil, err
		}
	}
	return items, nil
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
	return row.Scan(&run.RunID, &run.TaskID, &run.TenantID, &run.OwnerID, &run.WorkflowVersion, &run.ExecutionLedgerVersion, &run.Status, &run.QueueSlotAcquired, &run.AttemptCount, &run.LeaseID, &run.WorkerID, &run.FencingToken, &run.LeaseExpiresAt, &run.CreatedAt, &run.UpdatedAt)
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

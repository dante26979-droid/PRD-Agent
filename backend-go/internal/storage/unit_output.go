package storage

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/jackc/pgx/v5"
)

func (s *PostgresStore) SetRunUnitScope(ctx context.Context, tenantID, ownerID, runID string, expectedTaskVersion int, scope runcontrol.UnitScope) error {
	built, err := runcontrol.BuildUnitScope(scope)
	if err != nil {
		return err
	}
	payload, err := json.Marshal(built)
	if err != nil {
		return err
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	var taskVersion int
	var workflow runcontrol.WorkflowVersion
	if err := tx.QueryRow(ctx, `SELECT t.version,r.workflow_version FROM go_agent_runs r JOIN go_control_tasks t ON t.task_id=r.task_id WHERE r.run_id=$1 AND r.tenant_id=$2 AND r.owner_id=$3 FOR UPDATE`, runID, tenantID, ownerID).Scan(&taskVersion, &workflow); err != nil {
		return mapNotFound(err)
	}
	if taskVersion != expectedTaskVersion {
		return runcontrol.ErrTaskVersionConflict
	}
	if workflow != runcontrol.WorkflowVersionV4 {
		return runcontrol.ErrInvalidPayload
	}
	var existingHash string
	err = tx.QueryRow(ctx, `SELECT scope_hash FROM go_run_unit_scopes WHERE run_id=$1`, runID).Scan(&existingHash)
	if err == nil {
		if existingHash != built.ScopeHash {
			return runcontrol.ErrInvalidIdempotency
		}
		return tx.Commit(ctx)
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return err
	}
	if _, err := tx.Exec(ctx, `INSERT INTO go_run_unit_scopes (run_id,run_purpose,scope_payload,scope_hash,expected_task_version,created_at) VALUES ($1,$2,$3,$4,$5,$6)`, runID, built.Purpose, payload, built.ScopeHash, expectedTaskVersion, time.Now().UTC()); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *PostgresStore) SubmitRunOutput(ctx context.Context, lease runcontrol.LeaseContext, output runcontrol.RunOutput) (runcontrol.RunOutputReceipt, error) {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.RunOutputReceipt{}, err
	}
	defer tx.Rollback(ctx)
	if err := assertLease(ctx, tx, lease, time.Now().UTC()); err != nil {
		return runcontrol.RunOutputReceipt{}, err
	}
	var scopePayload []byte
	var taskVersion int
	var taskID, tenantID, ownerID, taskMessage, taskStatus string
	if err := tx.QueryRow(ctx, `SELECT s.scope_payload,t.version,t.task_id,t.tenant_id,t.owner_id,t.message,t.status FROM go_run_unit_scopes s JOIN go_agent_runs r ON r.run_id=s.run_id JOIN go_control_tasks t ON t.task_id=r.task_id WHERE s.run_id=$1 FOR UPDATE`, lease.RunID).Scan(&scopePayload, &taskVersion, &taskID, &tenantID, &ownerID, &taskMessage, &taskStatus); err != nil {
		return runcontrol.RunOutputReceipt{}, mapNotFound(err)
	}
	var scope runcontrol.UnitScope
	if err := json.Unmarshal(scopePayload, &scope); err != nil {
		return runcontrol.RunOutputReceipt{}, err
	}
	var existingHash string
	var existingTaskVersion int
	normalizedOutputHash := strings.TrimPrefix(output.ContentHash, "sha256:")
	err = tx.QueryRow(ctx, `SELECT content_hash,COALESCE(materialized_task_version,expected_task_version) FROM go_run_outputs WHERE run_id=$1 AND output_key=$2`, lease.RunID, output.OutputKey).Scan(&existingHash, &existingTaskVersion)
	if err == nil {
		if existingHash != normalizedOutputHash {
			return runcontrol.RunOutputReceipt{}, runcontrol.ErrInvalidIdempotency
		}
		if err := tx.Commit(ctx); err != nil {
			return runcontrol.RunOutputReceipt{}, err
		}
		return runcontrol.RunOutputReceipt{OutputKey: output.OutputKey, ContentHash: existingHash, TaskVersion: existingTaskVersion}, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.RunOutputReceipt{}, err
	}
	if err := runcontrol.ValidateRunOutput(scope, output, taskVersion); err != nil {
		return runcontrol.RunOutputReceipt{}, err
	}
	contentHash := normalizedOutputHash
	if _, err := tx.Exec(ctx, `INSERT INTO go_run_outputs (run_id,output_key,output_kind,run_purpose,scope_hash,expected_task_version,content_hash,payload,created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)`, lease.RunID, output.OutputKey, output.OutputKind, output.RunPurpose, output.ScopeHash, output.ExpectedTaskVersion, contentHash, output.Payload, time.Now().UTC()); err != nil {
		return runcontrol.RunOutputReceipt{}, err
	}
	if output.RunPurpose == runcontrol.RunPurposePlanOutline {
		candidate, err := runcontrol.ParseOutlineCandidate(output.Payload)
		if err != nil {
			return runcontrol.RunOutputReceipt{}, err
		}
		now := time.Now().UTC()
		outlineID := "outline-" + confirmationRequestHash(taskID)
		outlineVersionID := "outline-version-" + confirmationRequestHash(lease.RunID, output.OutputKey)
		if _, err := tx.Exec(ctx, `INSERT INTO go_prd_outlines (outline_id,task_id,created_at) VALUES ($1,$2,$3) ON CONFLICT (task_id) DO NOTHING`, outlineID, taskID, now); err != nil {
			return runcontrol.RunOutputReceipt{}, err
		}
		if _, err := tx.Exec(ctx, `INSERT INTO go_prd_outline_versions (outline_version_id,outline_id,version,status,payload,content_hash,source_run_id,created_at) VALUES ($1,$2,1,'DRAFT',$3,$4,$5,$6)`, outlineVersionID, outlineID, output.Payload, candidate.ContentHash, lease.RunID, now); err != nil {
			return runcontrol.RunOutputReceipt{}, err
		}
		taskVersion++
		if _, err := tx.Exec(ctx, `UPDATE go_control_tasks SET version=$2,status=$3,updated_at=$4 WHERE task_id=$1`, taskID, taskVersion, runcontrol.ReviewOutlineReview, now); err != nil {
			return runcontrol.RunOutputReceipt{}, err
		}
		if _, err := tx.Exec(ctx, `UPDATE go_run_outputs SET materialized_kind='OUTLINE_VERSION',materialized_id=$3,materialized_at=$4,materialized_task_version=$5 WHERE run_id=$1 AND output_key=$2`, lease.RunID, output.OutputKey, outlineVersionID, now, taskVersion); err != nil {
			return runcontrol.RunOutputReceipt{}, err
		}
		task := runcontrol.Task{TaskID: taskID, TenantID: tenantID, OwnerID: ownerID, Message: taskMessage, Status: taskStatus, Version: taskVersion}
		if _, err := appendReviewEvent(ctx, tx, task, "review.outline_materialized", map[string]any{"outline_version_id": outlineVersionID, "task_version": taskVersion}, now); err != nil {
			return runcontrol.RunOutputReceipt{}, err
		}
	} else if output.RunPurpose == runcontrol.RunPurposeGenerateUnit || output.RunPurpose == runcontrol.RunPurposeReviseUnit {
		var value struct {
			UnitKey             string   `json:"unit_key"`
			Title               string   `json:"title"`
			Ordinal             int      `json:"ordinal"`
			Markdown            string   `json:"markdown"`
			ReplacementMarkdown string   `json:"replacement_markdown"`
			ContentHash         string   `json:"content_hash"`
			ClaimIDs            []string `json:"claim_ids"`
			UnknownIDs          []string `json:"unknown_ids"`
			PreservedUnknownIDs []string `json:"preserved_unknown_ids"`
		}
		if err := json.Unmarshal(output.Payload, &value); err != nil {
			return runcontrol.RunOutputReceipt{}, runcontrol.ErrInvalidPayload
		}
		if output.RunPurpose == runcontrol.RunPurposeReviseUnit {
			value.Markdown = value.ReplacementMarkdown
			value.Title = scope.CurrentUnitTitle
			value.Ordinal = scope.CurrentUnitOrdinal
			value.UnknownIDs = value.PreservedUnknownIDs
		}
		var unitID, outlineVersionID, unitTitle, reviewStatus string
		var unitOrdinal int
		var nodeKeys, dependsOn []string
		if err := tx.QueryRow(ctx, `SELECT unit_id,outline_version_id,COALESCE(title,''),COALESCE(ordinal,0),node_keys,depends_on,COALESCE(review_status,'PENDING') FROM go_confirmation_units WHERE task_id=$1 AND unit_key=$2 FOR UPDATE`, taskID, scope.CurrentUnitKey).Scan(&unitID, &outlineVersionID, &unitTitle, &unitOrdinal, &nodeKeys, &dependsOn, &reviewStatus); err != nil {
			return runcontrol.RunOutputReceipt{}, mapNotFound(err)
		}
		expectedStatus := "PENDING"
		if output.RunPurpose == runcontrol.RunPurposeReviseUnit {
			expectedStatus = "REOPENED"
		}
		if reviewStatus != expectedStatus || value.UnitKey != scope.CurrentUnitKey {
			return runcontrol.RunOutputReceipt{}, runcontrol.ErrInvalidRunStatus
		}
		var versionNo int64
		if err := tx.QueryRow(ctx, `SELECT COALESCE(MAX(unit_version_no),0)+1 FROM go_confirmation_unit_versions WHERE unit_id=$1 AND outline_version_id=$2`, unitID, outlineVersionID).Scan(&versionNo); err != nil {
			return runcontrol.RunOutputReceipt{}, err
		}
		normalizedHash := strings.TrimPrefix(value.ContentHash, "sha256:")
		if normalizedHash == "" {
			normalizedHash = confirmationRequestHash(strings.TrimSpace(value.Markdown))
		}
		normalizedPayload, _ := json.Marshal(runcontrol.ConfirmationCandidate{UnitKey: value.UnitKey, Title: unitTitle, Ordinal: unitOrdinal, Markdown: strings.TrimSpace(value.Markdown), ContentHash: normalizedHash, ClaimIDs: value.ClaimIDs, UnknownIDs: value.UnknownIDs, DependsOn: dependsOn})
		unitVersionID := "review-unit-version-" + confirmationRequestHash(lease.RunID, output.OutputKey)
		now := time.Now().UTC()
		if _, err := tx.Exec(ctx, `INSERT INTO go_confirmation_unit_versions (unit_version_id,unit_id,draft_id,content_hash,title,ordinal,payload,created_at,outline_version_id,source_run_id,unit_version_no) VALUES ($1,$2,NULL,$3,$4,$5,$6,$7,$8,$9,$10)`, unitVersionID, unitID, normalizedHash, unitTitle, unitOrdinal, normalizedPayload, now, outlineVersionID, lease.RunID, versionNo); err != nil {
			return runcontrol.RunOutputReceipt{}, err
		}
		if _, err := tx.Exec(ctx, `UPDATE go_confirmation_units SET review_status='REVIEWING' WHERE unit_id=$1`, unitID); err != nil {
			return runcontrol.RunOutputReceipt{}, err
		}
		taskVersion++
		if _, err := tx.Exec(ctx, `UPDATE go_control_tasks SET version=$2,status=$3,updated_at=$4 WHERE task_id=$1`, taskID, taskVersion, runcontrol.ReviewUnitReview, now); err != nil {
			return runcontrol.RunOutputReceipt{}, err
		}
		kind := "UNIT_VERSION"
		if _, err := tx.Exec(ctx, `UPDATE go_run_outputs SET materialized_kind=$3,materialized_id=$4,materialized_at=$5,materialized_task_version=$6 WHERE run_id=$1 AND output_key=$2`, lease.RunID, output.OutputKey, kind, unitVersionID, now, taskVersion); err != nil {
			return runcontrol.RunOutputReceipt{}, err
		}
		task := runcontrol.Task{TaskID: taskID, TenantID: tenantID, OwnerID: ownerID, Message: taskMessage, Version: taskVersion}
		if _, err := appendReviewEvent(ctx, tx, task, "review.unit_materialized", map[string]any{"unit_version_id": unitVersionID, "unit_version_no": versionNo, "task_version": taskVersion}, now); err != nil {
			return runcontrol.RunOutputReceipt{}, err
		}
	} else if output.RunPurpose == runcontrol.RunPurposeFullReview {
		var report struct {
			Outcome    string            `json:"outcome"`
			UnitHashes map[string]string `json:"unit_hashes"`
		}
		if err := json.Unmarshal(output.Payload, &report); err != nil || (report.Outcome != "PASSED" && report.Outcome != "NEEDS_REVISION") {
			return runcontrol.RunOutputReceipt{}, runcontrol.ErrInvalidPayload
		}
		var outlineVersionID string
		if err := tx.QueryRow(ctx, `SELECT v.outline_version_id FROM go_prd_outline_versions v JOIN go_prd_outlines o ON o.outline_id=v.outline_id WHERE o.task_id=$1 AND v.status='LOCKED'`, taskID).Scan(&outlineVersionID); err != nil {
			return runcontrol.RunOutputReceipt{}, mapNotFound(err)
		}
		unitHashesPayload, _ := json.Marshal(report.UnitHashes)
		reportID := "full-review-" + confirmationRequestHash(lease.RunID, output.OutputKey)
		now := time.Now().UTC()
		if _, err := tx.Exec(ctx, `INSERT INTO go_full_review_reports (report_id,task_id,outline_version_id,source_run_id,payload,content_hash,disposition,unit_hash_set_hash,created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)`, reportID, taskID, outlineVersionID, lease.RunID, output.Payload, contentHash, report.Outcome, confirmationRequestHash(string(unitHashesPayload)), now); err != nil {
			return runcontrol.RunOutputReceipt{}, err
		}
		taskVersion++
		nextStatus := runcontrol.ReviewFullReview
		if report.Outcome == "PASSED" {
			nextStatus = runcontrol.ReviewReviewable
		}
		if _, err := tx.Exec(ctx, `UPDATE go_control_tasks SET version=$2,status=$3,updated_at=$4 WHERE task_id=$1`, taskID, taskVersion, nextStatus, now); err != nil {
			return runcontrol.RunOutputReceipt{}, err
		}
		if _, err := tx.Exec(ctx, `UPDATE go_run_outputs SET materialized_kind='FULL_REVIEW_REPORT',materialized_id=$3,materialized_at=$4,materialized_task_version=$5 WHERE run_id=$1 AND output_key=$2`, lease.RunID, output.OutputKey, reportID, now, taskVersion); err != nil {
			return runcontrol.RunOutputReceipt{}, err
		}
		task := runcontrol.Task{TaskID: taskID, TenantID: tenantID, OwnerID: ownerID, Message: taskMessage, Version: taskVersion}
		if _, err := appendReviewEvent(ctx, tx, task, "review.full_review_materialized", map[string]any{"report_id": reportID, "disposition": report.Outcome, "task_version": taskVersion}, now); err != nil {
			return runcontrol.RunOutputReceipt{}, err
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.RunOutputReceipt{}, err
	}
	return runcontrol.RunOutputReceipt{OutputKey: output.OutputKey, ContentHash: contentHash, TaskVersion: taskVersion}, nil
}

func loadRunUnitScope(ctx context.Context, tx pgx.Tx, runID string) (runcontrol.UnitScope, error) {
	var payload []byte
	if err := tx.QueryRow(ctx, `SELECT scope_payload FROM go_run_unit_scopes WHERE run_id=$1`, runID).Scan(&payload); err != nil {
		return runcontrol.UnitScope{}, err
	}
	var scope runcontrol.UnitScope
	if err := json.Unmarshal(payload, &scope); err != nil {
		return runcontrol.UnitScope{}, err
	}
	return scope, nil
}

func insertRunUnitScope(ctx context.Context, tx pgx.Tx, runID string, expectedTaskVersion int, scope runcontrol.UnitScope, now time.Time) error {
	built, err := runcontrol.BuildUnitScope(scope)
	if err != nil {
		return err
	}
	payload, err := json.Marshal(built)
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `INSERT INTO go_run_unit_scopes (run_id,run_purpose,scope_payload,scope_hash,expected_task_version,created_at) VALUES ($1,$2,$3,$4,$5,$6)`, runID, built.Purpose, payload, built.ScopeHash, expectedTaskVersion, now)
	return err
}

func loadLatestUnitScopeForTask(ctx context.Context, query rowQuerier, taskID string) (runcontrol.UnitScope, error) {
	var payload []byte
	err := query.QueryRow(ctx, `SELECT s.scope_payload FROM go_run_unit_scopes s JOIN go_agent_runs r ON r.run_id=s.run_id WHERE r.task_id=$1 ORDER BY r.created_at DESC,r.run_id DESC LIMIT 1`, taskID).Scan(&payload)
	if err != nil {
		return runcontrol.UnitScope{}, err
	}
	var scope runcontrol.UnitScope
	if err := json.Unmarshal(payload, &scope); err != nil {
		return runcontrol.UnitScope{}, err
	}
	return scope, nil
}

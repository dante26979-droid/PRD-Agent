package storage

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"strconv"
	"strings"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/id"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/jackc/pgx/v5"
)

func (s *PostgresStore) ListConfirmationUnits(ctx context.Context, tenantID, ownerID, taskID string) ([]runcontrol.ConfirmationUnitVersion, error) {
	var isV4 bool
	if err := s.pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM go_prd_outlines o JOIN go_control_tasks t ON t.task_id=o.task_id WHERE o.task_id=$1 AND t.tenant_id=$2 AND t.owner_id=$3)`, taskID, tenantID, ownerID).Scan(&isV4); err != nil {
		return nil, err
	}
	if isV4 {
		return s.listReviewConfirmationUnits(ctx, tenantID, ownerID, taskID)
	}
	rows, err := s.pool.Query(ctx, `
		WITH latest_draft AS (
			SELECT d.draft_id FROM go_working_draft_versions d
			JOIN go_control_tasks t ON t.task_id=d.task_id
			WHERE d.task_id=$1 AND t.tenant_id=$2 AND t.owner_id=$3
			ORDER BY d.task_version DESC,d.created_at DESC LIMIT 1
		)
		SELECT u.unit_id,v.unit_version_id,COALESCE(v.draft_id,''),COALESCE(v.outline_version_id,''),COALESCE(v.source_run_id,''),COALESCE(v.unit_version_no,0),u.unit_key,v.title,v.ordinal,
		       v.payload,v.content_hash,
		       COALESCE((SELECT decision FROM go_confirmation_decisions cd
		                 WHERE cd.unit_version_id=v.unit_version_id
		                 ORDER BY cd.created_at DESC,cd.decision_id DESC LIMIT 1),'PENDING'),
		       v.created_at
		FROM go_confirmation_unit_versions v
		JOIN go_confirmation_units u ON u.unit_id=v.unit_id
		JOIN latest_draft d ON d.draft_id=v.draft_id
		ORDER BY v.ordinal,v.unit_version_id`, taskID, tenantID, ownerID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]runcontrol.ConfirmationUnitVersion, 0)
	for rows.Next() {
		item, err := scanConfirmationUnit(rows)
		if err != nil {
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

func (s *PostgresStore) listReviewConfirmationUnits(ctx context.Context, tenantID, ownerID, taskID string) ([]runcontrol.ConfirmationUnitVersion, error) {
	rows, err := s.pool.Query(ctx, `
		WITH latest AS (
			SELECT DISTINCT ON (u.unit_id)
			       u.unit_id,v.unit_version_id,COALESCE(v.draft_id,''),COALESCE(v.outline_version_id,''),
			       COALESCE(v.source_run_id,''),COALESCE(v.unit_version_no,0),u.unit_key,v.title,v.ordinal,
			       v.payload,v.content_hash,COALESCE(u.review_status,'PENDING'),v.created_at
			  FROM go_confirmation_units u
			  JOIN go_confirmation_unit_versions v ON v.unit_id=u.unit_id AND v.outline_version_id=u.outline_version_id
			  JOIN go_control_tasks t ON t.task_id=u.task_id
			 WHERE u.task_id=$1 AND t.tenant_id=$2 AND t.owner_id=$3
			 ORDER BY u.unit_id,v.unit_version_no DESC
		)
		SELECT * FROM latest ORDER BY ordinal,unit_version_id`, taskID, tenantID, ownerID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]runcontrol.ConfirmationUnitVersion, 0)
	for rows.Next() {
		item, err := scanConfirmationUnit(rows)
		if err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

func (s *PostgresStore) ConfirmConfirmationUnit(ctx context.Context, tenantID, ownerID, taskID, unitVersionID, idempotencyKey string, expectedTaskVersion int) (runcontrol.ConfirmationMutationResult, error) {
	return s.mutateConfirmation(ctx, tenantID, ownerID, taskID, unitVersionID, "", idempotencyKey, expectedTaskVersion, false)
}

func (s *PostgresStore) ReopenConfirmationUnit(ctx context.Context, tenantID, ownerID, taskID, unitVersionID, feedback, idempotencyKey string, expectedTaskVersion int) (runcontrol.ConfirmationMutationResult, error) {
	if strings.TrimSpace(feedback) == "" {
		return runcontrol.ConfirmationMutationResult{}, runcontrol.ErrInvalidPayload
	}
	return s.mutateConfirmation(ctx, tenantID, ownerID, taskID, unitVersionID, strings.TrimSpace(feedback), idempotencyKey, expectedTaskVersion, true)
}

func (s *PostgresStore) mutateConfirmation(ctx context.Context, tenantID, ownerID, taskID, unitVersionID, feedback, idempotencyKey string, expectedTaskVersion int, reopen bool) (runcontrol.ConfirmationMutationResult, error) {
	if idempotencyKey == "" {
		return runcontrol.ConfirmationMutationResult{}, runcontrol.ErrInvalidPayload
	}
	operation := "CONFIRM"
	decision := "CONFIRMED"
	if reopen {
		operation = "REOPEN"
		decision = "REOPENED"
	}
	requestHash := confirmationRequestHash(operation, taskID, unitVersionID, feedback, strconv.Itoa(expectedTaskVersion))
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.ConfirmationMutationResult{}, err
	}
	defer tx.Rollback(ctx)
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtext($1))`, strings.Join([]string{
		"confirmation-decision", tenantID, ownerID, idempotencyKey,
	}, "\x1f")); err != nil {
		return runcontrol.ConfirmationMutationResult{}, err
	}
	var replayID, replayHash string
	var replayVersion int
	var replayRunID string
	err = tx.QueryRow(ctx, `SELECT decision_id,request_hash,resulting_task_version,COALESCE(created_run_id,'') FROM go_confirmation_decisions WHERE tenant_id=$1 AND owner_id=$2 AND idempotency_key=$3`, tenantID, ownerID, idempotencyKey).Scan(&replayID, &replayHash, &replayVersion, &replayRunID)
	if err == nil {
		if replayHash != requestHash {
			return runcontrol.ConfirmationMutationResult{}, runcontrol.ErrInvalidIdempotency
		}
		unit, err := loadConfirmationUnit(ctx, tx, taskID, unitVersionID)
		if err != nil {
			return runcontrol.ConfirmationMutationResult{}, err
		}
		result := runcontrol.ConfirmationMutationResult{DecisionID: replayID, TaskVersion: replayVersion, Unit: unit}
		if replayRunID != "" {
			var run runcontrol.AgentRun
			if err := scanAgentRun(tx.QueryRow(ctx, `SELECT run_id,task_id,tenant_id,owner_id,workflow_version,status,queue_slot_acquired,attempt_count,COALESCE(lease_id,''),COALESCE(worker_id,''),fencing_token,COALESCE(lease_expires_at,'epoch'::timestamptz),created_at,updated_at FROM go_agent_runs WHERE run_id=$1`, replayRunID), &run); err != nil {
				return runcontrol.ConfirmationMutationResult{}, err
			}
			result.Run = &run
		} else if reopen {
			var run runcontrol.AgentRun
			err := scanAgentRun(tx.QueryRow(ctx, `SELECT r.run_id,r.task_id,r.tenant_id,r.owner_id,r.workflow_version,r.status,r.queue_slot_acquired,r.attempt_count,COALESCE(r.lease_id,''),COALESCE(r.worker_id,''),r.fencing_token,COALESCE(r.lease_expires_at,'epoch'::timestamptz),r.created_at,r.updated_at FROM go_agent_runs r JOIN go_run_revision_scopes s ON s.run_id=r.run_id WHERE r.task_id=$1 AND $2=ANY(s.reopened_unit_keys) ORDER BY r.created_at DESC LIMIT 1`, taskID, unit.UnitKey), &run)
			if err == nil {
				result.Run = &run
			}
		}
		if err := tx.Commit(ctx); err != nil {
			return runcontrol.ConfirmationMutationResult{}, err
		}
		return result, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.ConfirmationMutationResult{}, err
	}
	var taskVersion int
	if err := tx.QueryRow(ctx, `SELECT version FROM go_control_tasks WHERE task_id=$1 AND tenant_id=$2 AND owner_id=$3 FOR UPDATE`, taskID, tenantID, ownerID).Scan(&taskVersion); err != nil {
		return runcontrol.ConfirmationMutationResult{}, mapNotFound(err)
	}
	if taskVersion != expectedTaskVersion {
		return runcontrol.ConfirmationMutationResult{}, runcontrol.ErrTaskVersionConflict
	}
	unit, err := loadConfirmationUnit(ctx, tx, taskID, unitVersionID)
	if err != nil {
		return runcontrol.ConfirmationMutationResult{}, err
	}
	if unit.OutlineVersionID != "" {
		result, err := s.mutateReviewConfirmationTx(ctx, tx, tenantID, ownerID, taskID, unit, feedback, idempotencyKey, requestHash, expectedTaskVersion, reopen)
		if err != nil {
			return runcontrol.ConfirmationMutationResult{}, err
		}
		if err := tx.Commit(ctx); err != nil {
			return runcontrol.ConfirmationMutationResult{}, err
		}
		return result, nil
	}
	var latestDraftID, latestDraftHash string
	if err := tx.QueryRow(ctx, `SELECT draft_id,patch_hash FROM go_working_draft_versions WHERE task_id=$1 ORDER BY task_version DESC,created_at DESC LIMIT 1`, taskID).Scan(&latestDraftID, &latestDraftHash); err != nil {
		return runcontrol.ConfirmationMutationResult{}, mapNotFound(err)
	}
	if unit.DraftID != latestDraftID {
		return runcontrol.ConfirmationMutationResult{}, runcontrol.ErrNotFound
	}
	if reopen && unit.ConfirmationStatus != "CONFIRMED" {
		return runcontrol.ConfirmationMutationResult{}, runcontrol.ErrInvalidRunStatus
	}
	if !reopen {
		for _, dependency := range unit.DependsOn {
			var status string
			err := tx.QueryRow(ctx, `
				SELECT COALESCE((SELECT decision FROM go_confirmation_decisions d WHERE d.unit_version_id=v.unit_version_id ORDER BY d.created_at DESC,d.decision_id DESC LIMIT 1),'PENDING')
				FROM go_confirmation_unit_versions v JOIN go_confirmation_units u ON u.unit_id=v.unit_id
				WHERE v.draft_id=$1 AND u.unit_key=$2`, latestDraftID, dependency).Scan(&status)
			if err != nil || status != "CONFIRMED" {
				return runcontrol.ConfirmationMutationResult{}, runcontrol.ErrInvalidRunStatus
			}
		}
	}
	if reopen {
		var locked int
		if err := tx.QueryRow(ctx, `
			SELECT COUNT(*) FROM go_confirmation_unit_versions v
			WHERE v.draft_id=$1 AND v.payload->'depends_on' ? $2
			  AND COALESCE((SELECT decision FROM go_confirmation_decisions d WHERE d.unit_version_id=v.unit_version_id ORDER BY d.created_at DESC,d.decision_id DESC LIMIT 1),'PENDING')='CONFIRMED'`,
			latestDraftID, unit.UnitKey).Scan(&locked); err != nil {
			return runcontrol.ConfirmationMutationResult{}, err
		}
		if locked > 0 {
			return runcontrol.ConfirmationMutationResult{}, runcontrol.ErrInvalidRunStatus
		}
	}
	decisionID, err := id.New("decision")
	if err != nil {
		return runcontrol.ConfirmationMutationResult{}, err
	}
	now := time.Now().UTC()
	resultingVersion := taskVersion + 1
	if _, err := tx.Exec(ctx, `INSERT INTO go_confirmation_decisions (decision_id,unit_version_id,tenant_id,owner_id,decision,feedback,idempotency_key,request_hash,expected_task_version,resulting_task_version,created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)`, decisionID, unitVersionID, tenantID, ownerID, decision, feedback, idempotencyKey, requestHash, expectedTaskVersion, resultingVersion, now); err != nil {
		return runcontrol.ConfirmationMutationResult{}, err
	}
	unit.ConfirmationStatus = decision
	result := runcontrol.ConfirmationMutationResult{DecisionID: decisionID, TaskVersion: resultingVersion, Unit: unit}
	if reopen {
		run, err := s.createRevisionRun(ctx, tx, tenantID, ownerID, taskID, unit, feedback, latestDraftID, latestDraftHash, now)
		if err != nil {
			return runcontrol.ConfirmationMutationResult{}, err
		}
		result.Run = &run
		if _, err := tx.Exec(ctx, `UPDATE go_control_tasks SET version=$2,status='DRAFT',updated_at=$3 WHERE task_id=$1`, taskID, resultingVersion, now); err != nil {
			return runcontrol.ConfirmationMutationResult{}, err
		}
	} else {
		var pending int
		if err := tx.QueryRow(ctx, `
			SELECT COUNT(*) FROM go_confirmation_unit_versions v WHERE v.draft_id=$1
			  AND COALESCE((SELECT decision FROM go_confirmation_decisions d WHERE d.unit_version_id=v.unit_version_id ORDER BY d.created_at DESC,d.decision_id DESC LIMIT 1),'PENDING') <> 'CONFIRMED'`, latestDraftID).Scan(&pending); err != nil {
			return runcontrol.ConfirmationMutationResult{}, err
		}
		status := "DRAFT"
		if pending == 0 {
			status = "REVIEWABLE"
		}
		if _, err := tx.Exec(ctx, `UPDATE go_control_tasks SET version=$2,status=$3,updated_at=$4 WHERE task_id=$1`, taskID, resultingVersion, status, now); err != nil {
			return runcontrol.ConfirmationMutationResult{}, err
		}
	}
	if err := appendConfirmationEvent(ctx, tx, taskID, tenantID, ownerID, "confirmation_unit."+strings.ToLower(decision), result, now); err != nil {
		return runcontrol.ConfirmationMutationResult{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.ConfirmationMutationResult{}, err
	}
	return result, nil
}

func (s *PostgresStore) mutateReviewConfirmationTx(ctx context.Context, tx pgx.Tx, tenantID, ownerID, taskID string, unit runcontrol.ConfirmationUnitVersion, feedback, idempotencyKey, requestHash string, expectedTaskVersion int, reopen bool) (runcontrol.ConfirmationMutationResult, error) {
	var reviewStatus, title string
	var ordinal int
	var nodeKeys, dependsOn []string
	if err := tx.QueryRow(ctx, `SELECT COALESCE(review_status,'PENDING'),COALESCE(title,''),COALESCE(ordinal,0),node_keys,depends_on FROM go_confirmation_units WHERE unit_id=$1 FOR UPDATE`, unit.UnitID).Scan(&reviewStatus, &title, &ordinal, &nodeKeys, &dependsOn); err != nil {
		return runcontrol.ConfirmationMutationResult{}, err
	}
	var latestVersionNo int64
	if err := tx.QueryRow(ctx, `SELECT COALESCE(MAX(unit_version_no),0) FROM go_confirmation_unit_versions WHERE unit_id=$1 AND outline_version_id=$2`, unit.UnitID, unit.OutlineVersionID).Scan(&latestVersionNo); err != nil {
		return runcontrol.ConfirmationMutationResult{}, err
	}
	if latestVersionNo != unit.UnitVersionNo {
		return runcontrol.ConfirmationMutationResult{}, runcontrol.ErrTaskVersionConflict
	}
	if reopen {
		if reviewStatus != "CONFIRMED" || unit.ConfirmationStatus != "CONFIRMED" {
			return runcontrol.ConfirmationMutationResult{}, runcontrol.ErrInvalidRunStatus
		}
		var dependentCount int
		if err := tx.QueryRow(ctx, `SELECT COUNT(*) FROM go_confirmation_units WHERE task_id=$1 AND review_status='CONFIRMED' AND depends_on @> ARRAY[$2]::text[]`, taskID, unit.UnitKey).Scan(&dependentCount); err != nil {
			return runcontrol.ConfirmationMutationResult{}, err
		}
		if dependentCount != 0 {
			return runcontrol.ConfirmationMutationResult{}, runcontrol.ErrInvalidRunStatus
		}
	} else {
		if reviewStatus != "REVIEWING" {
			return runcontrol.ConfirmationMutationResult{}, runcontrol.ErrInvalidRunStatus
		}
		for _, dependency := range dependsOn {
			var status string
			if err := tx.QueryRow(ctx, `SELECT COALESCE(review_status,'PENDING') FROM go_confirmation_units WHERE task_id=$1 AND unit_key=$2`, taskID, dependency).Scan(&status); err != nil || status != "CONFIRMED" {
				return runcontrol.ConfirmationMutationResult{}, runcontrol.ErrInvalidRunStatus
			}
		}
	}
	var active int
	if err := tx.QueryRow(ctx, `SELECT COUNT(*) FROM go_agent_runs WHERE task_id=$1 AND status NOT IN ('SUCCEEDED','FAILED','STOPPED')`, taskID).Scan(&active); err != nil {
		return runcontrol.ConfirmationMutationResult{}, err
	}
	if active != 0 {
		return runcontrol.ConfirmationMutationResult{}, runcontrol.ErrInvalidRunStatus
	}
	decisionID, err := id.New("decision")
	if err != nil {
		return runcontrol.ConfirmationMutationResult{}, err
	}
	now := time.Now().UTC()
	decision, nextUnitStatus := "CONFIRMED", "CONFIRMED"
	if reopen {
		decision, nextUnitStatus = "REOPENED", "REOPENED"
	}
	if _, err := tx.Exec(ctx, `UPDATE go_confirmation_units SET review_status=$2 WHERE unit_id=$1`, unit.UnitID, nextUnitStatus); err != nil {
		return runcontrol.ConfirmationMutationResult{}, err
	}
	if reopen {
		if _, err := tx.Exec(ctx, `DELETE FROM go_full_review_reports WHERE task_id=$1`, taskID); err != nil {
			return runcontrol.ConfirmationMutationResult{}, err
		}
	}
	confirmed, err := loadReviewConfirmedContext(ctx, tx, taskID)
	if err != nil {
		return runcontrol.ConfirmationMutationResult{}, err
	}
	if reopen {
		confirmed = append(confirmed, runcontrol.ConfirmedUnitContext{UnitKey: unit.UnitKey, UnitVersion: unit.UnitVersionNo, ContentHash: unit.ContentHash, Summary: title, Markdown: unit.Markdown})
	}
	outline, err := loadReviewOutlineVersion(ctx, tx, taskID, unit.OutlineVersionID)
	if err != nil {
		return runcontrol.ConfirmationMutationResult{}, err
	}
	resultingVersion := expectedTaskVersion + 1
	task := runcontrol.Task{TaskID: taskID, TenantID: tenantID, OwnerID: ownerID, Version: resultingVersion}
	if err := tx.QueryRow(ctx, `SELECT message,created_at,updated_at FROM go_control_tasks WHERE task_id=$1`, taskID).Scan(&task.Message, &task.CreatedAt, &task.UpdatedAt); err != nil {
		return runcontrol.ConfirmationMutationResult{}, err
	}
	var createdRun *runcontrol.AgentRun
	nextTaskStatus := runcontrol.ReviewUnitGenerating
	if reopen {
		selected := runcontrol.OutlineUnit{UnitKey: unit.UnitKey, Title: title, Ordinal: ordinal, NodeKeys: nodeKeys, DependsOn: dependsOn}
		run, err := s.createReviewRunTx(ctx, tx, task, outline, selected, confirmed, runcontrol.RunPurposeReviseUnit, feedback, now)
		if err != nil {
			return runcontrol.ConfirmationMutationResult{}, err
		}
		createdRun = &run
	} else {
		planned, selected, allConfirmed, err := loadNextReviewUnit(ctx, tx, taskID)
		if err != nil {
			return runcontrol.ConfirmationMutationResult{}, err
		}
		if allConfirmed {
			nextTaskStatus = runcontrol.ReviewFullReviewRunning
			run, err := s.createReviewRunTx(ctx, tx, task, outline, runcontrol.OutlineUnit{}, confirmed, runcontrol.RunPurposeFullReview, "", now)
			if err != nil {
				return runcontrol.ConfirmationMutationResult{}, err
			}
			createdRun = &run
		} else {
			if !planned {
				return runcontrol.ConfirmationMutationResult{}, runcontrol.ErrInvalidRunStatus
			}
			run, err := s.createReviewRunTx(ctx, tx, task, outline, selected, confirmed, runcontrol.RunPurposeGenerateUnit, "", now)
			if err != nil {
				return runcontrol.ConfirmationMutationResult{}, err
			}
			createdRun = &run
		}
	}
	if _, err := tx.Exec(ctx, `INSERT INTO go_confirmation_decisions (decision_id,unit_version_id,tenant_id,owner_id,decision,feedback,idempotency_key,request_hash,expected_task_version,resulting_task_version,created_at,created_run_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)`, decisionID, unit.UnitVersionID, tenantID, ownerID, decision, feedback, idempotencyKey, requestHash, expectedTaskVersion, resultingVersion, now, createdRun.RunID); err != nil {
		return runcontrol.ConfirmationMutationResult{}, err
	}
	if _, err := tx.Exec(ctx, `UPDATE go_control_tasks SET version=$2,status=$3,updated_at=$4 WHERE task_id=$1`, taskID, resultingVersion, nextTaskStatus, now); err != nil {
		return runcontrol.ConfirmationMutationResult{}, err
	}
	unit.ConfirmationStatus = decision
	result := runcontrol.ConfirmationMutationResult{DecisionID: decisionID, TaskVersion: resultingVersion, Unit: unit, Run: createdRun}
	if err := appendConfirmationEvent(ctx, tx, taskID, tenantID, ownerID, "review.unit_"+strings.ToLower(decision), result, now); err != nil {
		return runcontrol.ConfirmationMutationResult{}, err
	}
	return result, nil
}

func loadReviewOutlineVersion(ctx context.Context, query rowQuerier, taskID, outlineVersionID string) (runcontrol.ReviewOutlineVersion, error) {
	var value runcontrol.ReviewOutlineVersion
	var payload []byte
	err := query.QueryRow(ctx, `SELECT o.outline_id,v.outline_version_id,o.task_id,v.version,v.status,v.payload,v.content_hash,v.source_run_id,v.created_at,v.locked_at FROM go_prd_outlines o JOIN go_prd_outline_versions v ON v.outline_id=o.outline_id WHERE o.task_id=$1 AND v.outline_version_id=$2`, taskID, outlineVersionID).Scan(&value.OutlineID, &value.OutlineVersionID, &value.TaskID, &value.Version, &value.Status, &payload, &value.ContentHash, &value.SourceRunID, &value.CreatedAt, &value.LockedAt)
	if err != nil {
		return value, mapNotFound(err)
	}
	if err := json.Unmarshal(payload, &value.Candidate); err != nil {
		return value, err
	}
	return value, nil
}

func loadReviewConfirmedContext(ctx context.Context, query pgx.Tx, taskID string) ([]runcontrol.ConfirmedUnitContext, error) {
	rows, err := query.Query(ctx, `
		SELECT DISTINCT ON (u.unit_id) u.unit_key,v.unit_version_no,v.content_hash,COALESCE(u.title,''),v.payload
		  FROM go_confirmation_units u JOIN go_confirmation_unit_versions v ON v.unit_id=u.unit_id
		 WHERE u.task_id=$1 AND u.review_status='CONFIRMED' AND v.outline_version_id=u.outline_version_id
		 ORDER BY u.unit_id,v.unit_version_no DESC`, taskID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]runcontrol.ConfirmedUnitContext, 0)
	for rows.Next() {
		var item runcontrol.ConfirmedUnitContext
		var payload []byte
		if err := rows.Scan(&item.UnitKey, &item.UnitVersion, &item.ContentHash, &item.Summary, &payload); err != nil {
			return nil, err
		}
		var candidate runcontrol.ConfirmationCandidate
		if err := json.Unmarshal(payload, &candidate); err != nil {
			return nil, err
		}
		item.Markdown = candidate.Markdown
		items = append(items, item)
	}
	return items, rows.Err()
}

func loadNextReviewUnit(ctx context.Context, query pgx.Tx, taskID string) (bool, runcontrol.OutlineUnit, bool, error) {
	rows, err := query.Query(ctx, `SELECT unit_key,COALESCE(title,''),COALESCE(ordinal,0),node_keys,depends_on,COALESCE(review_status,'PENDING') FROM go_confirmation_units WHERE task_id=$1 AND outline_version_id IS NOT NULL ORDER BY ordinal,unit_key`, taskID)
	if err != nil {
		return false, runcontrol.OutlineUnit{}, false, err
	}
	defer rows.Close()
	planned := make([]runcontrol.PlannedConfirmationUnit, 0)
	units := make(map[string]runcontrol.OutlineUnit)
	allConfirmed := true
	for rows.Next() {
		var unit runcontrol.OutlineUnit
		var status string
		if err := rows.Scan(&unit.UnitKey, &unit.Title, &unit.Ordinal, &unit.NodeKeys, &unit.DependsOn, &status); err != nil {
			return false, runcontrol.OutlineUnit{}, false, err
		}
		units[unit.UnitKey] = unit
		planned = append(planned, runcontrol.PlannedConfirmationUnit{UnitKey: unit.UnitKey, Ordinal: unit.Ordinal, DependsOn: unit.DependsOn, ConfirmationStatus: status})
		allConfirmed = allConfirmed && status == "CONFIRMED"
	}
	if err := rows.Err(); err != nil {
		return false, runcontrol.OutlineUnit{}, false, err
	}
	if allConfirmed && len(planned) > 0 {
		return false, runcontrol.OutlineUnit{}, true, nil
	}
	next, ok, err := runcontrol.NextDependencyReadyUnit(planned)
	if err != nil || !ok {
		return false, runcontrol.OutlineUnit{}, false, err
	}
	return true, units[next.UnitKey], false, nil
}

func (s *PostgresStore) createRevisionRun(ctx context.Context, tx pgx.Tx, tenantID, ownerID, taskID string, unit runcontrol.ConfirmationUnitVersion, feedback, draftID, draftHash string, now time.Time) (runcontrol.AgentRun, error) {
	var nonterminal int
	if err := tx.QueryRow(ctx, `SELECT COUNT(*) FROM go_agent_runs WHERE task_id=$1 AND status NOT IN ('SUCCEEDED','FAILED','STOPPED')`, taskID).Scan(&nonterminal); err != nil {
		return runcontrol.AgentRun{}, err
	}
	if nonterminal > 0 {
		return runcontrol.AgentRun{}, runcontrol.ErrInvalidRunStatus
	}
	var activeGlobal, activeOwner, waiting int
	if err := tx.QueryRow(ctx, `SELECT COUNT(*) FROM go_agent_runs WHERE queue_slot_acquired AND status IN ('QUEUED','RUNNING')`).Scan(&activeGlobal); err != nil {
		return runcontrol.AgentRun{}, err
	}
	if err := tx.QueryRow(ctx, `SELECT COUNT(*) FROM go_agent_runs WHERE tenant_id=$1 AND owner_id=$2 AND queue_slot_acquired AND status IN ('QUEUED','RUNNING')`, tenantID, ownerID).Scan(&activeOwner); err != nil {
		return runcontrol.AgentRun{}, err
	}
	status := runcontrol.RunQueued
	admitted := true
	if activeGlobal >= s.maxGlobalRunnable || activeOwner >= s.maxRunnablePerOwner {
		status, admitted = runcontrol.RunWaitingCapacity, false
		if err := tx.QueryRow(ctx, `SELECT COUNT(*) FROM go_agent_runs WHERE status='WAITING_CAPACITY'`).Scan(&waiting); err != nil {
			return runcontrol.AgentRun{}, err
		}
		if waiting >= s.maxWaitingRuns {
			return runcontrol.AgentRun{}, runcontrol.ErrCapacityExhausted
		}
	}
	runID, err := id.New("run")
	if err != nil {
		return runcontrol.AgentRun{}, err
	}
	runWorkflowVersion := s.defaultWorkflowVersion
	inheritedAssignment, assignmentErr := loadLatestRolloutAssignmentForTask(ctx, tx, taskID)
	if assignmentErr == nil {
		runWorkflowVersion = inheritedAssignment.AuthoritativeWorkflowVersion
	} else if !errors.Is(assignmentErr, pgx.ErrNoRows) {
		return runcontrol.AgentRun{}, assignmentErr
	}
	if _, err := tx.Exec(ctx, `INSERT INTO go_agent_runs (run_id,task_id,tenant_id,owner_id,workflow_version,execution_ledger_version,status,queue_slot_acquired,created_at,updated_at) VALUES ($1,$2,$3,$4,$5,NULLIF($6,''),$7,$8,$9,$9)`, runID, taskID, tenantID, ownerID, runWorkflowVersion, s.defaultExecutionLedgerVersion, status, admitted, now); err != nil {
		return runcontrol.AgentRun{}, err
	}
	if assignmentErr == nil {
		if err := insertRolloutAssignment(ctx, tx, runID, inheritedAssignment, now); err != nil {
			return runcontrol.AgentRun{}, err
		}
	}
	if err := s.initializeBudgetState(ctx, tx, runID, now); err != nil {
		return runcontrol.AgentRun{}, err
	}
	var immutable []string
	rows, err := tx.Query(ctx, `
		SELECT u.unit_key FROM go_confirmation_unit_versions v JOIN go_confirmation_units u ON u.unit_id=v.unit_id
		WHERE v.draft_id=$1 AND v.unit_version_id<>$2
		  AND COALESCE((SELECT decision FROM go_confirmation_decisions d WHERE d.unit_version_id=v.unit_version_id ORDER BY d.created_at DESC,d.decision_id DESC LIMIT 1),'PENDING')='CONFIRMED'
		ORDER BY u.unit_key`, draftID, unit.UnitVersionID)
	if err != nil {
		return runcontrol.AgentRun{}, err
	}
	for rows.Next() {
		var key string
		if err := rows.Scan(&key); err != nil {
			rows.Close()
			return runcontrol.AgentRun{}, err
		}
		immutable = append(immutable, key)
	}
	rows.Close()
	if _, err := tx.Exec(ctx, `INSERT INTO go_run_revision_scopes (run_id,base_draft_id,base_draft_hash,reopened_unit_keys,immutable_unit_keys,user_feedback,created_at) VALUES ($1,$2,$3,$4,$5,$6,$7)`, runID, draftID, draftHash, []string{unit.UnitKey}, immutable, feedback, now); err != nil {
		return runcontrol.AgentRun{}, err
	}
	if admitted {
		if _, err := tx.Exec(ctx, `INSERT INTO go_queue_slots (slot_id,run_id,tenant_id,owner_id,state,acquired_at) VALUES ($1,$2,$3,$4,'ACQUIRED',$5)`, "slot-"+runID, runID, tenantID, ownerID, now); err != nil {
			return runcontrol.AgentRun{}, err
		}
		if _, err := tx.Exec(ctx, `INSERT INTO go_outbox_messages (message_id,aggregate_id,event_type,payload,available_at) VALUES ($1,$2,'agent.run.requested',jsonb_build_object('run_id',$2::text,'task_id',$3::text,'reason','CONFIRMATION_REOPENED'),$4)`, "outbox-"+runID, runID, taskID, now); err != nil {
			return runcontrol.AgentRun{}, err
		}
	}
	return runcontrol.AgentRun{RunID: runID, TaskID: taskID, TenantID: tenantID, OwnerID: ownerID, WorkflowVersion: runWorkflowVersion, ExecutionLedgerVersion: s.defaultExecutionLedgerVersion, Status: status, QueueSlotAcquired: admitted, CreatedAt: now, UpdatedAt: now}, nil
}

func loadConfirmationUnit(ctx context.Context, query rowQuerier, taskID, unitVersionID string) (runcontrol.ConfirmationUnitVersion, error) {
	return scanConfirmationUnit(query.QueryRow(ctx, `
		SELECT u.unit_id,v.unit_version_id,COALESCE(v.draft_id,''),COALESCE(v.outline_version_id,''),COALESCE(v.source_run_id,''),COALESCE(v.unit_version_no,0),u.unit_key,v.title,v.ordinal,v.payload,v.content_hash,
		       COALESCE((SELECT decision FROM go_confirmation_decisions d WHERE d.unit_version_id=v.unit_version_id ORDER BY d.created_at DESC,d.decision_id DESC LIMIT 1),'PENDING'),
		       v.created_at
		FROM go_confirmation_unit_versions v JOIN go_confirmation_units u ON u.unit_id=v.unit_id
		WHERE u.task_id=$1 AND v.unit_version_id=$2`, taskID, unitVersionID))
}

type confirmationScanner interface {
	Scan(dest ...any) error
}

func scanConfirmationUnit(row confirmationScanner) (runcontrol.ConfirmationUnitVersion, error) {
	var item runcontrol.ConfirmationUnitVersion
	var payload []byte
	if err := row.Scan(&item.UnitID, &item.UnitVersionID, &item.DraftID, &item.OutlineVersionID, &item.SourceRunID, &item.UnitVersionNo, &item.UnitKey, &item.Title, &item.Ordinal, &payload, &item.ContentHash, &item.ConfirmationStatus, &item.CreatedAt); err != nil {
		return item, mapNotFound(err)
	}
	var candidate runcontrol.ConfirmationCandidate
	if err := json.Unmarshal(payload, &candidate); err != nil {
		return item, err
	}
	item.Markdown, item.ClaimIDs, item.UnknownIDs, item.DependsOn = candidate.Markdown, candidate.ClaimIDs, candidate.UnknownIDs, candidate.DependsOn
	return item, nil
}

func appendConfirmationEvent(ctx context.Context, tx pgx.Tx, taskID, tenantID, ownerID, eventType string, result runcontrol.ConfirmationMutationResult, now time.Time) error {
	eventID, err := id.New("event")
	if err != nil {
		return err
	}
	payload, err := json.Marshal(result)
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `INSERT INTO go_task_events (event_id,task_id,tenant_id,owner_id,sequence,event_type,payload,occurred_at) SELECT $1,$2,$3,$4,COALESCE(MAX(sequence),0)+1,$5,$6,$7 FROM go_task_events WHERE task_id=$2`, eventID, taskID, tenantID, ownerID, eventType, payload, now)
	return err
}

func confirmationRequestHash(parts ...string) string {
	digest := sha256.Sum256([]byte(strings.Join(parts, "\x00")))
	return hex.EncodeToString(digest[:])
}

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
	rows, err := s.pool.Query(ctx, `
		WITH latest_draft AS (
			SELECT d.draft_id FROM go_working_draft_versions d
			JOIN go_control_tasks t ON t.task_id=d.task_id
			WHERE d.task_id=$1 AND t.tenant_id=$2 AND t.owner_id=$3
			ORDER BY d.task_version DESC,d.created_at DESC LIMIT 1
		)
		SELECT u.unit_id,v.unit_version_id,v.draft_id,u.unit_key,v.title,v.ordinal,
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
	var replayID, replayHash string
	var replayVersion int
	err = tx.QueryRow(ctx, `SELECT decision_id,request_hash,resulting_task_version FROM go_confirmation_decisions WHERE tenant_id=$1 AND owner_id=$2 AND idempotency_key=$3`, tenantID, ownerID, idempotencyKey).Scan(&replayID, &replayHash, &replayVersion)
	if err == nil {
		if replayHash != requestHash {
			return runcontrol.ConfirmationMutationResult{}, runcontrol.ErrInvalidIdempotency
		}
		unit, err := loadConfirmationUnit(ctx, tx, taskID, unitVersionID)
		if err != nil {
			return runcontrol.ConfirmationMutationResult{}, err
		}
		result := runcontrol.ConfirmationMutationResult{DecisionID: replayID, TaskVersion: replayVersion, Unit: unit}
		if reopen {
			var run runcontrol.AgentRun
			err := scanAgentRun(tx.QueryRow(ctx, `SELECT r.run_id,r.task_id,r.tenant_id,r.owner_id,r.status,r.queue_slot_acquired,r.attempt_count,COALESCE(r.lease_id,''),COALESCE(r.worker_id,''),r.fencing_token,COALESCE(r.lease_expires_at,'epoch'::timestamptz),r.created_at,r.updated_at FROM go_agent_runs r JOIN go_run_revision_scopes s ON s.run_id=r.run_id WHERE r.task_id=$1 AND $2=ANY(s.reopened_unit_keys) ORDER BY r.created_at DESC LIMIT 1`, taskID, unit.UnitKey), &run)
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
	if _, err := tx.Exec(ctx, `INSERT INTO go_agent_runs (run_id,task_id,tenant_id,owner_id,status,queue_slot_acquired,created_at,updated_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$7)`, runID, taskID, tenantID, ownerID, status, admitted, now); err != nil {
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
	return runcontrol.AgentRun{RunID: runID, TaskID: taskID, TenantID: tenantID, OwnerID: ownerID, Status: status, QueueSlotAcquired: admitted, CreatedAt: now, UpdatedAt: now}, nil
}

func loadConfirmationUnit(ctx context.Context, query rowQuerier, taskID, unitVersionID string) (runcontrol.ConfirmationUnitVersion, error) {
	return scanConfirmationUnit(query.QueryRow(ctx, `
		SELECT u.unit_id,v.unit_version_id,v.draft_id,u.unit_key,v.title,v.ordinal,v.payload,v.content_hash,
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
	if err := row.Scan(&item.UnitID, &item.UnitVersionID, &item.DraftID, &item.UnitKey, &item.Title, &item.Ordinal, &payload, &item.ContentHash, &item.ConfirmationStatus, &item.CreatedAt); err != nil {
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

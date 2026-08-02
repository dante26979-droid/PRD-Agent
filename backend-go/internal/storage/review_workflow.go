package storage

import (
	"context"
	"encoding/json"
	"errors"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/id"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/jackc/pgx/v5"
)

func (s *PostgresStore) GetReviewOutline(ctx context.Context, tenantID, ownerID, taskID string) (runcontrol.ReviewOutlineVersion, error) {
	var value runcontrol.ReviewOutlineVersion
	var payload []byte
	err := s.pool.QueryRow(ctx, `
		SELECT o.outline_id,v.outline_version_id,o.task_id,v.version,v.status,v.payload,
		       v.content_hash,v.source_run_id,v.created_at,v.locked_at
		  FROM go_prd_outlines o
		  JOIN go_prd_outline_versions v ON v.outline_id=o.outline_id
		  JOIN go_control_tasks t ON t.task_id=o.task_id
		 WHERE o.task_id=$1 AND t.tenant_id=$2 AND t.owner_id=$3
		 ORDER BY v.version DESC LIMIT 1`, taskID, tenantID, ownerID).Scan(
		&value.OutlineID, &value.OutlineVersionID, &value.TaskID, &value.Version, &value.Status,
		&payload, &value.ContentHash, &value.SourceRunID, &value.CreatedAt, &value.LockedAt,
	)
	if err != nil {
		return runcontrol.ReviewOutlineVersion{}, mapNotFound(err)
	}
	if err := json.Unmarshal(payload, &value.Candidate); err != nil {
		return runcontrol.ReviewOutlineVersion{}, err
	}
	return value, nil
}

func (s *PostgresStore) ListReviewUnits(ctx context.Context, tenantID, ownerID, taskID string) ([]runcontrol.ReviewUnit, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT u.unit_id,u.task_id,u.outline_version_id,u.unit_key,COALESCE(u.title,''),
		       COALESCE(u.ordinal,0),u.node_keys,u.depends_on,COALESCE(u.review_status,'PENDING')
		  FROM go_confirmation_units u
		  JOIN go_control_tasks t ON t.task_id=u.task_id
		 WHERE u.task_id=$1 AND t.tenant_id=$2 AND t.owner_id=$3
		   AND u.outline_version_id IS NOT NULL
		 ORDER BY u.ordinal,u.unit_key`, taskID, tenantID, ownerID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]runcontrol.ReviewUnit, 0)
	for rows.Next() {
		var item runcontrol.ReviewUnit
		if err := rows.Scan(&item.UnitID, &item.TaskID, &item.OutlineVersionID, &item.UnitKey, &item.Title, &item.Ordinal, &item.NodeKeys, &item.DependsOn, &item.Status); err != nil {
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

func (s *PostgresStore) ConfirmOutline(ctx context.Context, command runcontrol.ConfirmOutlineCommand) (runcontrol.ReviewTransition, error) {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.ReviewTransition{}, err
	}
	defer tx.Rollback(ctx)
	// Serialize one logical command before checking its idempotency record. A
	// task row lock alone is too late: two transactions can both miss the
	// replay row, then the loser observes a stale task version instead of the
	// committed replay result.
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtext($1))`, strings.Join([]string{
		"review-transition", command.TenantID, command.OwnerID, "CONFIRM_OUTLINE", command.IdempotencyKey,
	}, "\x1f")); err != nil {
		return runcontrol.ReviewTransition{}, err
	}
	requestHash := confirmationRequestHash("CONFIRM_OUTLINE", command.TaskID, command.OutlineVersionID, strconv.Itoa(command.ExpectedTaskVersion))
	var replayHash string
	var replayPayload []byte
	err = tx.QueryRow(ctx, `SELECT request_hash,transition_payload FROM go_review_transition_idempotency WHERE tenant_id=$1 AND owner_id=$2 AND operation='CONFIRM_OUTLINE' AND idempotency_key=$3`, command.TenantID, command.OwnerID, command.IdempotencyKey).Scan(&replayHash, &replayPayload)
	if err == nil {
		if replayHash != requestHash {
			return runcontrol.ReviewTransition{}, runcontrol.ErrInvalidIdempotency
		}
		var replay runcontrol.ReviewTransition
		if err := json.Unmarshal(replayPayload, &replay); err != nil {
			return runcontrol.ReviewTransition{}, err
		}
		if err := tx.Commit(ctx); err != nil {
			return runcontrol.ReviewTransition{}, err
		}
		return replay, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.ReviewTransition{}, err
	}
	if command.IdempotencyKey == "" {
		return runcontrol.ReviewTransition{}, runcontrol.ErrInvalidPayload
	}
	var task runcontrol.Task
	if err := tx.QueryRow(ctx, `SELECT task_id,tenant_id,owner_id,message,status,version,created_at,updated_at FROM go_control_tasks WHERE task_id=$1 AND tenant_id=$2 AND owner_id=$3 FOR UPDATE`, command.TaskID, command.TenantID, command.OwnerID).Scan(&task.TaskID, &task.TenantID, &task.OwnerID, &task.Message, &task.Status, &task.Version, &task.CreatedAt, &task.UpdatedAt); err != nil {
		return runcontrol.ReviewTransition{}, mapNotFound(err)
	}
	if task.Version != command.ExpectedTaskVersion {
		return runcontrol.ReviewTransition{}, runcontrol.ErrTaskVersionConflict
	}
	var outline runcontrol.ReviewOutlineVersion
	var outlinePayload []byte
	if err := tx.QueryRow(ctx, `SELECT o.outline_id,v.outline_version_id,o.task_id,v.version,v.status,v.payload,v.content_hash,v.source_run_id,v.created_at,v.locked_at FROM go_prd_outlines o JOIN go_prd_outline_versions v ON v.outline_id=o.outline_id WHERE o.task_id=$1 AND v.outline_version_id=$2 FOR UPDATE`, command.TaskID, command.OutlineVersionID).Scan(&outline.OutlineID, &outline.OutlineVersionID, &outline.TaskID, &outline.Version, &outline.Status, &outlinePayload, &outline.ContentHash, &outline.SourceRunID, &outline.CreatedAt, &outline.LockedAt); err != nil {
		return runcontrol.ReviewTransition{}, mapNotFound(err)
	}
	if err := json.Unmarshal(outlinePayload, &outline.Candidate); err != nil {
		return runcontrol.ReviewTransition{}, err
	}
	if outline.Status != runcontrol.OutlineDraft || task.Status != string(runcontrol.ReviewOutlineReview) {
		return runcontrol.ReviewTransition{}, runcontrol.ErrInvalidRunStatus
	}
	var active int
	if err := tx.QueryRow(ctx, `SELECT COUNT(*) FROM go_agent_runs WHERE task_id=$1 AND status NOT IN ('SUCCEEDED','FAILED','STOPPED')`, task.TaskID).Scan(&active); err != nil {
		return runcontrol.ReviewTransition{}, err
	}
	if active != 0 {
		return runcontrol.ReviewTransition{}, runcontrol.ErrInvalidRunStatus
	}
	now := time.Now().UTC()
	if _, err := tx.Exec(ctx, `UPDATE go_prd_outline_versions SET status='LOCKED',locked_at=$2 WHERE outline_version_id=$1`, outline.OutlineVersionID, now); err != nil {
		return runcontrol.ReviewTransition{}, err
	}
	planned := make([]runcontrol.PlannedConfirmationUnit, 0, len(outline.Candidate.Units))
	for _, unit := range outline.Candidate.Units {
		unitID := "review-unit-" + confirmationRequestHash(task.TaskID, unit.UnitKey)
		if _, err := tx.Exec(ctx, `INSERT INTO go_confirmation_units (unit_id,task_id,unit_key,created_at,outline_version_id,title,ordinal,node_keys,depends_on,review_status) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'PENDING') ON CONFLICT (task_id,unit_key) DO NOTHING`, unitID, task.TaskID, unit.UnitKey, now, outline.OutlineVersionID, unit.Title, unit.Ordinal, unit.NodeKeys, unit.DependsOn); err != nil {
			return runcontrol.ReviewTransition{}, err
		}
		planned = append(planned, runcontrol.PlannedConfirmationUnit{UnitKey: unit.UnitKey, Ordinal: unit.Ordinal, DependsOn: unit.DependsOn, ConfirmationStatus: "PENDING"})
	}
	next, ok, err := runcontrol.NextDependencyReadyUnit(planned)
	if err != nil || !ok {
		if err != nil {
			return runcontrol.ReviewTransition{}, err
		}
		return runcontrol.ReviewTransition{}, runcontrol.ErrInvalidPayload
	}
	var selected runcontrol.OutlineUnit
	for _, unit := range outline.Candidate.Units {
		if unit.UnitKey == next.UnitKey {
			selected = unit
			break
		}
	}
	task.Version++
	run, err := s.createReviewRunTx(ctx, tx, task, outline, selected, nil, runcontrol.RunPurposeGenerateUnit, "", now)
	if err != nil {
		return runcontrol.ReviewTransition{}, err
	}
	if _, err := tx.Exec(ctx, `UPDATE go_control_tasks SET version=$2,status=$3,updated_at=$4 WHERE task_id=$1`, task.TaskID, task.Version, runcontrol.ReviewUnitGenerating, now); err != nil {
		return runcontrol.ReviewTransition{}, err
	}
	transition := runcontrol.ReviewTransition{TaskVersion: task.Version, TaskStatus: string(runcontrol.ReviewUnitGenerating), MaterializedKind: "LOCKED_OUTLINE", MaterializedID: outline.OutlineVersionID, CreatedRun: &run}
	eventSequence, err := appendReviewEvent(ctx, tx, task, "review.outline_confirmed", transition, now)
	if err != nil {
		return runcontrol.ReviewTransition{}, err
	}
	transition.EventSequence = eventSequence
	transitionPayload, _ := json.Marshal(transition)
	if _, err := tx.Exec(ctx, `INSERT INTO go_review_transition_idempotency (tenant_id,owner_id,operation,idempotency_key,request_hash,transition_payload,created_at) VALUES ($1,$2,'CONFIRM_OUTLINE',$3,$4,$5,$6)`, command.TenantID, command.OwnerID, command.IdempotencyKey, requestHash, transitionPayload, now); err != nil {
		return runcontrol.ReviewTransition{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.ReviewTransition{}, err
	}
	return transition, nil
}

func (s *PostgresStore) createReviewRunTx(ctx context.Context, tx pgx.Tx, task runcontrol.Task, outline runcontrol.ReviewOutlineVersion, selected runcontrol.OutlineUnit, confirmed []runcontrol.ConfirmedUnitContext, purpose runcontrol.RunPurpose, feedback string, now time.Time) (runcontrol.AgentRun, error) {
	var activeGlobal, activeOwner, waiting int
	if err := tx.QueryRow(ctx, `SELECT COUNT(*) FROM go_agent_runs WHERE queue_slot_acquired AND status IN ('QUEUED','RUNNING')`).Scan(&activeGlobal); err != nil {
		return runcontrol.AgentRun{}, err
	}
	if err := tx.QueryRow(ctx, `SELECT COUNT(*) FROM go_agent_runs WHERE tenant_id=$1 AND owner_id=$2 AND queue_slot_acquired AND status IN ('QUEUED','RUNNING')`, task.TenantID, task.OwnerID).Scan(&activeOwner); err != nil {
		return runcontrol.AgentRun{}, err
	}
	status, admitted := runcontrol.RunQueued, true
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
	run := runcontrol.AgentRun{RunID: runID, TaskID: task.TaskID, TenantID: task.TenantID, OwnerID: task.OwnerID, WorkflowVersion: runcontrol.WorkflowVersionV4, ExecutionLedgerVersion: runcontrol.ExecutionLedgerVersionV1, Status: status, QueueSlotAcquired: admitted, CreatedAt: now, UpdatedAt: now}
	if _, err := tx.Exec(ctx, `INSERT INTO go_agent_runs (run_id,task_id,tenant_id,owner_id,workflow_version,execution_ledger_version,status,queue_slot_acquired,created_at,updated_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$9)`, run.RunID, run.TaskID, run.TenantID, run.OwnerID, run.WorkflowVersion, run.ExecutionLedgerVersion, run.Status, run.QueueSlotAcquired, now); err != nil {
		return runcontrol.AgentRun{}, err
	}
	if assignment, assignmentErr := loadLatestRolloutAssignmentForTask(ctx, tx, task.TaskID); assignmentErr == nil {
		if err := insertRolloutAssignment(ctx, tx, runID, assignment, now); err != nil {
			return runcontrol.AgentRun{}, err
		}
	} else if !errors.Is(assignmentErr, pgx.ErrNoRows) {
		return runcontrol.AgentRun{}, assignmentErr
	}
	if err := s.initializeBudgetState(ctx, tx, runID, now); err != nil {
		return runcontrol.AgentRun{}, err
	}
	required := make([]string, 0)
	for _, node := range outline.Candidate.Nodes {
		if containsString(selected.NodeKeys, node.NodeKey) {
			required = append(required, node.RequiredContent...)
		}
	}
	sort.Strings(required)
	scope := runcontrol.UnitScope{Purpose: purpose, OutlineID: outline.OutlineID, OutlineVersion: outline.Version, OutlineHash: outline.ContentHash, ConfirmedContext: confirmed, RequirementRef: runcontrol.BuildExecutionInputReference(task.Message, required, outline.Candidate), RequirementHash: confirmationRequestHash(task.Message)}
	immutable := make([]string, 0, len(confirmed))
	for _, item := range confirmed {
		if purpose != runcontrol.RunPurposeReviseUnit || item.UnitKey != selected.UnitKey {
			immutable = append(immutable, item.UnitKey)
		}
	}
	scope.ImmutableUnitKeys = immutable
	if purpose == runcontrol.RunPurposeGenerateUnit || purpose == runcontrol.RunPurposeReviseUnit {
		scope.CurrentUnitKey, scope.CurrentUnitTitle, scope.CurrentUnitOrdinal = selected.UnitKey, selected.Title, selected.Ordinal
		scope.SectionNodeKeys, scope.DependencyUnitKeys = selected.NodeKeys, selected.DependsOn
	}
	if purpose == runcontrol.RunPurposeReviseUnit {
		for _, item := range confirmed {
			if item.UnitKey == selected.UnitKey {
				scope.BaseUnitHash = item.ContentHash
				break
			}
		}
		scope.ReopenedUnitKeys = []string{selected.UnitKey}
		scope.UserFeedback = feedback
	}
	if err := insertRunUnitScope(ctx, tx, runID, task.Version, scope, now); err != nil {
		return runcontrol.AgentRun{}, err
	}
	if admitted {
		if _, err := tx.Exec(ctx, `INSERT INTO go_queue_slots (slot_id,run_id,tenant_id,owner_id,state,acquired_at) VALUES ($1,$2,$3,$4,'ACQUIRED',$5)`, "slot-"+runID, runID, task.TenantID, task.OwnerID, now); err != nil {
			return runcontrol.AgentRun{}, err
		}
		if _, err := tx.Exec(ctx, `INSERT INTO go_outbox_messages (message_id,aggregate_id,event_type,payload,available_at) VALUES ($1,$2,'agent.run.requested',jsonb_build_object('run_id',$2::text,'task_id',$3::text,'reason','REVIEW_TRANSITION'),$4)`, "outbox-"+runID, runID, task.TaskID, now); err != nil {
			return runcontrol.AgentRun{}, err
		}
	}
	return run, nil
}

func appendReviewEvent(ctx context.Context, tx pgx.Tx, task runcontrol.Task, eventType string, value any, now time.Time) (int64, error) {
	eventID, err := id.New("event")
	if err != nil {
		return 0, err
	}
	payload, err := json.Marshal(value)
	if err != nil {
		return 0, err
	}
	var sequence int64
	err = tx.QueryRow(ctx, `INSERT INTO go_task_events (event_id,task_id,tenant_id,owner_id,sequence,event_type,payload,occurred_at) SELECT $1,$2,$3,$4,COALESCE(MAX(sequence),0)+1,$5,$6,$7 FROM go_task_events WHERE task_id=$2 RETURNING sequence`, eventID, task.TaskID, task.TenantID, task.OwnerID, eventType, payload, now).Scan(&sequence)
	return sequence, err
}

var _ runcontrol.ReviewWorkflow = (*PostgresStore)(nil)

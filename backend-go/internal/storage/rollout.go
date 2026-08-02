package storage

import (
	"context"
	"errors"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/jackc/pgx/v5"
)

func (s *PostgresStore) SaveRolloutAssignment(ctx context.Context, runID string, assignment runcontrol.RolloutAssignment) error {
	assignment = assignment.WithHash()
	if assignment.PolicyVersion == "" || !assignment.AuthoritativeWorkflowVersion.Supported() {
		return runcontrol.ErrInvalidPayload
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	var workflow runcontrol.WorkflowVersion
	if err := tx.QueryRow(ctx, `SELECT workflow_version FROM go_agent_runs WHERE run_id=$1 FOR UPDATE`, runID).Scan(&workflow); err != nil {
		return mapNotFound(err)
	}
	if workflow != assignment.AuthoritativeWorkflowVersion {
		return runcontrol.ErrInvalidPayload
	}
	var existing runcontrol.RolloutAssignment
	err = tx.QueryRow(ctx, `SELECT authoritative_workflow_version,evaluation_mode,COALESCE(shadow_workflow_version,''),cohort,policy_version,assignment_reason,assignment_hash FROM go_agent_rollout_assignments WHERE run_id=$1`, runID).Scan(
		&existing.AuthoritativeWorkflowVersion, &existing.EvaluationMode, &existing.ShadowWorkflowVersion,
		&existing.Cohort, &existing.PolicyVersion, &existing.AssignmentReason, &existing.AssignmentHash,
	)
	if err == nil {
		if existing != assignment {
			return runcontrol.ErrInvalidIdempotency
		}
		return tx.Commit(ctx)
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return err
	}
	if _, err := tx.Exec(ctx, `INSERT INTO go_agent_rollout_assignments (run_id,authoritative_workflow_version,evaluation_mode,shadow_workflow_version,cohort,policy_version,assignment_reason,assignment_hash,created_at) VALUES ($1,$2,$3,NULLIF($4,''),$5,$6,$7,$8,$9)`, runID, assignment.AuthoritativeWorkflowVersion, assignment.EvaluationMode, assignment.ShadowWorkflowVersion, assignment.Cohort, assignment.PolicyVersion, assignment.AssignmentReason, assignment.AssignmentHash, time.Now().UTC()); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *PostgresStore) GetRolloutAssignment(ctx context.Context, runID string) (runcontrol.RolloutAssignment, error) {
	var assignment runcontrol.RolloutAssignment
	err := s.pool.QueryRow(ctx, `SELECT authoritative_workflow_version,evaluation_mode,COALESCE(shadow_workflow_version,''),cohort,policy_version,assignment_reason,assignment_hash FROM go_agent_rollout_assignments WHERE run_id=$1`, runID).Scan(
		&assignment.AuthoritativeWorkflowVersion, &assignment.EvaluationMode, &assignment.ShadowWorkflowVersion,
		&assignment.Cohort, &assignment.PolicyVersion, &assignment.AssignmentReason, &assignment.AssignmentHash,
	)
	return assignment, mapNotFound(err)
}

func loadLatestRolloutAssignmentForTask(ctx context.Context, query rowQuerier, taskID string) (runcontrol.RolloutAssignment, error) {
	var assignment runcontrol.RolloutAssignment
	err := query.QueryRow(ctx, `
		SELECT a.authoritative_workflow_version,a.evaluation_mode,COALESCE(a.shadow_workflow_version,''),a.cohort,a.policy_version,a.assignment_reason,a.assignment_hash
		  FROM go_agent_rollout_assignments a
		  JOIN go_agent_runs r ON r.run_id=a.run_id
		 WHERE r.task_id=$1
		 ORDER BY r.created_at DESC,r.run_id DESC
		 LIMIT 1`, taskID).Scan(
		&assignment.AuthoritativeWorkflowVersion, &assignment.EvaluationMode, &assignment.ShadowWorkflowVersion,
		&assignment.Cohort, &assignment.PolicyVersion, &assignment.AssignmentReason, &assignment.AssignmentHash,
	)
	return assignment, err
}

func insertRolloutAssignment(ctx context.Context, tx pgx.Tx, runID string, assignment runcontrol.RolloutAssignment, now time.Time) error {
	assignment = assignment.WithHash()
	_, err := tx.Exec(ctx, `INSERT INTO go_agent_rollout_assignments (run_id,authoritative_workflow_version,evaluation_mode,shadow_workflow_version,cohort,policy_version,assignment_reason,assignment_hash,created_at) VALUES ($1,$2,$3,NULLIF($4,''),$5,$6,$7,$8,$9)`, runID, assignment.AuthoritativeWorkflowVersion, assignment.EvaluationMode, assignment.ShadowWorkflowVersion, assignment.Cohort, assignment.PolicyVersion, assignment.AssignmentReason, assignment.AssignmentHash, now)
	return err
}

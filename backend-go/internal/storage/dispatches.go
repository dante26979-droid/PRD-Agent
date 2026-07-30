package storage

import (
	"context"
	"errors"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/jackc/pgx/v5"
)

func (s *PostgresStore) ListQueuedRuns(ctx context.Context, limit int) ([]runcontrol.AgentRun, error) {
	if limit <= 0 {
		limit = 100
	}
	rows, err := s.pool.Query(ctx, `
		SELECT run_id, task_id, tenant_id, owner_id, status, queue_slot_acquired,
		       attempt_count, COALESCE(lease_id,''), COALESCE(worker_id,''), fencing_token,
		       COALESCE(lease_expires_at,'epoch'::timestamptz), created_at, updated_at
		  FROM go_agent_runs
		 WHERE status='QUEUED'
		 ORDER BY created_at, run_id
		 LIMIT $1`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]runcontrol.AgentRun, 0, limit)
	for rows.Next() {
		var run runcontrol.AgentRun
		if err := rows.Scan(&run.RunID, &run.TaskID, &run.TenantID, &run.OwnerID, &run.Status, &run.QueueSlotAcquired, &run.AttemptCount, &run.LeaseID, &run.WorkerID, &run.FencingToken, &run.LeaseExpiresAt, &run.CreatedAt, &run.UpdatedAt); err != nil {
			return nil, err
		}
		items = append(items, run)
	}
	return items, rows.Err()
}

func (s *PostgresStore) ListStoppingRuns(ctx context.Context, limit int) ([]runcontrol.AgentRun, error) {
	if limit <= 0 {
		limit = 100
	}
	rows, err := s.pool.Query(ctx, `
		SELECT run_id, task_id, tenant_id, owner_id, status, queue_slot_acquired,
		       attempt_count, COALESCE(lease_id,''), COALESCE(worker_id,''), fencing_token,
		       COALESCE(lease_expires_at,'epoch'::timestamptz), created_at, updated_at
		  FROM go_agent_runs
		 WHERE status='STOPPING'
		 ORDER BY updated_at, run_id
		 LIMIT $1`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]runcontrol.AgentRun, 0, limit)
	for rows.Next() {
		var run runcontrol.AgentRun
		if err := rows.Scan(&run.RunID, &run.TaskID, &run.TenantID, &run.OwnerID, &run.Status, &run.QueueSlotAcquired, &run.AttemptCount, &run.LeaseID, &run.WorkerID, &run.FencingToken, &run.LeaseExpiresAt, &run.CreatedAt, &run.UpdatedAt); err != nil {
			return nil, err
		}
		items = append(items, run)
	}
	return items, rows.Err()
}

func (s *PostgresStore) CreateDispatch(ctx context.Context, dispatch runcontrol.AgentDispatch) (runcontrol.AgentDispatch, error) {
	if dispatch.DispatchID == "" || dispatch.RunID == "" || dispatch.WorkerID == "" || dispatch.AttemptNo < 1 || dispatch.RequestHash == "" {
		return runcontrol.AgentDispatch{}, runcontrol.ErrInvalidPayload
	}
	if dispatch.Status == "" {
		dispatch.Status = runcontrol.DispatchStarted
	}
	if dispatch.StartedAt.IsZero() {
		dispatch.StartedAt = time.Now().UTC()
	}
	var tenantID, ownerID string
	if err := s.pool.QueryRow(ctx, `SELECT tenant_id, owner_id FROM go_agent_runs WHERE run_id=$1`, dispatch.RunID).Scan(&tenantID, &ownerID); errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.AgentDispatch{}, runcontrol.ErrNotFound
	} else if err != nil {
		return runcontrol.AgentDispatch{}, err
	}
	if tenantID != dispatch.TenantID || ownerID != dispatch.OwnerID {
		return runcontrol.AgentDispatch{}, runcontrol.ErrNotFound
	}
	var existing runcontrol.AgentDispatch
	if _, err := s.pool.Exec(ctx, `INSERT INTO go_agent_dispatches (dispatch_id, run_id, tenant_id, owner_id, worker_id, attempt_no, request_hash, status, started_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT (run_id, attempt_no) DO NOTHING`, dispatch.DispatchID, dispatch.RunID, dispatch.TenantID, dispatch.OwnerID, dispatch.WorkerID, dispatch.AttemptNo, dispatch.RequestHash, dispatch.Status, dispatch.StartedAt); err != nil {
		return runcontrol.AgentDispatch{}, err
	}
	if err := s.pool.QueryRow(ctx, `
		SELECT dispatch_id, run_id, tenant_id, owner_id, worker_id, attempt_no,
		       request_hash, status, last_error, started_at, finished_at
		  FROM go_agent_dispatches
		 WHERE run_id=$1 AND attempt_no=$2`, dispatch.RunID, dispatch.AttemptNo).
		Scan(&existing.DispatchID, &existing.RunID, &existing.TenantID, &existing.OwnerID, &existing.WorkerID, &existing.AttemptNo, &existing.RequestHash, &existing.Status, &existing.LastError, &existing.StartedAt, &existing.FinishedAt); err != nil {
		return runcontrol.AgentDispatch{}, err
	}
	if existing.RequestHash != dispatch.RequestHash {
		return runcontrol.AgentDispatch{}, runcontrol.ErrInvalidIdempotency
	}
	return existing, nil
}

func (s *PostgresStore) UpdateDispatch(ctx context.Context, dispatchID string, status runcontrol.DispatchStatus, lastError string, finishedAt *time.Time) error {
	result, err := s.pool.Exec(ctx, `UPDATE go_agent_dispatches SET status=$2, last_error=$3, finished_at=COALESCE($4, finished_at) WHERE dispatch_id=$1`, dispatchID, status, lastError, finishedAt)
	if err != nil {
		return err
	}
	if result.RowsAffected() == 0 {
		return runcontrol.ErrNotFound
	}
	return nil
}

func (s *PostgresStore) ListDispatches(ctx context.Context, status runcontrol.DispatchStatus, limit int) ([]runcontrol.AgentDispatch, error) {
	if limit <= 0 {
		limit = 100
	}
	rows, err := s.pool.Query(ctx, `
		SELECT dispatch_id, run_id, tenant_id, owner_id, worker_id, attempt_no,
		       request_hash, status, last_error, started_at, finished_at
		  FROM go_agent_dispatches
		 WHERE ($1='' OR status=$1)
		 ORDER BY started_at
		 LIMIT $2`, status, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]runcontrol.AgentDispatch, 0, limit)
	for rows.Next() {
		var dispatch runcontrol.AgentDispatch
		if err := rows.Scan(&dispatch.DispatchID, &dispatch.RunID, &dispatch.TenantID, &dispatch.OwnerID, &dispatch.WorkerID, &dispatch.AttemptNo, &dispatch.RequestHash, &dispatch.Status, &dispatch.LastError, &dispatch.StartedAt, &dispatch.FinishedAt); err != nil {
			return nil, err
		}
		items = append(items, dispatch)
	}
	return items, rows.Err()
}

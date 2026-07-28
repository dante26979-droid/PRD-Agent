package storage

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/id"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Config struct {
	DSN                 string
	MinConns            int32
	MaxConns            int32
	MaxGlobalRunnable   int
	MaxRunnablePerOwner int
}

type PostgresStore struct {
	pool                *pgxpool.Pool
	maxGlobalRunnable   int
	maxRunnablePerOwner int
}

func NewPostgresStore(ctx context.Context, cfg Config) (*PostgresStore, error) {
	poolConfig, err := pgxpool.ParseConfig(cfg.DSN)
	if err != nil {
		return nil, fmt.Errorf("parse postgres config: %w", err)
	}
	poolConfig.MinConns = cfg.MinConns
	poolConfig.MaxConns = cfg.MaxConns
	poolConfig.MaxConnLifetime = 30 * time.Minute
	pool, err := pgxpool.NewWithConfig(ctx, poolConfig)
	if err != nil {
		return nil, fmt.Errorf("create postgres pool: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping postgres: %w", err)
	}
	return &PostgresStore{
		pool:                pool,
		maxGlobalRunnable:   positiveOrDefault(cfg.MaxGlobalRunnable, 30),
		maxRunnablePerOwner: positiveOrDefault(cfg.MaxRunnablePerOwner, 1),
	}, nil
}

func (s *PostgresStore) Close() { s.pool.Close() }

func (s *PostgresStore) Pool() *pgxpool.Pool { return s.pool }

func positiveOrDefault(value, fallback int) int {
	if value < 1 {
		return fallback
	}
	return value
}

func (s *PostgresStore) Health(ctx context.Context) error {
	return s.pool.Ping(ctx)
}

func (s *PostgresStore) CreateTaskWithRun(ctx context.Context, tenantID, ownerID, message, idempotencyKey string) (runcontrol.TaskWithRun, error) {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.TaskWithRun{}, err
	}
	defer tx.Rollback(ctx)

	if _, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtext($1))", "prd-agent-go-admission"); err != nil {
		return runcontrol.TaskWithRun{}, err
	}
	var existingTaskID, existingRequestHash string
	err = tx.QueryRow(ctx, `
		SELECT resource_id, request_hash
		  FROM go_command_idempotency
		 WHERE tenant_id = $1 AND owner_id = $2 AND operation = 'START_TASK' AND idempotency_key = $3`,
		tenantID, ownerID, idempotencyKey).Scan(&existingTaskID, &existingRequestHash)
	if err == nil {
		if existingRequestHash != runcontrol.StartTaskRequestHash(tenantID, ownerID, message) {
			return runcontrol.TaskWithRun{}, runcontrol.ErrInvalidIdempotency
		}
		return s.loadTaskWithRun(ctx, tx, tenantID, ownerID, existingTaskID)
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.TaskWithRun{}, err
	}

	var activeGlobal, activeOwner int
	if err := tx.QueryRow(ctx, `SELECT COUNT(*) FROM go_agent_runs WHERE queue_slot_acquired AND status IN ('QUEUED','RUNNING')`).Scan(&activeGlobal); err != nil {
		return runcontrol.TaskWithRun{}, err
	}
	if err := tx.QueryRow(ctx, `SELECT COUNT(*) FROM go_agent_runs WHERE tenant_id = $1 AND owner_id = $2 AND queue_slot_acquired AND status IN ('QUEUED','RUNNING')`, tenantID, ownerID).Scan(&activeOwner); err != nil {
		return runcontrol.TaskWithRun{}, err
	}
	status := runcontrol.RunQueued
	admitted := true
	if activeGlobal >= s.maxGlobalRunnable || activeOwner >= s.maxRunnablePerOwner {
		status = runcontrol.RunWaitingCapacity
		admitted = false
	}
	taskID, err := id.New("task")
	if err != nil {
		return runcontrol.TaskWithRun{}, err
	}
	runID, err := id.New("run")
	if err != nil {
		return runcontrol.TaskWithRun{}, err
	}
	now := time.Now().UTC()
	if _, err := tx.Exec(ctx, `INSERT INTO go_control_tasks (task_id, tenant_id, owner_id, message, status, version, created_at, updated_at) VALUES ($1,$2,$3,$4,'DRAFT',1,$5,$5)`, taskID, tenantID, ownerID, message, now); err != nil {
		return runcontrol.TaskWithRun{}, err
	}
	if _, err := tx.Exec(ctx, `INSERT INTO go_agent_runs (run_id, task_id, tenant_id, owner_id, status, queue_slot_acquired, created_at, updated_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$7)`, runID, taskID, tenantID, ownerID, status, admitted, now); err != nil {
		return runcontrol.TaskWithRun{}, err
	}
	if _, err := tx.Exec(ctx, `INSERT INTO go_task_events (event_id, task_id, tenant_id, owner_id, sequence, event_type, payload, occurred_at) VALUES ($1,$2,$3,$4,1,'task.created',jsonb_build_object('task_id',$2::text,'run_id',$5::text,'status','DRAFT'),$6)`, "evt-"+taskID+"-1", taskID, tenantID, ownerID, runID, now); err != nil {
		return runcontrol.TaskWithRun{}, err
	}
	if admitted {
		if _, err := tx.Exec(ctx, `INSERT INTO go_queue_slots (slot_id, run_id, tenant_id, owner_id, state, acquired_at) VALUES ($1,$2,$3,$4,'ACQUIRED',$5)`, "slot-"+runID, runID, tenantID, ownerID, now); err != nil {
			return runcontrol.TaskWithRun{}, err
		}
	}
	if _, err := tx.Exec(ctx, `INSERT INTO go_command_idempotency (tenant_id, owner_id, operation, idempotency_key, request_hash, resource_id, created_at) VALUES ($1,$2,'START_TASK',$3,$4,$5,$6)`, tenantID, ownerID, idempotencyKey, runcontrol.StartTaskRequestHash(tenantID, ownerID, message), taskID, now); err != nil {
		return runcontrol.TaskWithRun{}, err
	}
	if _, err := tx.Exec(ctx, `INSERT INTO go_outbox_messages (message_id, aggregate_id, event_type, payload, available_at) VALUES ($1,$2,'agent.run.requested',jsonb_build_object('run_id',$2::text,'task_id',$3::text,'reason','START_OR_RESUME'),$4)`, "outbox-"+runID, runID, taskID, now); err != nil {
		return runcontrol.TaskWithRun{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.TaskWithRun{}, err
	}
	return runcontrol.TaskWithRun{
		Task: runcontrol.Task{TaskID: taskID, TenantID: tenantID, OwnerID: ownerID, Message: message, Status: "DRAFT", Version: 1, CreatedAt: now, UpdatedAt: now},
		Run:  runcontrol.AgentRun{RunID: runID, TaskID: taskID, TenantID: tenantID, OwnerID: ownerID, Status: status, QueueSlotAcquired: admitted, CreatedAt: now, UpdatedAt: now},
	}, nil
}

func (s *PostgresStore) loadTaskWithRun(ctx context.Context, tx pgx.Tx, tenantID, ownerID, taskID string) (runcontrol.TaskWithRun, error) {
	var task runcontrol.Task
	if err := tx.QueryRow(ctx, `SELECT task_id, tenant_id, owner_id, message, status, version, created_at, updated_at FROM go_control_tasks WHERE task_id=$1 AND tenant_id=$2 AND owner_id=$3`, taskID, tenantID, ownerID).Scan(&task.TaskID, &task.TenantID, &task.OwnerID, &task.Message, &task.Status, &task.Version, &task.CreatedAt, &task.UpdatedAt); err != nil {
		return runcontrol.TaskWithRun{}, err
	}
	var run runcontrol.AgentRun
	if err := tx.QueryRow(ctx, `SELECT run_id, task_id, tenant_id, owner_id, status, queue_slot_acquired, attempt_count, COALESCE(lease_id,''), COALESCE(worker_id,''), fencing_token, COALESCE(lease_expires_at,'epoch'::timestamptz), created_at, updated_at FROM go_agent_runs WHERE task_id=$1 ORDER BY created_at DESC LIMIT 1`, taskID).Scan(&run.RunID, &run.TaskID, &run.TenantID, &run.OwnerID, &run.Status, &run.QueueSlotAcquired, &run.AttemptCount, &run.LeaseID, &run.WorkerID, &run.FencingToken, &run.LeaseExpiresAt, &run.CreatedAt, &run.UpdatedAt); err != nil {
		return runcontrol.TaskWithRun{}, err
	}
	return runcontrol.TaskWithRun{Task: task, Run: run}, nil
}

func (s *PostgresStore) ListTasks(ctx context.Context, tenantID, ownerID string, limit int) ([]runcontrol.Task, error) {
	rows, err := s.pool.Query(ctx, `SELECT task_id, tenant_id, owner_id, message, status, version, created_at, updated_at FROM go_control_tasks WHERE tenant_id=$1 AND owner_id=$2 ORDER BY updated_at DESC LIMIT $3`, tenantID, ownerID, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]runcontrol.Task, 0)
	for rows.Next() {
		var item runcontrol.Task
		if err := rows.Scan(&item.TaskID, &item.TenantID, &item.OwnerID, &item.Message, &item.Status, &item.Version, &item.CreatedAt, &item.UpdatedAt); err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

func (s *PostgresStore) GetTask(ctx context.Context, tenantID, ownerID, taskID string) (runcontrol.Task, error) {
	var task runcontrol.Task
	err := s.pool.QueryRow(ctx, `SELECT task_id, tenant_id, owner_id, message, status, version, created_at, updated_at FROM go_control_tasks WHERE task_id=$1 AND tenant_id=$2 AND owner_id=$3`, taskID, tenantID, ownerID).Scan(&task.TaskID, &task.TenantID, &task.OwnerID, &task.Message, &task.Status, &task.Version, &task.CreatedAt, &task.UpdatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.Task{}, runcontrol.ErrNotFound
	}
	return task, err
}

func (s *PostgresStore) ListRuns(ctx context.Context, tenantID, ownerID, taskID string) ([]runcontrol.AgentRun, error) {
	if _, err := s.GetTask(ctx, tenantID, ownerID, taskID); err != nil {
		return nil, err
	}
	rows, err := s.pool.Query(ctx, `SELECT run_id, task_id, tenant_id, owner_id, status, queue_slot_acquired, attempt_count, COALESCE(lease_id,''), COALESCE(worker_id,''), fencing_token, COALESCE(lease_expires_at,'epoch'::timestamptz), created_at, updated_at FROM go_agent_runs WHERE task_id=$1 AND tenant_id=$2 AND owner_id=$3 ORDER BY created_at DESC`, taskID, tenantID, ownerID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]runcontrol.AgentRun, 0)
	for rows.Next() {
		var item runcontrol.AgentRun
		if err := rows.Scan(&item.RunID, &item.TaskID, &item.TenantID, &item.OwnerID, &item.Status, &item.QueueSlotAcquired, &item.AttemptCount, &item.LeaseID, &item.WorkerID, &item.FencingToken, &item.LeaseExpiresAt, &item.CreatedAt, &item.UpdatedAt); err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

func (s *PostgresStore) StopRun(ctx context.Context, tenantID, ownerID, taskID, runID string) (runcontrol.AgentRun, error) {
	var run runcontrol.AgentRun
	err := s.pool.QueryRow(ctx, `UPDATE go_agent_runs SET status=CASE WHEN status IN ('SUCCEEDED','FAILED','STOPPED') THEN status ELSE 'STOPPING' END, updated_at=NOW() WHERE run_id=$1 AND task_id=$2 AND tenant_id=$3 AND owner_id=$4 RETURNING run_id, task_id, tenant_id, owner_id, status, queue_slot_acquired, attempt_count, COALESCE(lease_id,''), COALESCE(worker_id,''), fencing_token, COALESCE(lease_expires_at,'epoch'::timestamptz), created_at, updated_at`, runID, taskID, tenantID, ownerID).Scan(&run.RunID, &run.TaskID, &run.TenantID, &run.OwnerID, &run.Status, &run.QueueSlotAcquired, &run.AttemptCount, &run.LeaseID, &run.WorkerID, &run.FencingToken, &run.LeaseExpiresAt, &run.CreatedAt, &run.UpdatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.AgentRun{}, runcontrol.ErrNotFound
	}
	return run, err
}

func (s *PostgresStore) AcquireRun(ctx context.Context, runID, workerID string, now time.Time, ttl time.Duration) (runcontrol.AgentRun, error) {
	if ttl <= 0 {
		ttl = 30 * time.Second
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.AgentRun{}, err
	}
	defer tx.Rollback(ctx)

	var run runcontrol.AgentRun
	err = tx.QueryRow(ctx, `SELECT run_id, task_id, tenant_id, owner_id, status, queue_slot_acquired, attempt_count, COALESCE(lease_id,''), COALESCE(worker_id,''), fencing_token, COALESCE(lease_expires_at,'epoch'::timestamptz), created_at, updated_at FROM go_agent_runs WHERE run_id=$1 FOR UPDATE`, runID).Scan(&run.RunID, &run.TaskID, &run.TenantID, &run.OwnerID, &run.Status, &run.QueueSlotAcquired, &run.AttemptCount, &run.LeaseID, &run.WorkerID, &run.FencingToken, &run.LeaseExpiresAt, &run.CreatedAt, &run.UpdatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.AgentRun{}, runcontrol.ErrNotFound
	}
	if err != nil {
		return runcontrol.AgentRun{}, err
	}
	if run.Status == runcontrol.RunRunning && run.LeaseExpiresAt.After(now) {
		return runcontrol.AgentRun{}, runcontrol.ErrLeaseHeld
	}
	if run.Status != runcontrol.RunQueued && !(run.Status == runcontrol.RunRunning && !run.LeaseExpiresAt.After(now)) {
		return runcontrol.AgentRun{}, runcontrol.ErrInvalidRunStatus
	}
	leaseID, err := id.New("lease")
	if err != nil {
		return runcontrol.AgentRun{}, err
	}
	run.Status = runcontrol.RunRunning
	run.WorkerID = workerID
	run.LeaseID = leaseID
	run.FencingToken++
	run.LeaseExpiresAt = now.Add(ttl)
	run.AttemptCount++
	run.UpdatedAt = now
	err = tx.QueryRow(ctx, `UPDATE go_agent_runs SET status='RUNNING', worker_id=$2, lease_id=$3, fencing_token=fencing_token+1, lease_expires_at=$4, attempt_count=attempt_count+1, updated_at=$5 WHERE run_id=$1 RETURNING run_id, task_id, tenant_id, owner_id, status, queue_slot_acquired, attempt_count, COALESCE(lease_id,''), COALESCE(worker_id,''), fencing_token, COALESCE(lease_expires_at,'epoch'::timestamptz), created_at, updated_at`, runID, workerID, leaseID, run.LeaseExpiresAt, now).Scan(&run.RunID, &run.TaskID, &run.TenantID, &run.OwnerID, &run.Status, &run.QueueSlotAcquired, &run.AttemptCount, &run.LeaseID, &run.WorkerID, &run.FencingToken, &run.LeaseExpiresAt, &run.CreatedAt, &run.UpdatedAt)
	if err != nil {
		return runcontrol.AgentRun{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.AgentRun{}, err
	}
	return run, nil
}

func (s *PostgresStore) Heartbeat(ctx context.Context, lease runcontrol.LeaseContext, now time.Time, ttl time.Duration) (runcontrol.LeaseContext, error) {
	if ttl <= 0 {
		ttl = 30 * time.Second
	}
	expiresAt := now.Add(ttl)
	var updated runcontrol.LeaseContext
	err := s.pool.QueryRow(ctx, `UPDATE go_agent_runs SET lease_expires_at=$6, updated_at=$7 WHERE run_id=$1 AND status IN ('RUNNING','STOPPING') AND lease_id=$2 AND worker_id=$3 AND fencing_token=$4 AND lease_expires_at>$5 RETURNING run_id, lease_id, worker_id, fencing_token, lease_expires_at`, lease.RunID, lease.LeaseID, lease.WorkerID, lease.FencingToken, now, expiresAt, now).Scan(&updated.RunID, &updated.LeaseID, &updated.WorkerID, &updated.FencingToken, &updated.ExpiresAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.LeaseContext{}, runcontrol.ErrLeaseLost
	}
	return updated, err
}

func (s *PostgresStore) CompleteRun(ctx context.Context, lease runcontrol.LeaseContext, status runcontrol.RunStatus, now time.Time) (runcontrol.AgentRun, error) {
	if !status.Terminal() {
		return runcontrol.AgentRun{}, runcontrol.ErrInvalidRunStatus
	}
	var run runcontrol.AgentRun
	err := s.pool.QueryRow(ctx, `UPDATE go_agent_runs SET status=$6, queue_slot_acquired=FALSE, lease_id=NULL, worker_id=NULL, lease_expires_at=NULL, updated_at=$7 WHERE run_id=$1 AND status IN ('RUNNING','STOPPING') AND lease_id=$2 AND worker_id=$3 AND fencing_token=$4 AND lease_expires_at>$5 RETURNING run_id, task_id, tenant_id, owner_id, status, queue_slot_acquired, attempt_count, COALESCE(lease_id,''), COALESCE(worker_id,''), fencing_token, COALESCE(lease_expires_at,'epoch'::timestamptz), created_at, updated_at`, lease.RunID, lease.LeaseID, lease.WorkerID, lease.FencingToken, now, status, now).Scan(&run.RunID, &run.TaskID, &run.TenantID, &run.OwnerID, &run.Status, &run.QueueSlotAcquired, &run.AttemptCount, &run.LeaseID, &run.WorkerID, &run.FencingToken, &run.LeaseExpiresAt, &run.CreatedAt, &run.UpdatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.AgentRun{}, runcontrol.ErrLeaseLost
	}
	return run, err
}

func (s *PostgresStore) PromoteWaiting(ctx context.Context, now time.Time) ([]runcontrol.AgentRun, error) {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	if _, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtext($1))", "prd-agent-go-admission"); err != nil {
		return nil, err
	}
	var activeGlobal int
	if err := tx.QueryRow(ctx, `SELECT COUNT(*) FROM go_agent_runs WHERE queue_slot_acquired AND status IN ('QUEUED','RUNNING')`).Scan(&activeGlobal); err != nil {
		return nil, err
	}
	promoted := make([]runcontrol.AgentRun, 0)
	for activeGlobal < s.maxGlobalRunnable {
		var run runcontrol.AgentRun
		err := tx.QueryRow(ctx, `
			SELECT r.run_id, r.task_id, r.tenant_id, r.owner_id, r.status,
			       r.queue_slot_acquired, r.attempt_count,
			       COALESCE(r.lease_id,''), COALESCE(r.worker_id,''), r.fencing_token,
			       COALESCE(r.lease_expires_at,'epoch'::timestamptz), r.created_at, r.updated_at
			  FROM go_agent_runs r
			 WHERE r.status='WAITING_CAPACITY'
			   AND NOT EXISTS (
			       SELECT 1 FROM go_agent_runs active
			        WHERE active.tenant_id=r.tenant_id AND active.owner_id=r.owner_id
			          AND active.queue_slot_acquired AND active.status IN ('QUEUED','RUNNING')
			   )
			 ORDER BY r.created_at, r.run_id
			 FOR UPDATE SKIP LOCKED
			 LIMIT 1`).Scan(&run.RunID, &run.TaskID, &run.TenantID, &run.OwnerID, &run.Status, &run.QueueSlotAcquired, &run.AttemptCount, &run.LeaseID, &run.WorkerID, &run.FencingToken, &run.LeaseExpiresAt, &run.CreatedAt, &run.UpdatedAt)
		if errors.Is(err, pgx.ErrNoRows) {
			break
		}
		if err != nil {
			return nil, err
		}
		if _, err := tx.Exec(ctx, `UPDATE go_agent_runs SET status='QUEUED', queue_slot_acquired=TRUE, updated_at=$2 WHERE run_id=$1`, run.RunID, now); err != nil {
			return nil, err
		}
		if _, err := tx.Exec(ctx, `INSERT INTO go_queue_slots (slot_id, run_id, tenant_id, owner_id, state, acquired_at) VALUES ($1,$2,$3,$4,'ACQUIRED',$5) ON CONFLICT (run_id) DO UPDATE SET state='ACQUIRED', acquired_at=EXCLUDED.acquired_at, released_at=NULL`, "slot-"+run.RunID, run.RunID, run.TenantID, run.OwnerID, now); err != nil {
			return nil, err
		}
		if _, err := tx.Exec(ctx, `INSERT INTO go_outbox_messages (message_id, aggregate_id, event_type, payload, available_at) VALUES ($1,$2,'agent.run.requested',jsonb_build_object('run_id',$2::text,'task_id',$3::text,'reason','CAPACITY_AVAILABLE'),$4) ON CONFLICT (message_id) DO NOTHING`, "outbox-"+run.RunID, run.RunID, run.TaskID, now); err != nil {
			return nil, err
		}
		run.Status = runcontrol.RunQueued
		run.QueueSlotAcquired = true
		run.UpdatedAt = now
		promoted = append(promoted, run)
		activeGlobal++
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return promoted, nil
}

func (s *PostgresStore) ClaimOutbox(ctx context.Context, limit int, now time.Time) ([]runcontrol.OutboxMessage, error) {
	if limit <= 0 {
		limit = 100
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	rows, err := tx.Query(ctx, `SELECT message_id, aggregate_id, event_type, payload, available_at, attempts FROM go_outbox_messages WHERE published_at IS NULL AND available_at <= $1 AND (claimed_at IS NULL OR claimed_at < $1 - INTERVAL '5 minutes') ORDER BY available_at, message_id FOR UPDATE SKIP LOCKED LIMIT $2`, now, limit)
	if err != nil {
		return nil, err
	}
	items := make([]runcontrol.OutboxMessage, 0, limit)
	for rows.Next() {
		var item runcontrol.OutboxMessage
		if err := rows.Scan(&item.MessageID, &item.AggregateID, &item.EventType, &item.Payload, &item.AvailableAt, &item.Attempts); err != nil {
			rows.Close()
			return nil, err
		}
		items = append(items, item)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, err
	}
	for _, item := range items {
		if _, err := tx.Exec(ctx, `UPDATE go_outbox_messages SET claimed_at=$2, attempts=attempts+1 WHERE message_id=$1`, item.MessageID, now); err != nil {
			return nil, err
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	for i := range items {
		items[i].Attempts++
	}
	return items, nil
}

func (s *PostgresStore) MarkOutboxPublished(ctx context.Context, messageID string, now time.Time) error {
	result, err := s.pool.Exec(ctx, `UPDATE go_outbox_messages SET published_at=$2, claimed_at=NULL WHERE message_id=$1 AND published_at IS NULL`, messageID, now)
	if err != nil {
		return err
	}
	if result.RowsAffected() == 0 {
		return runcontrol.ErrNotFound
	}
	return nil
}

func (s *PostgresStore) MarkOutboxFailed(ctx context.Context, messageID string, nextAttemptAt time.Time) error {
	result, err := s.pool.Exec(ctx, `UPDATE go_outbox_messages SET available_at=$2, claimed_at=NULL WHERE message_id=$1 AND published_at IS NULL`, messageID, nextAttemptAt)
	if err != nil {
		return err
	}
	if result.RowsAffected() == 0 {
		return runcontrol.ErrNotFound
	}
	return nil
}

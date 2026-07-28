package storage

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/id"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/jackc/pgx/v5"
)

func (s *PostgresStore) AppendTaskEvent(ctx context.Context, tenantID, ownerID, taskID, eventType string, payload []byte) (runcontrol.TaskEvent, error) {
	if eventType == "" || len(payload) == 0 || len(payload) > runcontrol.MaxEventPayloadBytes || !json.Valid(payload) {
		return runcontrol.TaskEvent{}, runcontrol.ErrInvalidPayload
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.TaskEvent{}, err
	}
	defer tx.Rollback(ctx)

	var tenant, owner string
	if err := tx.QueryRow(ctx, `SELECT tenant_id, owner_id FROM go_control_tasks WHERE task_id=$1 FOR UPDATE`, taskID).Scan(&tenant, &owner); errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.TaskEvent{}, runcontrol.ErrNotFound
	} else if err != nil {
		return runcontrol.TaskEvent{}, err
	}
	if tenant != tenantID || owner != ownerID {
		return runcontrol.TaskEvent{}, runcontrol.ErrNotFound
	}

	var sequence int64
	err = tx.QueryRow(ctx, `SELECT sequence FROM go_task_events WHERE task_id=$1 ORDER BY sequence DESC LIMIT 1`, taskID).Scan(&sequence)
	if errors.Is(err, pgx.ErrNoRows) {
		sequence = 0
	} else if err != nil {
		return runcontrol.TaskEvent{}, err
	}
	sequence++
	eventID, err := id.New("evt")
	if err != nil {
		return runcontrol.TaskEvent{}, err
	}
	now := time.Now().UTC()
	if _, err := tx.Exec(ctx, `INSERT INTO go_task_events (event_id, task_id, tenant_id, owner_id, sequence, event_type, payload, occurred_at) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8)`, eventID, taskID, tenantID, ownerID, sequence, eventType, payload, now); err != nil {
		return runcontrol.TaskEvent{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.TaskEvent{}, err
	}
	return runcontrol.TaskEvent{EventID: eventID, TaskID: taskID, TenantID: tenantID, OwnerID: ownerID, Sequence: sequence, EventType: eventType, Payload: append([]byte(nil), payload...), OccurredAt: now}, nil
}

func (s *PostgresStore) ListTaskEvents(ctx context.Context, tenantID, ownerID, taskID string, afterSequence int64, limit int) ([]runcontrol.TaskEvent, error) {
	if afterSequence < 0 {
		return nil, runcontrol.ErrInvalidPayload
	}
	if limit <= 0 {
		limit = runcontrol.DefaultEventPageSize
	}
	if limit > runcontrol.MaxEventPageSize {
		limit = runcontrol.MaxEventPageSize
	}
	rows, err := s.pool.Query(ctx, `SELECT event_id, task_id, tenant_id, owner_id, sequence, event_type, payload::text, occurred_at FROM go_task_events WHERE task_id=$1 AND tenant_id=$2 AND owner_id=$3 AND sequence>$4 ORDER BY sequence LIMIT $5`, taskID, tenantID, ownerID, afterSequence, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]runcontrol.TaskEvent, 0, limit)
	for rows.Next() {
		var event runcontrol.TaskEvent
		var payload string
		if err := rows.Scan(&event.EventID, &event.TaskID, &event.TenantID, &event.OwnerID, &event.Sequence, &event.EventType, &payload, &event.OccurredAt); err != nil {
			return nil, err
		}
		event.Payload = []byte(payload)
		items = append(items, event)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if len(items) == 0 {
		var exists bool
		if err := s.pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM go_control_tasks WHERE task_id=$1 AND tenant_id=$2 AND owner_id=$3)`, taskID, tenantID, ownerID).Scan(&exists); err != nil {
			return nil, fmt.Errorf("check task event scope: %w", err)
		}
		if !exists {
			return nil, runcontrol.ErrNotFound
		}
	}
	return items, nil
}

package storage

import (
	"context"
	"errors"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/jackc/pgx/v5"
)

func (s *PostgresStore) ClaimExpiredPublishPayloads(ctx context.Context, workerID string, now time.Time, limit int, ttl time.Duration) ([]runcontrol.PublishPayloadLease, error) {
	if workerID == "" {
		return nil, runcontrol.ErrInvalidPayload
	}
	if limit < 1 {
		limit = 1
	}
	if ttl <= 0 {
		ttl = time.Minute
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	rows, err := tx.Query(ctx, `
		WITH claim AS (
			SELECT publish_id FROM go_publish_intents
			 WHERE source_version_id IS NOT NULL AND publish_content IS NOT NULL
			   AND status IN ('SUCCEEDED','MANUAL_REVIEW')
			   AND updated_at <= $1
			   AND (retention_claim_expires_at IS NULL OR retention_claim_expires_at <= $2)
			 ORDER BY updated_at,publish_id FOR UPDATE SKIP LOCKED LIMIT $3
		)
		UPDATE go_publish_intents p
		   SET retention_claim_worker=$4,retention_claim_expires_at=$5
		  FROM claim WHERE p.publish_id=claim.publish_id
		RETURNING p.publish_id,p.content_hash,p.retention_claim_expires_at`,
		now.Add(-runcontrol.PublishPayloadRetentionWindow), now, limit, workerID, now.Add(ttl))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]runcontrol.PublishPayloadLease, 0, limit)
	for rows.Next() {
		item := runcontrol.PublishPayloadLease{WorkerID: workerID}
		if err := rows.Scan(&item.PublishID, &item.ContentHash, &item.ExpiresAt); err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return items, nil
}

func (s *PostgresStore) PurgePublishPayload(ctx context.Context, lease runcontrol.PublishPayloadLease, now time.Time) error {
	command, err := s.pool.Exec(ctx, `
		UPDATE go_publish_intents
		   SET publish_content=NULL,payload_purged_at=$5,
		       retention_claim_worker=NULL,retention_claim_expires_at=NULL
		 WHERE publish_id=$1 AND content_hash=$2 AND retention_claim_worker=$3
		   AND retention_claim_expires_at=$4 AND retention_claim_expires_at>$5
		   AND publish_content IS NOT NULL`,
		lease.PublishID, lease.ContentHash, lease.WorkerID, lease.ExpiresAt, now)
	if err != nil {
		return err
	}
	if command.RowsAffected() == 1 {
		return nil
	}
	var purged bool
	err = s.pool.QueryRow(ctx, `SELECT payload_purged_at IS NOT NULL FROM go_publish_intents WHERE publish_id=$1`, lease.PublishID).Scan(&purged)
	if errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.ErrNotFound
	}
	if err != nil {
		return err
	}
	if purged {
		return nil
	}
	return runcontrol.ErrLeaseLost
}

var _ runcontrol.PublishPayloadRetention = (*PostgresStore)(nil)

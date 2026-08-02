package storage

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/id"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/jackc/pgx/v5"
)

func (s *PostgresStore) CreatePublishPreview(ctx context.Context, tenantID, ownerID, taskID, idempotencyKey string, expectedTaskVersion int, now time.Time) (runcontrol.PublishPreviewResult, error) {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.PublishPreviewResult{}, err
	}
	defer tx.Rollback(ctx)
	requestHash := publishRequestHash(taskID, strconv.Itoa(expectedTaskVersion))
	var replayID, replayHash string
	err = tx.QueryRow(ctx, `SELECT resource_id, request_hash FROM go_command_idempotency WHERE tenant_id=$1 AND owner_id=$2 AND operation='PUBLISH_PREVIEW' AND idempotency_key=$3`, tenantID, ownerID, idempotencyKey).Scan(&replayID, &replayHash)
	if err == nil {
		if replayHash != requestHash {
			return runcontrol.PublishPreviewResult{}, runcontrol.ErrInvalidIdempotency
		}
		record, expiresAt, err := loadPublishRecord(ctx, tx, tenantID, ownerID, taskID, replayID, false)
		if err != nil {
			return runcontrol.PublishPreviewResult{}, err
		}
		if err := tx.Commit(ctx); err != nil {
			return runcontrol.PublishPreviewResult{}, err
		}
		return s.previewResult(record, expiresAt), nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.PublishPreviewResult{}, err
	}
	var taskVersion int
	var taskStatus string
	if err := tx.QueryRow(ctx, `SELECT version,status FROM go_control_tasks WHERE task_id=$1 AND tenant_id=$2 AND owner_id=$3`, taskID, tenantID, ownerID).Scan(&taskVersion, &taskStatus); err != nil {
		return runcontrol.PublishPreviewResult{}, mapNotFound(err)
	}
	if taskVersion != expectedTaskVersion {
		return runcontrol.PublishPreviewResult{}, runcontrol.ErrTaskVersionConflict
	}
	var draftID, sourceVersionID any
	var publishContent any
	var contentHash string
	var draftVersion int
	var isV4 bool
	if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM go_prd_outlines WHERE task_id=$1)`, taskID).Scan(&isV4); err != nil {
		return runcontrol.PublishPreviewResult{}, err
	}
	if isV4 {
		if taskStatus != string(runcontrol.ReviewReviewable) {
			return runcontrol.PublishPreviewResult{}, runcontrol.ErrInvalidRunStatus
		}
		document, version, err := buildV4PublishDocumentTx(ctx, tx, taskID, taskVersion)
		if err != nil {
			return runcontrol.PublishPreviewResult{}, err
		}
		draftID = nil
		sourceVersionID = document.SourceVersionID
		publishContent = document.Content
		contentHash, draftVersion = document.ContentHash, version
	} else {
		var legacyDraftID string
		if err := tx.QueryRow(ctx, `SELECT draft_id, patch_hash, task_version FROM go_working_draft_versions WHERE task_id=$1 ORDER BY task_version DESC, created_at DESC LIMIT 1`, taskID).Scan(&legacyDraftID, &contentHash, &draftVersion); err != nil {
			return runcontrol.PublishPreviewResult{}, mapNotFound(err)
		}
		var unitCount, unconfirmed int
		if err := tx.QueryRow(ctx, `SELECT COUNT(*),COUNT(*) FILTER (WHERE COALESCE((SELECT decision FROM go_confirmation_decisions d WHERE d.unit_version_id=v.unit_version_id ORDER BY d.created_at DESC,d.decision_id DESC LIMIT 1),'PENDING')<>'CONFIRMED') FROM go_confirmation_unit_versions v WHERE v.draft_id=$1`, legacyDraftID).Scan(&unitCount, &unconfirmed); err != nil {
			return runcontrol.PublishPreviewResult{}, err
		}
		if unitCount > 0 && unconfirmed > 0 {
			return runcontrol.PublishPreviewResult{}, runcontrol.ErrInvalidRunStatus
		}
		draftID, sourceVersionID, publishContent = legacyDraftID, nil, nil
	}
	publishID, err := id.New("publish")
	if err != nil {
		return runcontrol.PublishPreviewResult{}, err
	}
	// PostgreSQL stores timestamptz at microsecond precision. Bind the token to
	// that durable representation so the immediate response and a replayed
	// confirmation calculate exactly the same HMAC input.
	expiresAt := now.Add(10 * time.Minute).Truncate(time.Microsecond)
	if _, err := tx.Exec(ctx, `INSERT INTO go_publish_intents (publish_id,task_id,tenant_id,owner_id,draft_id,task_version,draft_version,content_hash,status,expires_at,available_at,created_at,updated_at,source_version_id,publish_content) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'PREVIEW',$9,$10,$10,$10,$11,$12)`, publishID, taskID, tenantID, ownerID, draftID, taskVersion, draftVersion, contentHash, expiresAt, now, sourceVersionID, publishContent); err != nil {
		return runcontrol.PublishPreviewResult{}, err
	}
	if _, err := tx.Exec(ctx, `INSERT INTO go_command_idempotency (tenant_id,owner_id,operation,idempotency_key,request_hash,resource_id,created_at) VALUES ($1,$2,'PUBLISH_PREVIEW',$3,$4,$5,$6)`, tenantID, ownerID, idempotencyKey, requestHash, publishID, now); err != nil {
		return runcontrol.PublishPreviewResult{}, err
	}
	if err := appendPublishEvent(ctx, tx, taskID, tenantID, ownerID, "publish.previewed", publishID, runcontrol.PublishPreview, now); err != nil {
		return runcontrol.PublishPreviewResult{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.PublishPreviewResult{}, err
	}
	record := runcontrol.PublishRecord{
		PublishID: publishID, TaskID: taskID, Status: runcontrol.PublishPreview,
		TaskVersion: taskVersion, DraftVersion: draftVersion, ContentHash: contentHash,
		CreatedAt: now, UpdatedAt: now,
	}
	return s.previewResult(record, expiresAt), nil
}

func (s *PostgresStore) ConfirmPublish(ctx context.Context, tenantID, ownerID, taskID, publishID, confirmationToken, idempotencyKey string, expectedTaskVersion int, now time.Time) (runcontrol.PublishRecord, error) {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.PublishRecord{}, err
	}
	defer tx.Rollback(ctx)
	requestHash := publishRequestHash(taskID, publishID, confirmationToken, strconv.Itoa(expectedTaskVersion))
	var replayID, replayHash string
	err = tx.QueryRow(ctx, `SELECT resource_id, request_hash FROM go_command_idempotency WHERE tenant_id=$1 AND owner_id=$2 AND operation='PUBLISH_CONFIRM' AND idempotency_key=$3`, tenantID, ownerID, idempotencyKey).Scan(&replayID, &replayHash)
	if err == nil {
		if replayHash != requestHash || replayID != publishID {
			return runcontrol.PublishRecord{}, runcontrol.ErrInvalidIdempotency
		}
		record, _, err := loadPublishRecord(ctx, tx, tenantID, ownerID, taskID, publishID, false)
		if err != nil {
			return runcontrol.PublishRecord{}, err
		}
		if err := tx.Commit(ctx); err != nil {
			return runcontrol.PublishRecord{}, err
		}
		return record, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.PublishRecord{}, err
	}
	record, expiresAt, err := loadPublishRecord(ctx, tx, tenantID, ownerID, taskID, publishID, true)
	if err != nil {
		return runcontrol.PublishRecord{}, err
	}
	var taskVersion int
	if err := tx.QueryRow(ctx, `SELECT version FROM go_control_tasks WHERE task_id=$1 FOR UPDATE`, taskID).Scan(&taskVersion); err != nil {
		return runcontrol.PublishRecord{}, err
	}
	if taskVersion != expectedTaskVersion || record.TaskVersion != expectedTaskVersion {
		return runcontrol.PublishRecord{}, runcontrol.ErrTaskVersionConflict
	}
	if record.Status != runcontrol.PublishPreview || !now.Before(expiresAt) {
		return runcontrol.PublishRecord{}, runcontrol.ErrInvalidRunStatus
	}
	if !hmac.Equal([]byte(s.confirmationToken(record, expiresAt)), []byte(confirmationToken)) {
		return runcontrol.PublishRecord{}, runcontrol.ErrInvalidPayload
	}
	if _, err := tx.Exec(ctx, `UPDATE go_publish_intents SET status='PENDING', available_at=$2, updated_at=$2 WHERE publish_id=$1`, publishID, now); err != nil {
		return runcontrol.PublishRecord{}, err
	}
	if _, err := tx.Exec(ctx, `UPDATE go_control_tasks SET status='PUBLISHING', updated_at=$2 WHERE task_id=$1`, taskID, now); err != nil {
		return runcontrol.PublishRecord{}, err
	}
	if _, err := tx.Exec(ctx, `INSERT INTO go_command_idempotency (tenant_id,owner_id,operation,idempotency_key,request_hash,resource_id,created_at) VALUES ($1,$2,'PUBLISH_CONFIRM',$3,$4,$5,$6)`, tenantID, ownerID, idempotencyKey, requestHash, publishID, now); err != nil {
		return runcontrol.PublishRecord{}, err
	}
	if err := appendPublishEvent(ctx, tx, taskID, tenantID, ownerID, "publish.pending", publishID, runcontrol.PublishPending, now); err != nil {
		return runcontrol.PublishRecord{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.PublishRecord{}, err
	}
	record.Status = runcontrol.PublishPending
	record.UpdatedAt = now
	return record, nil
}

func (s *PostgresStore) ListPublishes(ctx context.Context, tenantID, ownerID, taskID string, limit int) ([]runcontrol.PublishRecord, error) {
	if limit < 1 {
		limit = 100
	}
	rows, err := s.pool.Query(ctx, `SELECT publish_id,task_id,status,task_version,draft_version,content_hash,COALESCE(safe_url,''),COALESCE(provider_revision,''),COALESCE(error_code,''),retryable,created_at,updated_at FROM go_publish_intents WHERE task_id=$1 AND tenant_id=$2 AND owner_id=$3 ORDER BY created_at DESC LIMIT $4`, taskID, tenantID, ownerID, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]runcontrol.PublishRecord, 0)
	for rows.Next() {
		var item runcontrol.PublishRecord
		if err := scanPublishRecord(rows, &item); err != nil {
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

func buildV4PublishDocumentTx(ctx context.Context, tx pgx.Tx, taskID string, taskVersion int) (runcontrol.PublishDocument, int, error) {
	var outlineVersionID, outlineHash string
	var outlineVersion int
	var outlinePayload []byte
	if err := tx.QueryRow(ctx, `SELECT v.outline_version_id,v.version,v.content_hash,v.payload FROM go_prd_outline_versions v JOIN go_prd_outlines o ON o.outline_id=v.outline_id WHERE o.task_id=$1 AND v.status='LOCKED'`, taskID).Scan(&outlineVersionID, &outlineVersion, &outlineHash, &outlinePayload); err != nil {
		return runcontrol.PublishDocument{}, 0, mapNotFound(err)
	}
	var outline runcontrol.OutlineCandidate
	if err := json.Unmarshal(outlinePayload, &outline); err != nil {
		return runcontrol.PublishDocument{}, 0, err
	}
	var reportID, disposition string
	var reportPayload []byte
	if err := tx.QueryRow(ctx, `SELECT report_id,disposition,payload FROM go_full_review_reports WHERE task_id=$1 AND outline_version_id=$2 ORDER BY created_at DESC LIMIT 1`, taskID, outlineVersionID).Scan(&reportID, &disposition, &reportPayload); err != nil {
		return runcontrol.PublishDocument{}, 0, mapNotFound(err)
	}
	if disposition != "PASSED" {
		return runcontrol.PublishDocument{}, 0, runcontrol.ErrInvalidRunStatus
	}
	var report struct {
		OutlineHash string            `json:"outline_hash"`
		UnitHashes  map[string]string `json:"unit_hashes"`
	}
	if err := json.Unmarshal(reportPayload, &report); err != nil || strings.TrimPrefix(report.OutlineHash, "sha256:") != strings.TrimPrefix(outlineHash, "sha256:") {
		return runcontrol.PublishDocument{}, 0, runcontrol.ErrInvalidRunStatus
	}
	parts := []string{"# " + strings.TrimSpace(outline.Title)}
	for _, planned := range outline.Units {
		var status, contentHash string
		var payload []byte
		err := tx.QueryRow(ctx, `
			SELECT COALESCE(u.review_status,'PENDING'),v.content_hash,v.payload
			  FROM go_confirmation_units u JOIN go_confirmation_unit_versions v ON v.unit_id=u.unit_id
			 WHERE u.task_id=$1 AND u.unit_key=$2 AND u.outline_version_id=$3
			 ORDER BY v.unit_version_no DESC LIMIT 1`, taskID, planned.UnitKey, outlineVersionID).Scan(&status, &contentHash, &payload)
		if err != nil {
			return runcontrol.PublishDocument{}, 0, mapNotFound(err)
		}
		if status != "CONFIRMED" || strings.TrimPrefix(report.UnitHashes[planned.UnitKey], "sha256:") != strings.TrimPrefix(contentHash, "sha256:") {
			return runcontrol.PublishDocument{}, 0, runcontrol.ErrInvalidRunStatus
		}
		var candidate runcontrol.ConfirmationCandidate
		if err := json.Unmarshal(payload, &candidate); err != nil {
			return runcontrol.PublishDocument{}, 0, err
		}
		parts = append(parts, strings.TrimSpace(candidate.Markdown))
	}
	if len(report.UnitHashes) != len(outline.Units) {
		return runcontrol.PublishDocument{}, 0, runcontrol.ErrInvalidRunStatus
	}
	content := []byte(strings.Join(parts, "\n\n"))
	return runcontrol.PublishDocument{
		WorkflowVersion: runcontrol.WorkflowVersionV4,
		SourceVersionID: outlineVersionID + ":" + reportID,
		Content:         content, ContentHash: confirmationRequestHash(string(content)), TaskVersion: taskVersion,
	}, outlineVersion, nil
}

func (s *PostgresStore) ClaimPendingPublishes(ctx context.Context, workerID string, limit int, now time.Time, ttl time.Duration) ([]runcontrol.PublishJob, error) {
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
			SELECT publish_id, status AS source_status
			  FROM go_publish_intents
			 WHERE status IN ('PENDING','FAILED_RETRYABLE','RECONCILING')
			   AND available_at <= $1
			   AND (claim_expires_at IS NULL OR claim_expires_at <= $1)
			 ORDER BY available_at, created_at
			 FOR UPDATE SKIP LOCKED
			 LIMIT $2
		)
		UPDATE go_publish_intents p
		   SET status='RUNNING', claim_worker=$3, claim_expires_at=$4, updated_at=$1
		  FROM claim
		 WHERE p.publish_id=claim.publish_id
		RETURNING p.publish_id,p.task_id,p.status,p.task_version,p.draft_version,p.content_hash,
		          COALESCE(p.safe_url,''),COALESCE(p.provider_revision,''),COALESCE(p.error_code,''),
		          p.retryable,p.created_at,p.updated_at,p.tenant_id,p.owner_id,
		          COALESCE((SELECT d.patch FROM go_working_draft_versions d WHERE d.draft_id=p.draft_id),p.publish_content),
		          claim.source_status='RECONCILING'`, now, limit, workerID, now.Add(ttl))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]runcontrol.PublishJob, 0)
	for rows.Next() {
		var job runcontrol.PublishJob
		if err := rows.Scan(
			&job.Record.PublishID, &job.Record.TaskID, &job.Record.Status,
			&job.Record.TaskVersion, &job.Record.DraftVersion, &job.Record.ContentHash,
			&job.Record.SafeURL, &job.Record.ProviderRevision, &job.Record.ErrorCode,
			&job.Record.Retryable, &job.Record.CreatedAt, &job.Record.UpdatedAt,
			&job.TenantID, &job.OwnerID, &job.Content, &job.ReconcileOnly,
		); err != nil {
			return nil, err
		}
		items = append(items, job)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return items, nil
}

func (s *PostgresStore) CompletePublish(ctx context.Context, workerID, publishID string, result runcontrol.PublishResult, now time.Time) error {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	availableAt := now
	if result.Status == runcontrol.PublishRetryable {
		availableAt = now.Add(30 * time.Second)
	}
	if result.Status == runcontrol.PublishReconciling {
		availableAt = now.Add(10 * time.Second)
	}
	var taskID, tenantID, ownerID string
	err = tx.QueryRow(ctx, `UPDATE go_publish_intents SET status=$4,safe_url=NULLIF($5,''),provider_revision=NULLIF($6,''),error_code=NULLIF($7,''),retryable=$8,available_at=$9,claim_worker=NULL,claim_expires_at=NULL,updated_at=$3 WHERE publish_id=$1 AND status='RUNNING' AND claim_worker=$2 AND claim_expires_at>$3 RETURNING task_id,tenant_id,owner_id`, publishID, workerID, now, result.Status, result.SafeURL, result.ProviderRevision, result.ErrorCode, result.Retryable, availableAt).Scan(&taskID, &tenantID, &ownerID)
	if errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.ErrLeaseLost
	}
	if err != nil {
		return err
	}
	if result.Status == runcontrol.PublishSucceeded {
		if _, err := tx.Exec(ctx, `UPDATE go_control_tasks SET status='PUBLISHED',updated_at=$2 WHERE task_id=$1`, taskID, now); err != nil {
			return err
		}
	} else if result.Status == runcontrol.PublishManualReview {
		if _, err := tx.Exec(ctx, `UPDATE go_control_tasks SET status='REVIEWABLE',updated_at=$2 WHERE task_id=$1`, taskID, now); err != nil {
			return err
		}
	}
	if err := appendPublishEvent(ctx, tx, taskID, tenantID, ownerID, "publish."+strings.ToLower(string(result.Status)), publishID, result.Status, now); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *PostgresStore) previewResult(record runcontrol.PublishRecord, expiresAt time.Time) runcontrol.PublishPreviewResult {
	return runcontrol.PublishPreviewResult{
		PublishID: record.PublishID, ConfirmationToken: s.confirmationToken(record, expiresAt),
		TaskVersion: record.TaskVersion, DraftVersion: record.DraftVersion,
		ContentHash: record.ContentHash, Target: "FEISHU_FIXED_WIKI_DOCX", ExpiresAt: expiresAt,
	}
}

func (s *PostgresStore) confirmationToken(record runcontrol.PublishRecord, expiresAt time.Time) string {
	mac := hmac.New(sha256.New, []byte(s.confirmationSecret))
	_, _ = mac.Write([]byte(strings.Join([]string{
		record.PublishID, record.TaskID, strconv.Itoa(record.TaskVersion),
		strconv.Itoa(record.DraftVersion), record.ContentHash, expiresAt.UTC().Format(time.RFC3339Nano),
	}, "\x00")))
	return base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

func publishRequestHash(parts ...string) string {
	digest := sha256.Sum256([]byte(strings.Join(parts, "\x00")))
	return fmt.Sprintf("%x", digest[:])
}

func loadPublishRecord(ctx context.Context, tx pgx.Tx, tenantID, ownerID, taskID, publishID string, lock bool) (runcontrol.PublishRecord, time.Time, error) {
	suffix := ""
	if lock {
		suffix = " FOR UPDATE"
	}
	var record runcontrol.PublishRecord
	var expiresAt time.Time
	row := tx.QueryRow(ctx, `SELECT publish_id,task_id,status,task_version,draft_version,content_hash,COALESCE(safe_url,''),COALESCE(provider_revision,''),COALESCE(error_code,''),retryable,created_at,updated_at,expires_at FROM go_publish_intents WHERE publish_id=$1 AND task_id=$2 AND tenant_id=$3 AND owner_id=$4`+suffix, publishID, taskID, tenantID, ownerID)
	err := row.Scan(
		&record.PublishID, &record.TaskID, &record.Status, &record.TaskVersion,
		&record.DraftVersion, &record.ContentHash, &record.SafeURL,
		&record.ProviderRevision, &record.ErrorCode, &record.Retryable,
		&record.CreatedAt, &record.UpdatedAt, &expiresAt,
	)
	return record, expiresAt, mapNotFound(err)
}

type publishRecordScanner interface {
	Scan(dest ...any) error
}

func scanPublishRecord(row publishRecordScanner, record *runcontrol.PublishRecord) error {
	return row.Scan(
		&record.PublishID, &record.TaskID, &record.Status, &record.TaskVersion,
		&record.DraftVersion, &record.ContentHash, &record.SafeURL,
		&record.ProviderRevision, &record.ErrorCode, &record.Retryable,
		&record.CreatedAt, &record.UpdatedAt,
	)
}

func appendPublishEvent(ctx context.Context, tx pgx.Tx, taskID, tenantID, ownerID, eventType, publishID string, status runcontrol.PublishStatus, now time.Time) error {
	eventID, err := id.New("event")
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `INSERT INTO go_task_events (event_id,task_id,tenant_id,owner_id,sequence,event_type,payload,occurred_at) SELECT $1,$2,$3,$4,COALESCE(MAX(sequence),0)+1,$5,jsonb_build_object('publish_id',$6::text,'status',$7::text),$8 FROM go_task_events WHERE task_id=$2`, eventID, taskID, tenantID, ownerID, eventType, publishID, status, now)
	return err
}

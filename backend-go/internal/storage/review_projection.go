package storage

import (
	"context"
	"encoding/json"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
)

func (s *PostgresStore) GetFullReviewReport(ctx context.Context, tenantID, ownerID, taskID string) (runcontrol.FullReviewReport, error) {
	var report runcontrol.FullReviewReport
	var payload []byte
	err := s.pool.QueryRow(ctx, `
		SELECT r.report_id,r.task_id,r.outline_version_id,r.source_run_id,r.payload,
		       r.content_hash,r.disposition,r.unit_hash_set_hash,r.created_at
		  FROM go_full_review_reports r
		  JOIN go_control_tasks t ON t.task_id=r.task_id
		 WHERE r.task_id=$1 AND t.tenant_id=$2 AND t.owner_id=$3
		 ORDER BY r.created_at DESC LIMIT 1`, taskID, tenantID, ownerID).Scan(
		&report.ReportID, &report.TaskID, &report.OutlineVersionID, &report.SourceRunID,
		&payload, &report.ContentHash, &report.Disposition, &report.UnitHashSetHash, &report.CreatedAt,
	)
	if err != nil {
		return runcontrol.FullReviewReport{}, mapNotFound(err)
	}
	report.Payload = append(json.RawMessage(nil), payload...)
	var parsed struct {
		UnitHashes map[string]string `json:"unit_hashes"`
	}
	if err := json.Unmarshal(payload, &parsed); err != nil {
		return runcontrol.FullReviewReport{}, err
	}
	report.UnitHashes = parsed.UnitHashes
	return report, nil
}

func (s *PostgresStore) GetReviewProjection(ctx context.Context, tenantID, ownerID, taskID string) (runcontrol.ReviewView, error) {
	return runcontrol.BuildReviewProjection(ctx, s, tenantID, ownerID, taskID)
}

var _ runcontrol.ReviewProjection = (*PostgresStore)(nil)

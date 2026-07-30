package storage

import (
	"context"
	"strings"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
)

type RepositoryHead struct {
	BindingID     string
	Repository    string
	DefaultBranch string
	Revision      string
	LastSuccessAt time.Time
	LastAttemptAt time.Time
	LastErrorCode string
}

func (s *PostgresStore) RecordRepositoryHead(ctx context.Context, head RepositoryHead) error {
	if strings.TrimSpace(head.BindingID) == "" ||
		strings.TrimSpace(head.Repository) == "" ||
		strings.TrimSpace(head.DefaultBranch) == "" ||
		!repositoryRevisionPattern.MatchString(head.Revision) {
		return runcontrol.ErrInvalidPayload
	}
	if head.LastSuccessAt.IsZero() {
		head.LastSuccessAt = time.Now().UTC()
	}
	if head.LastAttemptAt.IsZero() {
		head.LastAttemptAt = head.LastSuccessAt
	}
	_, err := s.pool.Exec(ctx, `
		INSERT INTO go_repository_heads (
			binding_id, repository, default_branch, head_revision,
			last_success_at, last_attempt_at, last_error_code
		) VALUES ($1,$2,$3,$4,$5,$6,NULL)
		ON CONFLICT (binding_id) DO UPDATE SET
			repository=EXCLUDED.repository,
			default_branch=EXCLUDED.default_branch,
			head_revision=EXCLUDED.head_revision,
			last_success_at=EXCLUDED.last_success_at,
			last_attempt_at=EXCLUDED.last_attempt_at,
			last_error_code=NULL`,
		head.BindingID, head.Repository, head.DefaultBranch, head.Revision,
		head.LastSuccessAt, head.LastAttemptAt,
	)
	return err
}

func (s *PostgresStore) RecordRepositoryRefreshFailure(ctx context.Context, bindingID, errorCode string, attemptedAt time.Time) error {
	if strings.TrimSpace(bindingID) == "" {
		return runcontrol.ErrInvalidPayload
	}
	if attemptedAt.IsZero() {
		attemptedAt = time.Now().UTC()
	}
	_, err := s.pool.Exec(ctx, `
		UPDATE go_repository_heads
		   SET last_attempt_at=$2, last_error_code=NULLIF($3,'')
		 WHERE binding_id=$1`,
		bindingID, attemptedAt, errorCode,
	)
	return err
}

func (s *PostgresStore) GetRepositoryHead(ctx context.Context, bindingID string) (RepositoryHead, error) {
	var head RepositoryHead
	err := s.pool.QueryRow(ctx, `
		SELECT binding_id, repository, default_branch, head_revision,
		       last_success_at, last_attempt_at, COALESCE(last_error_code,'')
		  FROM go_repository_heads
		 WHERE binding_id=$1`,
		bindingID,
	).Scan(
		&head.BindingID, &head.Repository, &head.DefaultBranch, &head.Revision,
		&head.LastSuccessAt, &head.LastAttemptAt, &head.LastErrorCode,
	)
	return head, mapNotFound(err)
}

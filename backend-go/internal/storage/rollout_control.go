package storage

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/jackc/pgx/v5"
)

func (s *PostgresStore) ResolveNewRun(ctx context.Context, request runcontrol.AssignmentRequest) (runcontrol.RolloutAssignment, error) {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{AccessMode: pgx.ReadOnly})
	if err != nil {
		return runcontrol.RolloutAssignment{}, err
	}
	defer tx.Rollback(ctx)
	assignment, err := s.resolveNewRunTx(ctx, tx, request, false)
	if err != nil {
		return runcontrol.RolloutAssignment{}, err
	}
	return assignment, tx.Commit(ctx)
}

func (s *PostgresStore) resolveNewRunTx(ctx context.Context, tx pgx.Tx, request runcontrol.AssignmentRequest, allowBootstrap bool) (runcontrol.RolloutAssignment, error) {
	policy, err := loadActiveRolloutPolicy(ctx, tx)
	if errors.Is(err, pgx.ErrNoRows) && allowBootstrap {
		policy = runcontrol.RolloutPolicy{
			PolicyVersion:   "db-bootstrap:" + string(s.defaultWorkflowVersion),
			DefaultWorkflow: s.defaultWorkflowVersion,
		}
		if s.rolloutPolicy != nil {
			policy = *s.rolloutPolicy
		}
		payload, marshalErr := json.Marshal(policy)
		if marshalErr != nil {
			return runcontrol.RolloutAssignment{}, marshalErr
		}
		digest := sha256.Sum256(payload)
		if _, err = tx.Exec(ctx, `INSERT INTO go_rollout_policies (policy_version,policy_payload,policy_hash,active,created_at) VALUES ($1,$2,$3,TRUE,$4) ON CONFLICT (policy_version) DO UPDATE SET active=TRUE`, policy.PolicyVersion, payload, hex.EncodeToString(digest[:]), time.Now().UTC()); err != nil {
			return runcontrol.RolloutAssignment{}, err
		}
	} else if err != nil {
		return runcontrol.RolloutAssignment{}, mapNotFound(err)
	}
	return policy.Assign(request)
}

func loadActiveRolloutPolicy(ctx context.Context, query rowQuerier) (runcontrol.RolloutPolicy, error) {
	var payload []byte
	err := query.QueryRow(ctx, `SELECT policy_payload FROM go_rollout_policies WHERE active=TRUE`).Scan(&payload)
	if err != nil {
		return runcontrol.RolloutPolicy{}, err
	}
	var policy runcontrol.RolloutPolicy
	if err := json.Unmarshal(payload, &policy); err != nil {
		return runcontrol.RolloutPolicy{}, err
	}
	return policy, nil
}

var _ runcontrol.WorkflowRolloutControl = (*PostgresStore)(nil)

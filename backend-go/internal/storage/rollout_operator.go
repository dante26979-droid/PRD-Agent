package storage

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/jackc/pgx/v5"
)

func (s *PostgresStore) InspectRolloutState(ctx context.Context) (runcontrol.RolloutStageState, error) {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.RolloutStageState{}, err
	}
	defer tx.Rollback(ctx)
	state, err := lockRolloutState(ctx, tx, false)
	if err != nil {
		return runcontrol.RolloutStageState{}, err
	}
	return state, tx.Commit(ctx)
}

func (s *PostgresStore) ApplyRolloutCommand(ctx context.Context, command runcontrol.RolloutOperatorCommand) (runcontrol.RolloutOperatorResult, error) {
	requestHash, err := command.RequestHash()
	if err != nil {
		return runcontrol.RolloutOperatorResult{}, err
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.RolloutOperatorResult{}, err
	}
	defer tx.Rollback(ctx)
	var existingHash string
	var existingPayload []byte
	err = tx.QueryRow(ctx, `SELECT request_hash,result_json FROM go_rollout_commands WHERE command_id=$1`, command.CommandID).Scan(&existingHash, &existingPayload)
	if err == nil {
		if existingHash != requestHash {
			return runcontrol.RolloutOperatorResult{}, runcontrol.ErrInvalidIdempotency
		}
		var existing runcontrol.RolloutOperatorResult
		if err := json.Unmarshal(existingPayload, &existing); err != nil {
			return runcontrol.RolloutOperatorResult{}, err
		}
		existing.Replayed = true
		return existing, tx.Commit(ctx)
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.RolloutOperatorResult{}, err
	}
	current, err := lockRolloutState(ctx, tx, true)
	if err != nil {
		return runcontrol.RolloutOperatorResult{}, err
	}
	if current.Version != command.ExpectedVersion {
		return runcontrol.RolloutOperatorResult{}, runcontrol.ErrTaskVersionConflict
	}
	next := current
	switch command.Kind {
	case runcontrol.RolloutCommandPause:
		if current.Paused {
			return runcontrol.RolloutOperatorResult{}, runcontrol.ErrInvalidRunStatus
		}
		next.Paused, next.PauseReasonCode = true, command.ReasonCode
	case runcontrol.RolloutCommandResume:
		var decision, evidenceHash string
		if err := tx.QueryRow(ctx, `SELECT decision,evidence_hash FROM go_rollout_gate_decisions WHERE decision_id=$1`, command.GateDecisionID).Scan(&decision, &evidenceHash); err != nil {
			return runcontrol.RolloutOperatorResult{}, mapNotFound(err)
		}
		if !current.Paused || decision != "PASSED" || evidenceHash != command.EvidenceHash {
			return runcontrol.RolloutOperatorResult{}, runcontrol.ErrInvalidRunStatus
		}
		next.Paused, next.PauseReasonCode = false, ""
		next.LastGateDecisionID = command.GateDecisionID
	case runcontrol.RolloutCommandRollback:
		var exists bool
		if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM go_rollout_policies WHERE policy_version=$1)`, command.PolicyVersion).Scan(&exists); err != nil {
			return runcontrol.RolloutOperatorResult{}, err
		}
		if !exists {
			return runcontrol.RolloutOperatorResult{}, runcontrol.ErrNotFound
		}
		next.RollbackPolicyVersion = current.PolicyVersion
		next.PolicyVersion = command.PolicyVersion
		next.Paused, next.PauseReasonCode = true, command.ReasonCode
		if command.Apply {
			if _, err := tx.Exec(ctx, `UPDATE go_rollout_policies SET active=FALSE WHERE active=TRUE`); err != nil {
				return runcontrol.RolloutOperatorResult{}, err
			}
			if _, err := tx.Exec(ctx, `UPDATE go_rollout_policies SET active=TRUE WHERE policy_version=$1`, command.PolicyVersion); err != nil {
				return runcontrol.RolloutOperatorResult{}, err
			}
		}
	}
	if command.Now.IsZero() {
		command.Now = time.Now().UTC()
	}
	next.EvidenceHash, next.Version, next.UpdatedAt = command.EvidenceHash, current.Version+1, command.Now
	result := runcontrol.RolloutOperatorResult{CommandID: command.CommandID, RequestHash: requestHash, Applied: command.Apply, State: next}
	if !command.Apply {
		return result, nil
	}
	if _, err := tx.Exec(ctx, `UPDATE go_rollout_stage_state SET policy_version=$1,last_gate_decision_id=NULLIF($2,''),evidence_hash=$3,version=$4,updated_at=$5,paused=$6,pause_reason_code=$7,rollback_policy_version=NULLIF($8,'') WHERE singleton_key='agent-runtime'`, next.PolicyVersion, next.LastGateDecisionID, next.EvidenceHash, next.Version, next.UpdatedAt, next.Paused, next.PauseReasonCode, next.RollbackPolicyVersion); err != nil {
		return runcontrol.RolloutOperatorResult{}, err
	}
	resultPayload, err := json.Marshal(result)
	if err != nil {
		return runcontrol.RolloutOperatorResult{}, err
	}
	if _, err := tx.Exec(ctx, `INSERT INTO go_rollout_commands (command_id,command_kind,request_hash,expected_state_version,result_state_version,evidence_hash,actor_ref,result_json,created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)`, command.CommandID, command.Kind, requestHash, command.ExpectedVersion, next.Version, command.EvidenceHash, command.ActorRef, resultPayload, command.Now); err != nil {
		return runcontrol.RolloutOperatorResult{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.RolloutOperatorResult{}, err
	}
	return result, nil
}

func lockRolloutState(ctx context.Context, tx pgx.Tx, forUpdate bool) (runcontrol.RolloutStageState, error) {
	now := time.Now().UTC()
	if _, err := tx.Exec(ctx, `INSERT INTO go_rollout_stage_state (singleton_key,stage,policy_version,evidence_hash,version,updated_at) SELECT 'agent-runtime','LOCAL_ONLY',COALESCE((SELECT policy_version FROM go_rollout_policies WHERE active=TRUE),''),'',1,$1 ON CONFLICT (singleton_key) DO NOTHING`, now); err != nil {
		return runcontrol.RolloutStageState{}, err
	}
	query := `SELECT stage,policy_version,COALESCE(last_gate_decision_id,''),evidence_hash,version,updated_at,paused,pause_reason_code,COALESCE(rollback_policy_version,'') FROM go_rollout_stage_state WHERE singleton_key='agent-runtime'`
	if forUpdate {
		query += ` FOR UPDATE`
	}
	var state runcontrol.RolloutStageState
	if err := tx.QueryRow(ctx, query).Scan(&state.Stage, &state.PolicyVersion, &state.LastGateDecisionID, &state.EvidenceHash, &state.Version, &state.UpdatedAt, &state.Paused, &state.PauseReasonCode, &state.RollbackPolicyVersion); err != nil {
		return runcontrol.RolloutStageState{}, err
	}
	return state, nil
}

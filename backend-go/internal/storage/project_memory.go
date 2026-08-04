package storage

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/id"
	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/jackc/pgx/v5"
)

func ensureDefaultMemoryAssignmentTx(ctx context.Context, tx pgx.Tx, taskID, runID, tenantID, ownerID string, now time.Time) error {
	spaceID := runcontrol.DefaultMemorySpaceID(tenantID, ownerID)
	if _, err := tx.Exec(ctx, `INSERT INTO go_memory_spaces (space_id,tenant_id,owner_id,project_key,display_name,status,memory_epoch,policy_version,created_at,updated_at) VALUES ($1,$2,$3,'default','Default project memory','ACTIVE',0,$4,$5,$5) ON CONFLICT (tenant_id,owner_id,project_key) DO NOTHING`, spaceID, tenantID, ownerID, runcontrol.ProjectMemoryPolicyV1, now); err != nil {
		return err
	}
	if _, err := tx.Exec(ctx, `INSERT INTO go_task_memory_bindings (task_id,space_id,created_at) VALUES ($1,$2,$3) ON CONFLICT (task_id) DO NOTHING`, taskID, spaceID, now); err != nil {
		return err
	}
	return assignTaskMemoryToRunTx(ctx, tx, taskID, runID, tenantID, ownerID, now)
}

func assignTaskMemoryToRunTx(ctx context.Context, tx pgx.Tx, taskID, runID, tenantID, ownerID string, now time.Time) error {
	var space runcontrol.MemorySpace
	if err := tx.QueryRow(ctx, `SELECT s.space_id,s.tenant_id,s.owner_id,s.project_key,s.display_name,s.status,s.memory_epoch,s.policy_version,s.created_at,s.updated_at FROM go_task_memory_bindings b JOIN go_memory_spaces s ON s.space_id=b.space_id WHERE b.task_id=$1 AND s.tenant_id=$2 AND s.owner_id=$3`, taskID, tenantID, ownerID).Scan(
		&space.SpaceID, &space.TenantID, &space.OwnerID, &space.ProjectKey, &space.DisplayName,
		&space.Status, &space.MemoryEpoch, &space.PolicyVersion, &space.CreatedAt, &space.UpdatedAt,
	); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return ensureDefaultMemoryAssignmentTx(ctx, tx, taskID, runID, tenantID, ownerID, now)
		}
		return err
	}
	assignment := runcontrol.BuildRunMemoryAssignment(runID, space, now)
	_, err := tx.Exec(ctx, `INSERT INTO go_run_memory_assignments (run_id,space_id,memory_watermark,policy_version,access_scope_hash,assignment_hash,created_at) VALUES ($1,$2,$3,$4,$5,$6,$7)`, assignment.RunID, assignment.SpaceID, assignment.MemoryWatermark, assignment.PolicyVersion, assignment.AccessScopeHash, assignment.AssignmentHash, assignment.CreatedAt)
	return err
}

func (s *PostgresStore) CreateMemorySpace(ctx context.Context, command runcontrol.CreateMemorySpaceCommand) (runcontrol.MemorySpace, error) {
	command.ProjectKey = strings.TrimSpace(command.ProjectKey)
	command.DisplayName = strings.TrimSpace(command.DisplayName)
	if command.TenantID == "" || command.OwnerID == "" || command.ProjectKey == "" || command.DisplayName == "" || command.IdempotencyKey == "" || len(command.ProjectKey) > 120 || len(command.DisplayName) > 240 {
		return runcontrol.MemorySpace{}, runcontrol.ErrInvalidPayload
	}
	now := memoryNow(command.Now)
	requestHash := memoryStorageHash(strings.Join([]string{command.TenantID, command.OwnerID, command.ProjectKey, command.DisplayName}, "\x00"))
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.MemorySpace{}, err
	}
	defer tx.Rollback(ctx)
	if resourceID, replay, err := memoryCommandReplay(ctx, tx, command.TenantID, command.OwnerID, "MEMORY_CREATE_SPACE", command.IdempotencyKey, requestHash); err != nil {
		return runcontrol.MemorySpace{}, err
	} else if replay {
		space, err := loadMemorySpace(ctx, tx, command.TenantID, command.OwnerID, resourceID, false)
		return space, finishMemoryReplay(ctx, tx, err)
	}
	spaceID := "memory-space-" + memoryStorageHash(command.TenantID + "\x00" + command.OwnerID + "\x00" + command.ProjectKey)[:24]
	space := runcontrol.MemorySpace{SpaceID: spaceID, TenantID: command.TenantID, OwnerID: command.OwnerID, ProjectKey: command.ProjectKey, DisplayName: command.DisplayName, Status: "ACTIVE", PolicyVersion: runcontrol.ProjectMemoryPolicyV1, CreatedAt: now, UpdatedAt: now}
	if _, err := tx.Exec(ctx, `INSERT INTO go_memory_spaces (space_id,tenant_id,owner_id,project_key,display_name,status,memory_epoch,policy_version,created_at,updated_at) VALUES ($1,$2,$3,$4,$5,$6,0,$7,$8,$8)`, space.SpaceID, space.TenantID, space.OwnerID, space.ProjectKey, space.DisplayName, space.Status, space.PolicyVersion, now); err != nil {
		return runcontrol.MemorySpace{}, err
	}
	if err := insertMemoryCommand(ctx, tx, command.TenantID, command.OwnerID, "MEMORY_CREATE_SPACE", command.IdempotencyKey, requestHash, space.SpaceID, now); err != nil {
		return runcontrol.MemorySpace{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.MemorySpace{}, err
	}
	return space, nil
}

func (s *PostgresStore) ListMemorySpaces(ctx context.Context, tenantID, ownerID string) ([]runcontrol.MemorySpace, error) {
	rows, err := s.pool.Query(ctx, `SELECT space_id,tenant_id,owner_id,project_key,display_name,status,memory_epoch,policy_version,created_at,updated_at FROM go_memory_spaces WHERE tenant_id=$1 AND owner_id=$2 ORDER BY project_key,space_id`, tenantID, ownerID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]runcontrol.MemorySpace, 0)
	for rows.Next() {
		var item runcontrol.MemorySpace
		if err := rows.Scan(&item.SpaceID, &item.TenantID, &item.OwnerID, &item.ProjectKey, &item.DisplayName, &item.Status, &item.MemoryEpoch, &item.PolicyVersion, &item.CreatedAt, &item.UpdatedAt); err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

func (s *PostgresStore) ProposeProjectMemory(ctx context.Context, command runcontrol.ProposeMemoryCommand) (runcontrol.ProjectMemoryCandidate, error) {
	if err := runcontrol.ValidateMemoryCandidate(command); err != nil {
		return runcontrol.ProjectMemoryCandidate{}, err
	}
	requestHash, err := runcontrol.MemoryCandidateRequestHash(command)
	if err != nil {
		return runcontrol.ProjectMemoryCandidate{}, runcontrol.ErrInvalidPayload
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.ProjectMemoryCandidate{}, err
	}
	defer tx.Rollback(ctx)
	if _, err := loadMemorySpace(ctx, tx, command.TenantID, command.OwnerID, command.SpaceID, false); err != nil {
		return runcontrol.ProjectMemoryCandidate{}, err
	}
	if resourceID, replay, err := memoryCommandReplay(ctx, tx, command.TenantID, command.OwnerID, "MEMORY_PROPOSE", command.IdempotencyKey, requestHash); err != nil {
		return runcontrol.ProjectMemoryCandidate{}, err
	} else if replay {
		item, err := loadMemoryCandidate(ctx, tx, command.SpaceID, resourceID, false)
		return item, finishMemoryReplay(ctx, tx, err)
	}
	candidateID, err := id.New("memory-candidate")
	if err != nil {
		return runcontrol.ProjectMemoryCandidate{}, err
	}
	now := memoryNow(command.Now)
	sensitivity := strings.ToUpper(strings.TrimSpace(command.Sensitivity))
	if sensitivity == "" {
		sensitivity = "INTERNAL"
	}
	extractor := strings.TrimSpace(command.ExtractorVersion)
	if extractor == "" {
		extractor = "manual.v1"
	}
	valueJSON, _ := json.Marshal(command.Value)
	sourceJSON := marshalMemorySourceRefs(command.SourceRefs)
	tags := runcontrol.NormalizeMemoryTerms(command.Tags)
	item := runcontrol.ProjectMemoryCandidate{CandidateID: candidateID, SpaceID: command.SpaceID, MemoryType: command.MemoryType, Subject: strings.TrimSpace(command.Subject), Predicate: strings.TrimSpace(command.Predicate), Value: command.Value, Statement: strings.TrimSpace(command.Statement), AuthorityClass: command.AuthorityClass, Tags: tags, Sensitivity: sensitivity, SourceRefs: append([]runcontrol.MemorySourceRef(nil), command.SourceRefs...), ReasonCode: strings.TrimSpace(command.ReasonCode), ProposedByRunID: command.ProposedByRunID, ExtractorVersion: extractor, RequestHash: requestHash, Status: runcontrol.MemoryCandidatePending, CreatedAt: now}
	if _, err := tx.Exec(ctx, `INSERT INTO go_project_memory_candidates (candidate_id,space_id,memory_type,subject,predicate,value_json,statement,authority_class,tags,sensitivity,source_refs_json,reason_code,proposed_by_run_id,extractor_version,request_hash,status,created_at) VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9,$10,$11::jsonb,$12,NULLIF($13,''),$14,$15,$16,$17)`, candidateID, command.SpaceID, command.MemoryType, item.Subject, item.Predicate, string(valueJSON), item.Statement, item.AuthorityClass, item.Tags, item.Sensitivity, string(sourceJSON), item.ReasonCode, item.ProposedByRunID, item.ExtractorVersion, requestHash, item.Status, now); err != nil {
		return runcontrol.ProjectMemoryCandidate{}, err
	}
	if err := insertMemoryCommand(ctx, tx, command.TenantID, command.OwnerID, "MEMORY_PROPOSE", command.IdempotencyKey, requestHash, candidateID, now); err != nil {
		return runcontrol.ProjectMemoryCandidate{}, err
	}
	if err := insertMemoryEvent(ctx, tx, command.SpaceID, "memory.candidate.proposed", candidateID, memoryActor(command.ProposedByRunID), requestHash, now); err != nil {
		return runcontrol.ProjectMemoryCandidate{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.ProjectMemoryCandidate{}, err
	}
	return item, nil
}

func (s *PostgresStore) ListProjectMemoryCandidates(ctx context.Context, tenantID, ownerID, spaceID string, status runcontrol.MemoryCandidateStatus) ([]runcontrol.ProjectMemoryCandidate, error) {
	if _, err := loadMemorySpace(ctx, s.pool, tenantID, ownerID, spaceID, false); err != nil {
		return nil, err
	}
	rows, err := s.pool.Query(ctx, `SELECT candidate_id,space_id,memory_type,subject,predicate,value_json,statement,authority_class,tags,sensitivity,source_refs_json,reason_code,COALESCE(proposed_by_run_id,''),extractor_version,request_hash,status,created_at,reviewed_at,COALESCE(reviewed_by,'') FROM go_project_memory_candidates WHERE space_id=$1 AND ($2='' OR status=$2) ORDER BY created_at,candidate_id`, spaceID, status)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]runcontrol.ProjectMemoryCandidate, 0)
	for rows.Next() {
		item, err := scanMemoryCandidate(rows)
		if err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

func (s *PostgresStore) ConfirmProjectMemoryCandidate(ctx context.Context, command runcontrol.ReviewMemoryCandidateCommand) (runcontrol.ProjectMemoryRecord, error) {
	if command.ExpectedHash == "" || command.ActorRef == "" || command.IdempotencyKey == "" {
		return runcontrol.ProjectMemoryRecord{}, runcontrol.ErrInvalidPayload
	}
	requestHash := memoryStorageHash(command.CandidateID + "\x00" + command.ExpectedHash + "\x00" + command.ActorRef)
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	defer tx.Rollback(ctx)
	space, err := loadMemorySpace(ctx, tx, command.TenantID, command.OwnerID, command.SpaceID, true)
	if err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	if resourceID, replay, err := memoryCommandReplay(ctx, tx, command.TenantID, command.OwnerID, "MEMORY_CONFIRM", command.IdempotencyKey, requestHash); err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	} else if replay {
		record, err := loadMemoryRecord(ctx, tx, command.SpaceID, resourceID)
		return record, finishMemoryReplay(ctx, tx, err)
	}
	candidate, err := loadMemoryCandidate(ctx, tx, command.SpaceID, command.CandidateID, true)
	if err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	if candidate.RequestHash != command.ExpectedHash || candidate.Status != runcontrol.MemoryCandidatePending {
		return runcontrol.ProjectMemoryRecord{}, runcontrol.ErrTaskVersionConflict
	}
	semanticKey := runcontrol.MemorySemanticKey(space.SpaceID, candidate.MemoryType, candidate.Subject, candidate.Predicate)
	valueJSON, _ := json.Marshal(candidate.Value)
	var duplicateID string
	err = tx.QueryRow(ctx, `SELECT r.memory_id FROM go_project_memory_records r JOIN go_project_memory_versions v ON v.memory_id=r.memory_id AND v.version=r.current_version WHERE r.space_id=$1 AND r.semantic_key=$2 AND r.current_status='ACTIVE' AND v.value_json=$3::jsonb AND v.statement=$4 ORDER BY r.memory_id LIMIT 1`, space.SpaceID, semanticKey, string(valueJSON), candidate.Statement).Scan(&duplicateID)
	now := memoryNow(command.Now)
	if err == nil {
		if err := markMemoryCandidateReviewed(ctx, tx, candidate.CandidateID, runcontrol.MemoryCandidateConfirmed, command.ActorRef, now); err != nil {
			return runcontrol.ProjectMemoryRecord{}, err
		}
		if err := insertMemoryCommand(ctx, tx, command.TenantID, command.OwnerID, "MEMORY_CONFIRM", command.IdempotencyKey, requestHash, duplicateID, now); err != nil {
			return runcontrol.ProjectMemoryRecord{}, err
		}
		record, err := loadMemoryRecord(ctx, tx, space.SpaceID, duplicateID)
		return record, finishMemoryReplay(ctx, tx, err)
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	memoryID, err := id.New("memory")
	if err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	if err := tx.QueryRow(ctx, `UPDATE go_memory_spaces SET memory_epoch=memory_epoch+1,updated_at=$2 WHERE space_id=$1 RETURNING memory_epoch`, space.SpaceID, now).Scan(&space.MemoryEpoch); err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	authority := candidate.AuthorityClass
	if authority == runcontrol.MemoryDerivedProposal {
		authority = runcontrol.MemoryUserConfirmed
	}
	version := runcontrol.ProjectMemoryVersion{SchemaVersion: runcontrol.ProjectMemorySchemaVersion, MemoryID: memoryID, SpaceID: space.SpaceID, Version: 1, MemoryType: candidate.MemoryType, Subject: candidate.Subject, Predicate: candidate.Predicate, Value: candidate.Value, Statement: candidate.Statement, AuthorityClass: authority, Status: runcontrol.MemoryActive, Tags: append([]string(nil), candidate.Tags...), Sensitivity: candidate.Sensitivity, SourceRevisionSetHash: runcontrol.MemorySourceSetHash(candidate.SourceRefs), SourceRefs: append([]runcontrol.MemorySourceRef(nil), candidate.SourceRefs...), ValidFrom: now, CommittedEpoch: space.MemoryEpoch, CreatedByKind: "USER_COMMAND", CreatedByRef: command.ActorRef, CreatedAt: now}
	version.ContentHash, err = runcontrol.MemoryVersionContentHash(version)
	if err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	if _, err := tx.Exec(ctx, `INSERT INTO go_project_memory_records (memory_id,space_id,semantic_key,current_version,current_status,created_at,updated_at) VALUES ($1,$2,$3,1,$4,$5,$5)`, memoryID, space.SpaceID, semanticKey, version.Status, now); err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	if err := insertMemoryVersion(ctx, tx, version); err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	if err := markMemoryCandidateReviewed(ctx, tx, candidate.CandidateID, runcontrol.MemoryCandidateConfirmed, command.ActorRef, now); err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	conflictIDs, err := activeSemanticMemoryIDs(ctx, tx, space.SpaceID, semanticKey)
	if err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	if len(conflictIDs) > 1 {
		sort.Strings(conflictIDs)
		conflictID := "memory-conflict-" + memoryStorageHash(strings.Join(conflictIDs, "\x00"))[:24]
		if _, err := tx.Exec(ctx, `INSERT INTO go_project_memory_conflicts (conflict_id,space_id,subject,predicate,memory_ids,status,committed_epoch,created_at) VALUES ($1,$2,$3,$4,$5,'OPEN',$6,$7) ON CONFLICT (conflict_id) DO NOTHING`, conflictID, space.SpaceID, version.Subject, version.Predicate, conflictIDs, space.MemoryEpoch, now); err != nil {
			return runcontrol.ProjectMemoryRecord{}, err
		}
	}
	if err := insertMemoryCommand(ctx, tx, command.TenantID, command.OwnerID, "MEMORY_CONFIRM", command.IdempotencyKey, requestHash, memoryID, now); err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	if err := insertMemoryEvent(ctx, tx, space.SpaceID, "memory.confirmed", memoryID, command.ActorRef, version.ContentHash, now); err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	record := runcontrol.ProjectMemoryRecord{MemoryID: memoryID, SpaceID: space.SpaceID, SemanticKey: semanticKey, CurrentStatus: runcontrol.MemoryActive, Current: version, CreatedAt: now, UpdatedAt: now}
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	return record, nil
}

func (s *PostgresStore) RejectProjectMemoryCandidate(ctx context.Context, command runcontrol.ReviewMemoryCandidateCommand) (runcontrol.ProjectMemoryCandidate, error) {
	if command.ExpectedHash == "" || command.ActorRef == "" || command.IdempotencyKey == "" {
		return runcontrol.ProjectMemoryCandidate{}, runcontrol.ErrInvalidPayload
	}
	requestHash := memoryStorageHash(command.CandidateID + "\x00" + command.ExpectedHash + "\x00" + command.ActorRef)
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.ProjectMemoryCandidate{}, err
	}
	defer tx.Rollback(ctx)
	if _, err := loadMemorySpace(ctx, tx, command.TenantID, command.OwnerID, command.SpaceID, false); err != nil {
		return runcontrol.ProjectMemoryCandidate{}, err
	}
	if resourceID, replay, err := memoryCommandReplay(ctx, tx, command.TenantID, command.OwnerID, "MEMORY_REJECT", command.IdempotencyKey, requestHash); err != nil {
		return runcontrol.ProjectMemoryCandidate{}, err
	} else if replay {
		item, err := loadMemoryCandidate(ctx, tx, command.SpaceID, resourceID, false)
		return item, finishMemoryReplay(ctx, tx, err)
	}
	item, err := loadMemoryCandidate(ctx, tx, command.SpaceID, command.CandidateID, true)
	if err != nil {
		return runcontrol.ProjectMemoryCandidate{}, err
	}
	if item.RequestHash != command.ExpectedHash || item.Status != runcontrol.MemoryCandidatePending {
		return runcontrol.ProjectMemoryCandidate{}, runcontrol.ErrTaskVersionConflict
	}
	now := memoryNow(command.Now)
	if err := markMemoryCandidateReviewed(ctx, tx, item.CandidateID, runcontrol.MemoryCandidateRejected, command.ActorRef, now); err != nil {
		return runcontrol.ProjectMemoryCandidate{}, err
	}
	if err := insertMemoryCommand(ctx, tx, command.TenantID, command.OwnerID, "MEMORY_REJECT", command.IdempotencyKey, requestHash, item.CandidateID, now); err != nil {
		return runcontrol.ProjectMemoryCandidate{}, err
	}
	item.Status, item.ReviewedAt, item.ReviewedBy = runcontrol.MemoryCandidateRejected, &now, command.ActorRef
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.ProjectMemoryCandidate{}, err
	}
	return item, nil
}

func (s *PostgresStore) RevokeProjectMemory(ctx context.Context, command runcontrol.RevokeMemoryCommand) (runcontrol.ProjectMemoryRecord, error) {
	if command.ExpectedVersion < 1 || command.ActorRef == "" || command.IdempotencyKey == "" {
		return runcontrol.ProjectMemoryRecord{}, runcontrol.ErrInvalidPayload
	}
	requestHash := memoryStorageHash(fmt.Sprintf("%s\x00%d\x00%s", command.MemoryID, command.ExpectedVersion, command.ActorRef))
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	defer tx.Rollback(ctx)
	space, err := loadMemorySpace(ctx, tx, command.TenantID, command.OwnerID, command.SpaceID, true)
	if err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	if resourceID, replay, err := memoryCommandReplay(ctx, tx, command.TenantID, command.OwnerID, "MEMORY_REVOKE", command.IdempotencyKey, requestHash); err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	} else if replay {
		record, err := loadMemoryRecord(ctx, tx, command.SpaceID, resourceID)
		return record, finishMemoryReplay(ctx, tx, err)
	}
	record, err := loadMemoryRecordForUpdate(ctx, tx, command.SpaceID, command.MemoryID)
	if err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	if record.Current.Version != command.ExpectedVersion || record.CurrentStatus != runcontrol.MemoryActive {
		return runcontrol.ProjectMemoryRecord{}, runcontrol.ErrTaskVersionConflict
	}
	now := memoryNow(command.Now)
	if err := tx.QueryRow(ctx, `UPDATE go_memory_spaces SET memory_epoch=memory_epoch+1,updated_at=$2 WHERE space_id=$1 RETURNING memory_epoch`, space.SpaceID, now).Scan(&space.MemoryEpoch); err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	version := record.Current
	version.Version++
	version.Status = runcontrol.MemoryRevoked
	version.CommittedEpoch = space.MemoryEpoch
	version.CreatedByKind = "USER_COMMAND"
	version.CreatedByRef = command.ActorRef
	version.CreatedAt = now
	version.ContentHash = ""
	version.ContentHash, err = runcontrol.MemoryVersionContentHash(version)
	if err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	if err := insertMemoryVersion(ctx, tx, version); err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	if _, err := tx.Exec(ctx, `UPDATE go_project_memory_records SET current_version=$2,current_status=$3,updated_at=$4 WHERE memory_id=$1`, record.MemoryID, version.Version, version.Status, now); err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	survivors, err := activeSemanticMemoryIDs(ctx, tx, space.SpaceID, record.SemanticKey)
	if err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	if len(survivors) <= 1 {
		resolutionID := ""
		if len(survivors) == 1 {
			resolutionID = survivors[0]
		}
		if _, err := tx.Exec(ctx, `UPDATE go_project_memory_conflicts SET status='RESOLVED',resolution_memory_id=NULLIF($2,''),resolved_at=$3,resolved_epoch=$4 WHERE space_id=$1 AND status='OPEN' AND $5=ANY(memory_ids)`, space.SpaceID, resolutionID, now, space.MemoryEpoch, record.MemoryID); err != nil {
			return runcontrol.ProjectMemoryRecord{}, err
		}
	}
	if err := insertMemoryCommand(ctx, tx, command.TenantID, command.OwnerID, "MEMORY_REVOKE", command.IdempotencyKey, requestHash, record.MemoryID, now); err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	if err := insertMemoryEvent(ctx, tx, space.SpaceID, "memory.revoked", record.MemoryID, command.ActorRef, version.ContentHash, now); err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	record.CurrentStatus, record.Current, record.UpdatedAt = runcontrol.MemoryRevoked, version, now
	if err := tx.Commit(ctx); err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	return record, nil
}

func (s *PostgresStore) ListProjectMemory(ctx context.Context, tenantID, ownerID, spaceID string, includeInactive bool) ([]runcontrol.ProjectMemoryRecord, error) {
	if _, err := loadMemorySpace(ctx, s.pool, tenantID, ownerID, spaceID, false); err != nil {
		return nil, err
	}
	rows, err := s.pool.Query(ctx, memoryRecordSelect+` WHERE r.space_id=$1 AND ($2 OR r.current_status='ACTIVE') ORDER BY r.memory_id`, spaceID, includeInactive)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]runcontrol.ProjectMemoryRecord, 0)
	for rows.Next() {
		item, err := scanMemoryRecord(rows)
		if err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

func (s *PostgresStore) GetProjectMemoryHistory(ctx context.Context, tenantID, ownerID, spaceID, memoryID string) ([]runcontrol.ProjectMemoryVersion, error) {
	if _, err := loadMemorySpace(ctx, s.pool, tenantID, ownerID, spaceID, false); err != nil {
		return nil, err
	}
	var exists bool
	if err := s.pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM go_project_memory_records WHERE space_id=$1 AND memory_id=$2)`, spaceID, memoryID).Scan(&exists); err != nil {
		return nil, err
	}
	if !exists {
		return nil, runcontrol.ErrNotFound
	}
	rows, err := s.pool.Query(ctx, `SELECT memory_id,version,schema_version,space_id,memory_type,subject,predicate,value_json,statement,authority_class,status,tags,sensitivity,source_revision_set_hash,source_refs_json,valid_from,valid_until,committed_epoch,content_hash,created_by_kind,created_by_ref,created_at FROM go_project_memory_versions WHERE space_id=$1 AND memory_id=$2 ORDER BY version`, spaceID, memoryID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]runcontrol.ProjectMemoryVersion, 0)
	for rows.Next() {
		item, err := scanMemoryVersion(rows)
		if err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

func (s *PostgresStore) ListProjectMemoryConflicts(ctx context.Context, tenantID, ownerID, spaceID string, includeResolved bool) ([]runcontrol.ProjectMemoryConflict, error) {
	if _, err := loadMemorySpace(ctx, s.pool, tenantID, ownerID, spaceID, false); err != nil {
		return nil, err
	}
	rows, err := s.pool.Query(ctx, `SELECT conflict_id,space_id,subject,predicate,memory_ids,status,committed_epoch,COALESCE(resolution_memory_id,''),created_at,resolved_at,resolved_epoch FROM go_project_memory_conflicts WHERE space_id=$1 AND ($2 OR status='OPEN') ORDER BY conflict_id`, spaceID, includeResolved)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]runcontrol.ProjectMemoryConflict, 0)
	for rows.Next() {
		var item runcontrol.ProjectMemoryConflict
		if err := rows.Scan(&item.ConflictID, &item.SpaceID, &item.Subject, &item.Predicate, &item.MemoryIDs, &item.Status, &item.CommittedEpoch, &item.ResolutionMemoryID, &item.CreatedAt, &item.ResolvedAt, &item.ResolvedEpoch); err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

func (s *PostgresStore) SearchProjectMemory(ctx context.Context, request runcontrol.ProjectMemorySearchRequest) (runcontrol.ProjectMemorySearchResult, error) {
	space, err := loadMemorySpace(ctx, s.pool, request.TenantID, request.OwnerID, request.SpaceID, false)
	if err != nil {
		return runcontrol.ProjectMemorySearchResult{}, err
	}
	if request.Watermark < 0 || request.Watermark > space.MemoryEpoch {
		return runcontrol.ProjectMemorySearchResult{}, runcontrol.ErrInvalidPayload
	}
	if request.Limit <= 0 {
		request.Limit = 12
	}
	if request.Limit > runcontrol.MaxMemorySearchLimit {
		request.Limit = runcontrol.MaxMemorySearchLimit
	}
	for _, value := range request.MemoryTypes {
		if !value.Valid() {
			return runcontrol.ProjectMemorySearchResult{}, runcontrol.ErrInvalidPayload
		}
	}
	asOf := memoryNow(request.AsOf)
	types := make([]string, 0, len(request.MemoryTypes))
	for _, item := range request.MemoryTypes {
		types = append(types, string(item))
	}
	query := strings.TrimSpace(request.Query + " " + strings.Join(request.Tags, " "))
	rows, err := s.pool.Query(ctx, `WITH visible AS (
		SELECT DISTINCT ON (memory_id) memory_id,version,schema_version,space_id,memory_type,subject,predicate,value_json,statement,authority_class,status,tags,sensitivity,source_revision_set_hash,source_refs_json,valid_from,valid_until,committed_epoch,content_hash,created_by_kind,created_by_ref,created_at,search_document
		FROM go_project_memory_versions WHERE space_id=$1 AND committed_epoch<=$2 ORDER BY memory_id,committed_epoch DESC
	) SELECT memory_id,version,schema_version,space_id,memory_type,subject,predicate,value_json,statement,authority_class,status,tags,sensitivity,source_revision_set_hash,source_refs_json,valid_from,valid_until,committed_epoch,content_hash,created_by_kind,created_by_ref,created_at
	FROM visible WHERE status='ACTIVE' AND valid_from<=$3 AND (valid_until IS NULL OR valid_until>$3)
	AND (cardinality($4::text[])=0 OR memory_type=ANY($4::text[]))
	AND (cardinality($5::text[])=0 OR tags && $5::text[])
	AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(source_refs_json) ref WHERE COALESCE(ref->>'access_scope_hash','')<>'' AND ref->>'access_scope_hash'<>$6)
	AND ($7='' OR to_tsvector('simple',search_document) @@ plainto_tsquery('simple',$7) OR lower(search_document) LIKE '%'||lower($7)||'%')
	ORDER BY CASE WHEN $7='' THEN 0 ELSE ts_rank_cd(to_tsvector('simple',search_document),plainto_tsquery('simple',$7)) END DESC, memory_id LIMIT $8`, request.SpaceID, request.Watermark, asOf, types, runcontrol.NormalizeMemoryTerms(request.Tags), request.AccessScopeHash, query, request.Limit)
	if err != nil {
		return runcontrol.ProjectMemorySearchResult{}, err
	}
	defer rows.Close()
	records := make([]runcontrol.ProjectMemoryVersion, 0)
	selectedIDs := make([]string, 0)
	identities := make([]string, 0)
	for rows.Next() {
		item, err := scanMemoryVersion(rows)
		if err != nil {
			return runcontrol.ProjectMemorySearchResult{}, err
		}
		records = append(records, item)
		selectedIDs = append(selectedIDs, item.MemoryID)
		identities = append(identities, fmt.Sprintf("%s@%d:%s", item.MemoryID, item.Version, item.ContentHash))
	}
	if err := rows.Err(); err != nil {
		return runcontrol.ProjectMemorySearchResult{}, err
	}
	conflicts, err := loadMemoryConflicts(ctx, s.pool, request.SpaceID, request.Watermark, selectedIDs)
	if err != nil {
		return runcontrol.ProjectMemorySearchResult{}, err
	}
	selected := make(map[string]struct{}, len(selectedIDs))
	for _, memoryID := range selectedIDs {
		selected[memoryID] = struct{}{}
	}
	remove := map[string]struct{}{}
	completeConflicts := conflicts[:0]
	for _, conflict := range conflicts {
		present := 0
		for _, memoryID := range conflict.MemoryIDs {
			if _, ok := selected[memoryID]; ok {
				present++
			}
		}
		if present == len(conflict.MemoryIDs) {
			completeConflicts = append(completeConflicts, conflict)
			continue
		}
		for _, memoryID := range conflict.MemoryIDs {
			if _, ok := selected[memoryID]; ok {
				remove[memoryID] = struct{}{}
			}
		}
	}
	if len(remove) > 0 {
		filtered := records[:0]
		identities = identities[:0]
		for _, item := range records {
			if _, ok := remove[item.MemoryID]; ok {
				continue
			}
			filtered = append(filtered, item)
			identities = append(identities, fmt.Sprintf("%s@%d:%s", item.MemoryID, item.Version, item.ContentHash))
		}
		records = filtered
	}
	return runcontrol.ProjectMemorySearchResult{SpaceID: request.SpaceID, MemoryWatermark: request.Watermark, Records: records, Conflicts: completeConflicts, ExcludedCount: len(remove), SourceSetHash: memoryStorageHash(strings.Join(identities, "\x00"))}, nil
}

const memoryRecordSelect = `SELECT r.memory_id,r.space_id,r.semantic_key,r.current_status,r.created_at,r.updated_at,v.memory_id,v.version,v.schema_version,v.space_id,v.memory_type,v.subject,v.predicate,v.value_json,v.statement,v.authority_class,v.status,v.tags,v.sensitivity,v.source_revision_set_hash,v.source_refs_json,v.valid_from,v.valid_until,v.committed_epoch,v.content_hash,v.created_by_kind,v.created_by_ref,v.created_at FROM go_project_memory_records r JOIN go_project_memory_versions v ON v.memory_id=r.memory_id AND v.version=r.current_version`

func loadMemorySpace(ctx context.Context, query rowQuerier, tenantID, ownerID, spaceID string, forUpdate bool) (runcontrol.MemorySpace, error) {
	suffix := ""
	if forUpdate {
		suffix = " FOR UPDATE"
	}
	var item runcontrol.MemorySpace
	err := query.QueryRow(ctx, `SELECT space_id,tenant_id,owner_id,project_key,display_name,status,memory_epoch,policy_version,created_at,updated_at FROM go_memory_spaces WHERE space_id=$1 AND tenant_id=$2 AND owner_id=$3`+suffix, spaceID, tenantID, ownerID).Scan(&item.SpaceID, &item.TenantID, &item.OwnerID, &item.ProjectKey, &item.DisplayName, &item.Status, &item.MemoryEpoch, &item.PolicyVersion, &item.CreatedAt, &item.UpdatedAt)
	return item, mapNotFound(err)
}

func loadMemoryCandidate(ctx context.Context, query rowQuerier, spaceID, candidateID string, forUpdate bool) (runcontrol.ProjectMemoryCandidate, error) {
	suffix := ""
	if forUpdate {
		suffix = " FOR UPDATE"
	}
	return scanMemoryCandidate(query.QueryRow(ctx, `SELECT candidate_id,space_id,memory_type,subject,predicate,value_json,statement,authority_class,tags,sensitivity,source_refs_json,reason_code,COALESCE(proposed_by_run_id,''),extractor_version,request_hash,status,created_at,reviewed_at,COALESCE(reviewed_by,'') FROM go_project_memory_candidates WHERE candidate_id=$1 AND space_id=$2`+suffix, candidateID, spaceID))
}

func scanMemoryCandidate(row interface{ Scan(...any) error }) (runcontrol.ProjectMemoryCandidate, error) {
	var item runcontrol.ProjectMemoryCandidate
	var valueJSON, sourceJSON []byte
	if err := row.Scan(&item.CandidateID, &item.SpaceID, &item.MemoryType, &item.Subject, &item.Predicate, &valueJSON, &item.Statement, &item.AuthorityClass, &item.Tags, &item.Sensitivity, &sourceJSON, &item.ReasonCode, &item.ProposedByRunID, &item.ExtractorVersion, &item.RequestHash, &item.Status, &item.CreatedAt, &item.ReviewedAt, &item.ReviewedBy); err != nil {
		return item, mapNotFound(err)
	}
	if err := json.Unmarshal(valueJSON, &item.Value); err != nil {
		return item, err
	}
	if err := json.Unmarshal(sourceJSON, &item.SourceRefs); err != nil {
		return item, err
	}
	return item, nil
}

func loadMemoryRecord(ctx context.Context, query rowQuerier, spaceID, memoryID string) (runcontrol.ProjectMemoryRecord, error) {
	return scanMemoryRecord(query.QueryRow(ctx, memoryRecordSelect+` WHERE r.space_id=$1 AND r.memory_id=$2`, spaceID, memoryID))
}

func loadMemoryRecordForUpdate(ctx context.Context, tx pgx.Tx, spaceID, memoryID string) (runcontrol.ProjectMemoryRecord, error) {
	if _, err := tx.Exec(ctx, `SELECT 1 FROM go_project_memory_records WHERE space_id=$1 AND memory_id=$2 FOR UPDATE`, spaceID, memoryID); err != nil {
		return runcontrol.ProjectMemoryRecord{}, err
	}
	return loadMemoryRecord(ctx, tx, spaceID, memoryID)
}

func scanMemoryRecord(row interface{ Scan(...any) error }) (runcontrol.ProjectMemoryRecord, error) {
	var record runcontrol.ProjectMemoryRecord
	var version runcontrol.ProjectMemoryVersion
	var valueJSON, sourceJSON []byte
	err := row.Scan(&record.MemoryID, &record.SpaceID, &record.SemanticKey, &record.CurrentStatus, &record.CreatedAt, &record.UpdatedAt, &version.MemoryID, &version.Version, &version.SchemaVersion, &version.SpaceID, &version.MemoryType, &version.Subject, &version.Predicate, &valueJSON, &version.Statement, &version.AuthorityClass, &version.Status, &version.Tags, &version.Sensitivity, &version.SourceRevisionSetHash, &sourceJSON, &version.ValidFrom, &version.ValidUntil, &version.CommittedEpoch, &version.ContentHash, &version.CreatedByKind, &version.CreatedByRef, &version.CreatedAt)
	if err != nil {
		return record, mapNotFound(err)
	}
	if err := json.Unmarshal(valueJSON, &version.Value); err != nil {
		return record, err
	}
	if err := json.Unmarshal(sourceJSON, &version.SourceRefs); err != nil {
		return record, err
	}
	record.Current = version
	return record, nil
}

func scanMemoryVersion(row interface{ Scan(...any) error }) (runcontrol.ProjectMemoryVersion, error) {
	var item runcontrol.ProjectMemoryVersion
	var valueJSON, sourceJSON []byte
	err := row.Scan(&item.MemoryID, &item.Version, &item.SchemaVersion, &item.SpaceID, &item.MemoryType, &item.Subject, &item.Predicate, &valueJSON, &item.Statement, &item.AuthorityClass, &item.Status, &item.Tags, &item.Sensitivity, &item.SourceRevisionSetHash, &sourceJSON, &item.ValidFrom, &item.ValidUntil, &item.CommittedEpoch, &item.ContentHash, &item.CreatedByKind, &item.CreatedByRef, &item.CreatedAt)
	if err != nil {
		return item, err
	}
	if err := json.Unmarshal(valueJSON, &item.Value); err != nil {
		return item, err
	}
	if err := json.Unmarshal(sourceJSON, &item.SourceRefs); err != nil {
		return item, err
	}
	return item, nil
}

func insertMemoryVersion(ctx context.Context, tx pgx.Tx, value runcontrol.ProjectMemoryVersion) error {
	valueJSON, _ := json.Marshal(value.Value)
	sourceJSON := marshalMemorySourceRefs(value.SourceRefs)
	searchDocument := strings.Join(append([]string{value.Subject, value.Predicate, value.Statement}, value.Tags...), " ")
	_, err := tx.Exec(ctx, `INSERT INTO go_project_memory_versions (memory_id,version,schema_version,space_id,memory_type,subject,predicate,value_json,statement,authority_class,status,tags,sensitivity,source_revision_set_hash,source_refs_json,valid_from,valid_until,committed_epoch,content_hash,created_by_kind,created_by_ref,search_document,created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12,$13,$14,$15::jsonb,$16,$17,$18,$19,$20,$21,$22,$23)`, value.MemoryID, value.Version, value.SchemaVersion, value.SpaceID, value.MemoryType, value.Subject, value.Predicate, string(valueJSON), value.Statement, value.AuthorityClass, value.Status, value.Tags, value.Sensitivity, value.SourceRevisionSetHash, string(sourceJSON), value.ValidFrom, value.ValidUntil, value.CommittedEpoch, value.ContentHash, value.CreatedByKind, value.CreatedByRef, searchDocument, value.CreatedAt)
	return err
}

func activeSemanticMemoryIDs(ctx context.Context, tx pgx.Tx, spaceID, semanticKey string) ([]string, error) {
	rows, err := tx.Query(ctx, `SELECT memory_id FROM go_project_memory_records WHERE space_id=$1 AND semantic_key=$2 AND current_status='ACTIVE' ORDER BY memory_id`, spaceID, semanticKey)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var value string
		if err := rows.Scan(&value); err != nil {
			return nil, err
		}
		ids = append(ids, value)
	}
	return ids, rows.Err()
}

func loadMemoryConflicts(ctx context.Context, query interface {
	Query(context.Context, string, ...any) (pgx.Rows, error)
}, spaceID string, watermark int64, selectedIDs []string) ([]runcontrol.ProjectMemoryConflict, error) {
	if len(selectedIDs) == 0 {
		return []runcontrol.ProjectMemoryConflict{}, nil
	}
	rows, err := query.Query(ctx, `SELECT conflict_id,space_id,subject,predicate,memory_ids,CASE WHEN resolved_epoch>$2 THEN 'OPEN' ELSE status END,committed_epoch,CASE WHEN resolved_epoch>$2 THEN '' ELSE COALESCE(resolution_memory_id,'') END,created_at,CASE WHEN resolved_epoch>$2 THEN NULL ELSE resolved_at END,CASE WHEN resolved_epoch>$2 THEN NULL ELSE resolved_epoch END FROM go_project_memory_conflicts WHERE space_id=$1 AND committed_epoch<=$2 AND (resolved_epoch IS NULL OR resolved_epoch>$2) AND memory_ids && $3::text[] ORDER BY conflict_id`, spaceID, watermark, selectedIDs)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]runcontrol.ProjectMemoryConflict, 0)
	for rows.Next() {
		var item runcontrol.ProjectMemoryConflict
		if err := rows.Scan(&item.ConflictID, &item.SpaceID, &item.Subject, &item.Predicate, &item.MemoryIDs, &item.Status, &item.CommittedEpoch, &item.ResolutionMemoryID, &item.CreatedAt, &item.ResolvedAt, &item.ResolvedEpoch); err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

func markMemoryCandidateReviewed(ctx context.Context, tx pgx.Tx, candidateID string, status runcontrol.MemoryCandidateStatus, actor string, now time.Time) error {
	_, err := tx.Exec(ctx, `UPDATE go_project_memory_candidates SET status=$2,reviewed_at=$3,reviewed_by=$4 WHERE candidate_id=$1`, candidateID, status, now, actor)
	return err
}

func memoryCommandReplay(ctx context.Context, query rowQuerier, tenantID, ownerID, operation, key, requestHash string) (string, bool, error) {
	var resourceID, existingHash string
	err := query.QueryRow(ctx, `SELECT resource_id,request_hash FROM go_command_idempotency WHERE tenant_id=$1 AND owner_id=$2 AND operation=$3 AND idempotency_key=$4`, tenantID, ownerID, operation, key).Scan(&resourceID, &existingHash)
	if err == nil {
		if existingHash != requestHash {
			return "", false, runcontrol.ErrInvalidIdempotency
		}
		return resourceID, true, nil
	}
	if errors.Is(err, pgx.ErrNoRows) {
		return "", false, nil
	}
	return "", false, err
}

func insertMemoryCommand(ctx context.Context, tx pgx.Tx, tenantID, ownerID, operation, key, requestHash, resourceID string, now time.Time) error {
	_, err := tx.Exec(ctx, `INSERT INTO go_command_idempotency (tenant_id,owner_id,operation,idempotency_key,request_hash,resource_id,created_at) VALUES ($1,$2,$3,$4,$5,$6,$7)`, tenantID, ownerID, operation, key, requestHash, resourceID, now)
	return err
}

func insertMemoryEvent(ctx context.Context, tx pgx.Tx, spaceID, eventType, resourceID, actor, payloadHash string, now time.Time) error {
	eventID, err := id.New("memory-event")
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `INSERT INTO go_project_memory_events (event_id,space_id,event_type,resource_id,actor_ref,payload_hash,occurred_at) VALUES ($1,$2,$3,$4,$5,$6,$7)`, eventID, spaceID, eventType, resourceID, actor, payloadHash, now)
	return err
}

func finishMemoryReplay(ctx context.Context, tx pgx.Tx, err error) error {
	if err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func memoryNow(value time.Time) time.Time {
	if value.IsZero() {
		return time.Now().UTC()
	}
	return value.UTC()
}

func memoryStorageHash(value string) string {
	digest := sha256.Sum256([]byte(value))
	return hex.EncodeToString(digest[:])
}

func memoryActor(runID string) string {
	if runID == "" {
		return "user:manual"
	}
	return "agent-run:" + runID
}

func marshalMemorySourceRefs(refs []runcontrol.MemorySourceRef) []byte {
	if refs == nil {
		return []byte("[]")
	}
	raw, _ := json.Marshal(refs)
	return raw
}

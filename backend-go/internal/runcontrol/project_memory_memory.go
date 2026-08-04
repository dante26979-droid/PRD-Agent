package runcontrol

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/id"
)

func (s *MemoryStore) ensureDefaultMemorySpaceLocked(tenantID, ownerID string, now time.Time) MemorySpace {
	spaceID := DefaultMemorySpaceID(tenantID, ownerID)
	if existing, ok := s.memorySpaces[spaceID]; ok {
		return existing
	}
	space := MemorySpace{
		SpaceID: spaceID, TenantID: tenantID, OwnerID: ownerID,
		ProjectKey: "default", DisplayName: "Default project memory",
		Status: "ACTIVE", PolicyVersion: ProjectMemoryPolicyV1,
		CreatedAt: now, UpdatedAt: now,
	}
	s.memorySpaces[spaceID] = space
	return space
}

func buildRunMemoryAssignment(runID string, space MemorySpace, now time.Time) RunMemoryAssignment {
	return BuildRunMemoryAssignment(runID, space, now)
}

func (s *MemoryStore) CreateMemorySpace(_ context.Context, command CreateMemorySpaceCommand) (MemorySpace, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	command.ProjectKey = strings.TrimSpace(command.ProjectKey)
	command.DisplayName = strings.TrimSpace(command.DisplayName)
	if command.TenantID == "" || command.OwnerID == "" || command.ProjectKey == "" ||
		command.DisplayName == "" || command.IdempotencyKey == "" || len(command.ProjectKey) > 120 || len(command.DisplayName) > 240 {
		return MemorySpace{}, ErrInvalidPayload
	}
	now := command.Now.UTC()
	if now.IsZero() {
		now = time.Now().UTC()
	}
	requestHash := stableHash(strings.Join([]string{command.TenantID, command.OwnerID, command.ProjectKey, command.DisplayName}, "\x00"))
	commandKey := memoryCommandKey(command.TenantID, command.OwnerID, "CREATE_SPACE", command.IdempotencyKey)
	if resourceID, ok := s.projectMemoryCommandResources[commandKey]; ok {
		if s.projectMemoryCommandHashes[commandKey] != requestHash {
			return MemorySpace{}, ErrInvalidIdempotency
		}
		return s.memorySpaces[resourceID], nil
	}
	for _, existing := range s.memorySpaces {
		if existing.TenantID == command.TenantID && existing.OwnerID == command.OwnerID && existing.ProjectKey == command.ProjectKey {
			return MemorySpace{}, fmt.Errorf("%w: project memory space already exists", ErrInvalidPayload)
		}
	}
	spaceID := "memory-space-" + stableHash(command.TenantID + "\x00" + command.OwnerID + "\x00" + command.ProjectKey)[:24]
	space := MemorySpace{SpaceID: spaceID, TenantID: command.TenantID, OwnerID: command.OwnerID,
		ProjectKey: command.ProjectKey, DisplayName: command.DisplayName, Status: "ACTIVE",
		PolicyVersion: ProjectMemoryPolicyV1, CreatedAt: now, UpdatedAt: now}
	s.memorySpaces[spaceID] = space
	s.projectMemoryCommandHashes[commandKey] = requestHash
	s.projectMemoryCommandResources[commandKey] = spaceID
	return space, nil
}

func (s *MemoryStore) ListMemorySpaces(_ context.Context, tenantID, ownerID string) ([]MemorySpace, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	items := make([]MemorySpace, 0)
	for _, item := range s.memorySpaces {
		if item.TenantID == tenantID && item.OwnerID == ownerID {
			items = append(items, item)
		}
	}
	sort.Slice(items, func(i, j int) bool { return items[i].SpaceID < items[j].SpaceID })
	return items, nil
}

func (s *MemoryStore) ProposeProjectMemory(_ context.Context, command ProposeMemoryCommand) (ProjectMemoryCandidate, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	space, ok := s.memorySpaces[command.SpaceID]
	if !ok || space.TenantID != command.TenantID || space.OwnerID != command.OwnerID {
		return ProjectMemoryCandidate{}, ErrNotFound
	}
	if err := ValidateMemoryCandidate(command); err != nil {
		return ProjectMemoryCandidate{}, err
	}
	requestHash, err := MemoryCandidateRequestHash(command)
	if err != nil {
		return ProjectMemoryCandidate{}, ErrInvalidPayload
	}
	commandKey := memoryCommandKey(command.TenantID, command.OwnerID, "PROPOSE", command.IdempotencyKey)
	if resourceID, ok := s.projectMemoryCommandResources[commandKey]; ok {
		if s.projectMemoryCommandHashes[commandKey] != requestHash {
			return ProjectMemoryCandidate{}, ErrInvalidIdempotency
		}
		return s.projectMemoryCandidates[resourceID], nil
	}
	candidateID, err := id.New("memory-candidate")
	if err != nil {
		return ProjectMemoryCandidate{}, err
	}
	now := command.Now.UTC()
	if now.IsZero() {
		now = time.Now().UTC()
	}
	sensitivity := strings.ToUpper(strings.TrimSpace(command.Sensitivity))
	if sensitivity == "" {
		sensitivity = "INTERNAL"
	}
	extractor := strings.TrimSpace(command.ExtractorVersion)
	if extractor == "" {
		extractor = "manual.v1"
	}
	candidate := ProjectMemoryCandidate{
		CandidateID: candidateID, SpaceID: command.SpaceID, MemoryType: command.MemoryType,
		Subject: strings.TrimSpace(command.Subject), Predicate: strings.TrimSpace(command.Predicate),
		Value: command.Value, Statement: strings.TrimSpace(command.Statement), AuthorityClass: command.AuthorityClass,
		Tags: normalizedMemoryTerms(command.Tags), Sensitivity: sensitivity, SourceRefs: append([]MemorySourceRef(nil), command.SourceRefs...),
		ReasonCode: strings.TrimSpace(command.ReasonCode), ProposedByRunID: command.ProposedByRunID,
		ExtractorVersion: extractor, RequestHash: requestHash, Status: MemoryCandidatePending, CreatedAt: now,
	}
	s.projectMemoryCandidates[candidateID] = candidate
	s.projectMemoryCommandHashes[commandKey] = requestHash
	s.projectMemoryCommandResources[commandKey] = candidateID
	return candidate, nil
}

func (s *MemoryStore) ListProjectMemoryCandidates(_ context.Context, tenantID, ownerID, spaceID string, status MemoryCandidateStatus) ([]ProjectMemoryCandidate, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	space, ok := s.memorySpaces[spaceID]
	if !ok || space.TenantID != tenantID || space.OwnerID != ownerID {
		return nil, ErrNotFound
	}
	items := make([]ProjectMemoryCandidate, 0)
	for _, item := range s.projectMemoryCandidates {
		if item.SpaceID == spaceID && (status == "" || item.Status == status) {
			items = append(items, item)
		}
	}
	sort.Slice(items, func(i, j int) bool {
		if items[i].CreatedAt.Equal(items[j].CreatedAt) {
			return items[i].CandidateID < items[j].CandidateID
		}
		return items[i].CreatedAt.Before(items[j].CreatedAt)
	})
	return items, nil
}

func (s *MemoryStore) ConfirmProjectMemoryCandidate(_ context.Context, command ReviewMemoryCandidateCommand) (ProjectMemoryRecord, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	space, candidate, err := s.reviewCandidateLocked(command, "CONFIRM")
	if err != nil {
		return ProjectMemoryRecord{}, err
	}
	commandKey := memoryCommandKey(command.TenantID, command.OwnerID, "CONFIRM", command.IdempotencyKey)
	requestHash := stableHash(command.CandidateID + "\x00" + command.ExpectedHash + "\x00" + command.ActorRef)
	if resourceID, ok := s.projectMemoryCommandResources[commandKey]; ok {
		if s.projectMemoryCommandHashes[commandKey] != requestHash {
			return ProjectMemoryRecord{}, ErrInvalidIdempotency
		}
		return s.projectMemoryRecords[resourceID], nil
	}
	if candidate.Status != MemoryCandidatePending {
		return ProjectMemoryRecord{}, fmt.Errorf("%w: memory candidate is not pending", ErrInvalidPayload)
	}
	now := command.Now.UTC()
	if now.IsZero() {
		now = time.Now().UTC()
	}
	authority := candidate.AuthorityClass
	if authority == MemoryDerivedProposal {
		authority = MemoryUserConfirmed
	}
	semanticKey := MemorySemanticKey(space.SpaceID, candidate.MemoryType, candidate.Subject, candidate.Predicate)
	for _, existing := range s.projectMemoryRecords {
		if existing.SpaceID != space.SpaceID || existing.SemanticKey != semanticKey || existing.CurrentStatus != MemoryActive {
			continue
		}
		if memoryJSONEqual(existing.Current.Value, candidate.Value) && existing.Current.Statement == candidate.Statement {
			candidate.Status = MemoryCandidateConfirmed
			candidate.ReviewedAt, candidate.ReviewedBy = &now, command.ActorRef
			s.projectMemoryCandidates[candidate.CandidateID] = candidate
			s.projectMemoryCommandHashes[commandKey] = requestHash
			s.projectMemoryCommandResources[commandKey] = existing.MemoryID
			return existing, nil
		}
	}
	memoryID, err := id.New("memory")
	if err != nil {
		return ProjectMemoryRecord{}, err
	}
	space.MemoryEpoch++
	space.UpdatedAt = now
	s.memorySpaces[space.SpaceID] = space
	version := ProjectMemoryVersion{
		SchemaVersion: ProjectMemorySchemaVersion, MemoryID: memoryID, SpaceID: space.SpaceID,
		Version: 1, MemoryType: candidate.MemoryType, Subject: candidate.Subject, Predicate: candidate.Predicate,
		Value: candidate.Value, Statement: candidate.Statement, AuthorityClass: authority, Status: MemoryActive,
		Tags: append([]string(nil), candidate.Tags...), Sensitivity: candidate.Sensitivity,
		SourceRevisionSetHash: memorySourceSetHash(candidate.SourceRefs), SourceRefs: append([]MemorySourceRef(nil), candidate.SourceRefs...),
		ValidFrom: now, CommittedEpoch: space.MemoryEpoch, CreatedByKind: "USER_COMMAND", CreatedByRef: command.ActorRef, CreatedAt: now,
	}
	version.ContentHash, err = memoryVersionContentHash(version)
	if err != nil {
		return ProjectMemoryRecord{}, err
	}
	record := ProjectMemoryRecord{MemoryID: memoryID, SpaceID: space.SpaceID, SemanticKey: semanticKey,
		CurrentStatus: MemoryActive, Current: version, CreatedAt: now, UpdatedAt: now}
	s.projectMemoryRecords[memoryID] = record
	s.projectMemoryVersions[memoryID] = []ProjectMemoryVersion{version}
	candidate.Status = MemoryCandidateConfirmed
	candidate.ReviewedAt, candidate.ReviewedBy = &now, command.ActorRef
	s.projectMemoryCandidates[candidate.CandidateID] = candidate
	conflicting := []string{memoryID}
	for _, existing := range s.projectMemoryRecords {
		if existing.MemoryID != memoryID && existing.SpaceID == space.SpaceID && existing.SemanticKey == semanticKey &&
			existing.CurrentStatus == MemoryActive && !memoryJSONEqual(existing.Current.Value, version.Value) {
			conflicting = append(conflicting, existing.MemoryID)
		}
	}
	if len(conflicting) > 1 {
		sort.Strings(conflicting)
		conflictID := "memory-conflict-" + stableHash(strings.Join(conflicting, "\x00"))[:24]
		s.projectMemoryConflicts[conflictID] = ProjectMemoryConflict{ConflictID: conflictID, SpaceID: space.SpaceID,
			Subject: version.Subject, Predicate: version.Predicate, MemoryIDs: conflicting, Status: "OPEN", CommittedEpoch: space.MemoryEpoch, CreatedAt: now}
	}
	s.projectMemoryCommandHashes[commandKey] = requestHash
	s.projectMemoryCommandResources[commandKey] = memoryID
	return record, nil
}

func (s *MemoryStore) RejectProjectMemoryCandidate(_ context.Context, command ReviewMemoryCandidateCommand) (ProjectMemoryCandidate, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, candidate, err := s.reviewCandidateLocked(command, "REJECT")
	if err != nil {
		return ProjectMemoryCandidate{}, err
	}
	commandKey := memoryCommandKey(command.TenantID, command.OwnerID, "REJECT", command.IdempotencyKey)
	requestHash := stableHash(command.CandidateID + "\x00" + command.ExpectedHash + "\x00" + command.ActorRef)
	if resourceID, ok := s.projectMemoryCommandResources[commandKey]; ok {
		if s.projectMemoryCommandHashes[commandKey] != requestHash {
			return ProjectMemoryCandidate{}, ErrInvalidIdempotency
		}
		return s.projectMemoryCandidates[resourceID], nil
	}
	if candidate.Status != MemoryCandidatePending {
		return ProjectMemoryCandidate{}, fmt.Errorf("%w: memory candidate is not pending", ErrInvalidPayload)
	}
	now := command.Now.UTC()
	if now.IsZero() {
		now = time.Now().UTC()
	}
	candidate.Status, candidate.ReviewedAt, candidate.ReviewedBy = MemoryCandidateRejected, &now, command.ActorRef
	s.projectMemoryCandidates[candidate.CandidateID] = candidate
	s.projectMemoryCommandHashes[commandKey] = requestHash
	s.projectMemoryCommandResources[commandKey] = candidate.CandidateID
	return candidate, nil
}

func (s *MemoryStore) reviewCandidateLocked(command ReviewMemoryCandidateCommand, _ string) (MemorySpace, ProjectMemoryCandidate, error) {
	space, ok := s.memorySpaces[command.SpaceID]
	if !ok || space.TenantID != command.TenantID || space.OwnerID != command.OwnerID {
		return MemorySpace{}, ProjectMemoryCandidate{}, ErrNotFound
	}
	candidate, ok := s.projectMemoryCandidates[command.CandidateID]
	if !ok || candidate.SpaceID != command.SpaceID {
		return MemorySpace{}, ProjectMemoryCandidate{}, ErrNotFound
	}
	if command.ExpectedHash == "" || candidate.RequestHash != command.ExpectedHash || command.ActorRef == "" || command.IdempotencyKey == "" {
		return MemorySpace{}, ProjectMemoryCandidate{}, ErrInvalidPayload
	}
	return space, candidate, nil
}

func (s *MemoryStore) RevokeProjectMemory(_ context.Context, command RevokeMemoryCommand) (ProjectMemoryRecord, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	space, ok := s.memorySpaces[command.SpaceID]
	if !ok || space.TenantID != command.TenantID || space.OwnerID != command.OwnerID {
		return ProjectMemoryRecord{}, ErrNotFound
	}
	record, ok := s.projectMemoryRecords[command.MemoryID]
	if !ok || record.SpaceID != command.SpaceID {
		return ProjectMemoryRecord{}, ErrNotFound
	}
	requestHash := stableHash(fmt.Sprintf("%s\x00%d\x00%s", command.MemoryID, command.ExpectedVersion, command.ActorRef))
	commandKey := memoryCommandKey(command.TenantID, command.OwnerID, "REVOKE", command.IdempotencyKey)
	if resourceID, ok := s.projectMemoryCommandResources[commandKey]; ok {
		if s.projectMemoryCommandHashes[commandKey] != requestHash {
			return ProjectMemoryRecord{}, ErrInvalidIdempotency
		}
		return s.projectMemoryRecords[resourceID], nil
	}
	if command.ExpectedVersion < 1 || record.Current.Version != command.ExpectedVersion || record.CurrentStatus != MemoryActive || command.ActorRef == "" || command.IdempotencyKey == "" {
		return ProjectMemoryRecord{}, ErrTaskVersionConflict
	}
	now := command.Now.UTC()
	if now.IsZero() {
		now = time.Now().UTC()
	}
	space.MemoryEpoch++
	space.UpdatedAt = now
	s.memorySpaces[space.SpaceID] = space
	version := record.Current
	version.Version++
	version.Status = MemoryRevoked
	version.CommittedEpoch = space.MemoryEpoch
	version.CreatedByKind = "USER_COMMAND"
	version.CreatedByRef = command.ActorRef
	version.CreatedAt = now
	version.ContentHash = ""
	var err error
	version.ContentHash, err = memoryVersionContentHash(version)
	if err != nil {
		return ProjectMemoryRecord{}, err
	}
	record.CurrentStatus, record.Current, record.UpdatedAt = MemoryRevoked, version, now
	s.projectMemoryRecords[record.MemoryID] = record
	s.projectMemoryVersions[record.MemoryID] = append(s.projectMemoryVersions[record.MemoryID], version)
	for conflictID, conflict := range s.projectMemoryConflicts {
		contains := false
		active := make([]string, 0, len(conflict.MemoryIDs))
		for _, memoryID := range conflict.MemoryIDs {
			if memoryID == record.MemoryID {
				contains = true
			}
			if current, ok := s.projectMemoryRecords[memoryID]; ok && current.CurrentStatus == MemoryActive {
				active = append(active, memoryID)
			}
		}
		if contains && conflict.Status == "OPEN" && len(active) <= 1 {
			conflict.Status = "RESOLVED"
			conflict.ResolvedAt = &now
			resolvedEpoch := space.MemoryEpoch
			conflict.ResolvedEpoch = &resolvedEpoch
			if len(active) == 1 {
				conflict.ResolutionMemoryID = active[0]
			}
			s.projectMemoryConflicts[conflictID] = conflict
		}
	}
	s.projectMemoryCommandHashes[commandKey] = requestHash
	s.projectMemoryCommandResources[commandKey] = record.MemoryID
	return record, nil
}

func (s *MemoryStore) ListProjectMemory(_ context.Context, tenantID, ownerID, spaceID string, includeInactive bool) ([]ProjectMemoryRecord, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	space, ok := s.memorySpaces[spaceID]
	if !ok || space.TenantID != tenantID || space.OwnerID != ownerID {
		return nil, ErrNotFound
	}
	items := make([]ProjectMemoryRecord, 0)
	for _, item := range s.projectMemoryRecords {
		if item.SpaceID == spaceID && (includeInactive || item.CurrentStatus == MemoryActive) {
			items = append(items, item)
		}
	}
	sort.Slice(items, func(i, j int) bool { return items[i].MemoryID < items[j].MemoryID })
	return items, nil
}

func (s *MemoryStore) GetProjectMemoryHistory(_ context.Context, tenantID, ownerID, spaceID, memoryID string) ([]ProjectMemoryVersion, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	space, ok := s.memorySpaces[spaceID]
	if !ok || space.TenantID != tenantID || space.OwnerID != ownerID {
		return nil, ErrNotFound
	}
	record, ok := s.projectMemoryRecords[memoryID]
	if !ok || record.SpaceID != spaceID {
		return nil, ErrNotFound
	}
	items := append([]ProjectMemoryVersion(nil), s.projectMemoryVersions[memoryID]...)
	sort.Slice(items, func(i, j int) bool { return items[i].Version < items[j].Version })
	return items, nil
}

func (s *MemoryStore) ListProjectMemoryConflicts(_ context.Context, tenantID, ownerID, spaceID string, includeResolved bool) ([]ProjectMemoryConflict, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	space, ok := s.memorySpaces[spaceID]
	if !ok || space.TenantID != tenantID || space.OwnerID != ownerID {
		return nil, ErrNotFound
	}
	items := make([]ProjectMemoryConflict, 0)
	for _, item := range s.projectMemoryConflicts {
		if item.SpaceID == spaceID && (includeResolved || item.Status == "OPEN") {
			items = append(items, item)
		}
	}
	sort.Slice(items, func(i, j int) bool { return items[i].ConflictID < items[j].ConflictID })
	return items, nil
}

func (s *MemoryStore) SearchProjectMemory(_ context.Context, request ProjectMemorySearchRequest) (ProjectMemorySearchResult, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	space, ok := s.memorySpaces[request.SpaceID]
	if !ok || space.TenantID != request.TenantID || space.OwnerID != request.OwnerID {
		return ProjectMemorySearchResult{}, ErrNotFound
	}
	if request.Watermark < 0 || request.Watermark > space.MemoryEpoch {
		return ProjectMemorySearchResult{}, ErrInvalidPayload
	}
	if request.Limit <= 0 {
		request.Limit = 12
	}
	if request.Limit > MaxMemorySearchLimit {
		request.Limit = MaxMemorySearchLimit
	}
	asOf := request.AsOf.UTC()
	if asOf.IsZero() {
		asOf = time.Now().UTC()
	}
	allowedTypes := map[ProjectMemoryType]struct{}{}
	for _, item := range request.MemoryTypes {
		if !item.Valid() {
			return ProjectMemorySearchResult{}, ErrInvalidPayload
		}
		allowedTypes[item] = struct{}{}
	}
	queryTerms := normalizedMemoryTerms(append(strings.Fields(request.Query), request.Tags...))
	type ranked struct {
		value ProjectMemoryVersion
		score int
	}
	selected := make([]ranked, 0)
	excluded := 0
	for memoryID, versions := range s.projectMemoryVersions {
		record := s.projectMemoryRecords[memoryID]
		if record.SpaceID != request.SpaceID {
			continue
		}
		var visible *ProjectMemoryVersion
		for index := range versions {
			candidate := versions[index]
			if candidate.CommittedEpoch <= request.Watermark && (visible == nil || candidate.CommittedEpoch > visible.CommittedEpoch) {
				copy := candidate
				visible = &copy
			}
		}
		if visible == nil || visible.Status != MemoryActive || visible.ValidFrom.After(asOf) || (visible.ValidUntil != nil && !visible.ValidUntil.After(asOf)) {
			excluded++
			continue
		}
		if len(allowedTypes) > 0 {
			if _, ok := allowedTypes[visible.MemoryType]; !ok {
				excluded++
				continue
			}
		}
		accessAllowed := true
		for _, ref := range visible.SourceRefs {
			if ref.AccessScopeHash != "" && request.AccessScopeHash != ref.AccessScopeHash {
				accessAllowed = false
				break
			}
		}
		if !accessAllowed {
			excluded++
			continue
		}
		haystack := strings.ToLower(strings.Join(append([]string{visible.Subject, visible.Predicate, visible.Statement}, visible.Tags...), " "))
		score := 0
		for _, term := range queryTerms {
			if strings.EqualFold(term, visible.Subject) {
				score += 100
			} else if strings.Contains(strings.ToLower(visible.Subject), term) {
				score += 40
			} else if strings.Contains(haystack, term) {
				score += 20
			}
		}
		if len(queryTerms) > 0 && score == 0 {
			continue
		}
		if visible.AuthorityClass == MemoryUserConfirmed || visible.AuthorityClass == MemoryWorkflowConfirmed {
			score += 10
		}
		selected = append(selected, ranked{value: *visible, score: score})
	}
	sort.Slice(selected, func(i, j int) bool {
		if selected[i].score == selected[j].score {
			return selected[i].value.MemoryID < selected[j].value.MemoryID
		}
		return selected[i].score > selected[j].score
	})
	if len(selected) > request.Limit {
		selected = selected[:request.Limit]
	}
	records := make([]ProjectMemoryVersion, 0, len(selected))
	identity := make([]string, 0, len(selected))
	selectedIDs := map[string]struct{}{}
	for _, item := range selected {
		records = append(records, item.value)
		identity = append(identity, item.value.MemoryID+"@"+fmt.Sprint(item.value.Version)+":"+item.value.ContentHash)
		selectedIDs[item.value.MemoryID] = struct{}{}
	}
	conflicts := make([]ProjectMemoryConflict, 0)
	removeIDs := map[string]struct{}{}
	for _, conflict := range s.projectMemoryConflicts {
		if conflict.SpaceID != request.SpaceID || conflict.CommittedEpoch > request.Watermark || (conflict.ResolvedEpoch != nil && *conflict.ResolvedEpoch <= request.Watermark) {
			continue
		}
		present := 0
		for _, memoryID := range conflict.MemoryIDs {
			if _, ok := selectedIDs[memoryID]; ok {
				present++
			}
		}
		if present == 0 {
			continue
		}
		if present != len(conflict.MemoryIDs) {
			for _, memoryID := range conflict.MemoryIDs {
				if _, ok := selectedIDs[memoryID]; ok {
					removeIDs[memoryID] = struct{}{}
				}
			}
			continue
		}
		visibleConflict := conflict
		if conflict.ResolvedEpoch != nil && *conflict.ResolvedEpoch > request.Watermark {
			visibleConflict.Status = "OPEN"
			visibleConflict.ResolutionMemoryID = ""
			visibleConflict.ResolvedAt = nil
			visibleConflict.ResolvedEpoch = nil
		}
		conflicts = append(conflicts, visibleConflict)
	}
	if len(removeIDs) > 0 {
		filteredRecords := records[:0]
		identity = identity[:0]
		for _, item := range records {
			if _, remove := removeIDs[item.MemoryID]; remove {
				excluded++
				continue
			}
			filteredRecords = append(filteredRecords, item)
			identity = append(identity, item.MemoryID+"@"+fmt.Sprint(item.Version)+":"+item.ContentHash)
		}
		records = filteredRecords
	}
	sort.Slice(conflicts, func(i, j int) bool { return conflicts[i].ConflictID < conflicts[j].ConflictID })
	return ProjectMemorySearchResult{SpaceID: request.SpaceID, MemoryWatermark: request.Watermark,
		Records: records, Conflicts: conflicts, ExcludedCount: excluded, SourceSetHash: stableHash(strings.Join(identity, "\x00"))}, nil
}

func memoryCommandKey(tenantID, ownerID, operation, idempotencyKey string) string {
	return strings.Join([]string{tenantID, ownerID, operation, idempotencyKey}, "\x00")
}

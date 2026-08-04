package capability

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	agentv1 "github.com/dante26979-droid/prd-agent/contracts/gen/go/agent/v1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

type LeaseStore interface {
	GetRunContext(context.Context, runcontrol.LeaseContext) (runcontrol.AgentRunInput, error)
}

type Server struct {
	agentv1.UnimplementedCapabilityGatewayServiceServer
	store      LeaseStore
	memory     runcontrol.ProjectMemoryStore
	repository *RepositoryManager
	bindingID  string
	token      string
}

func NewServer(store LeaseStore, repository *RepositoryManager, bindingID, token string) (*Server, error) {
	if store == nil || repository == nil {
		return nil, fmt.Errorf("capability lease store and repository are required")
	}
	if strings.TrimSpace(bindingID) == "" {
		return nil, fmt.Errorf("repository binding id is required")
	}
	if len(strings.TrimSpace(token)) < 32 {
		return nil, fmt.Errorf("capability service token must contain at least 32 characters")
	}
	memory, _ := store.(runcontrol.ProjectMemoryStore)
	return &Server{
		store:      store,
		memory:     memory,
		repository: repository,
		bindingID:  strings.TrimSpace(bindingID),
		token:      strings.TrimSpace(token),
	}, nil
}

func (s *Server) SearchProjectMemory(ctx context.Context, request *agentv1.SearchProjectMemoryRequest) (*agentv1.SearchProjectMemoryResponse, error) {
	if request == nil || strings.TrimSpace(request.GetQuery()) == "" || strings.TrimSpace(request.GetOperation()) == "" {
		return nil, status.Error(codes.InvalidArgument, "memory query and operation are required")
	}
	if request.GetLimit() < 0 || request.GetLimit() > int32(runcontrol.MaxMemorySearchLimit) {
		return nil, status.Error(codes.InvalidArgument, "memory search limit is invalid")
	}
	input, err := s.authorizeRun(ctx, request.GetCapability())
	if err != nil {
		return nil, err
	}
	if s.memory == nil {
		return nil, status.Error(codes.Unavailable, "project memory store is unavailable")
	}
	if input.MemorySpaceID == "" || input.MemoryAssignmentHash == "" || input.MemoryPolicyVersion == "" || input.MemoryAccessScopeHash == "" || input.MemoryWatermark < 0 {
		return nil, status.Error(codes.FailedPrecondition, "Agent Run has no memory assignment")
	}
	types := make([]runcontrol.ProjectMemoryType, 0, len(request.GetMemoryTypes()))
	for _, value := range request.GetMemoryTypes() {
		item := runcontrol.ProjectMemoryType(value)
		if !item.Valid() {
			return nil, status.Error(codes.InvalidArgument, "unknown project memory type")
		}
		types = append(types, item)
	}
	result, err := s.memory.SearchProjectMemory(ctx, runcontrol.ProjectMemorySearchRequest{
		TenantID: input.Run.TenantID, OwnerID: input.Run.OwnerID,
		SpaceID: input.MemorySpaceID, Watermark: input.MemoryWatermark,
		AccessScopeHash: input.MemoryAccessScopeHash, Query: request.GetQuery(),
		Operation: request.GetOperation(), MemoryTypes: types, Tags: request.GetTags(), Limit: int(request.GetLimit()),
	})
	if err != nil {
		if errors.Is(err, runcontrol.ErrInvalidPayload) {
			return nil, status.Error(codes.InvalidArgument, err.Error())
		}
		if errors.Is(err, runcontrol.ErrNotFound) {
			return nil, status.Error(codes.PermissionDenied, "project memory space is not available to this run")
		}
		return nil, status.Error(codes.Internal, "search project memory")
	}
	response := &agentv1.SearchProjectMemoryResponse{SpaceId: result.SpaceID, MemoryWatermark: result.MemoryWatermark, ExcludedCount: int32(result.ExcludedCount)}
	recordsByID := make(map[string]*agentv1.ProjectMemoryItem, len(result.Records))
	recordOrder := make([]string, 0, len(result.Records))
	for _, item := range result.Records {
		if !s.revalidateProjectMemory(ctx, input, item) {
			response.ExcludedCount++
			continue
		}
		valueJSON, _ := json.Marshal(item.Value)
		record := &agentv1.ProjectMemoryItem{MemoryId: item.MemoryID, Version: item.Version, MemoryType: string(item.MemoryType), Subject: item.Subject, Predicate: item.Predicate, ValueJson: string(valueJSON), Statement: item.Statement, AuthorityClass: string(item.AuthorityClass), Tags: append([]string(nil), item.Tags...), Sensitivity: item.Sensitivity, CommittedEpoch: item.CommittedEpoch, ContentHash: item.ContentHash}
		for _, ref := range item.SourceRefs {
			record.SourceRefs = append(record.SourceRefs, &agentv1.ProjectMemorySourceRef{SourceKind: ref.SourceKind, BindingId: ref.BindingID, SourceId: ref.SourceID, SourceVersion: ref.SourceVersion, Locator: ref.Locator, ContentHash: ref.ContentHash, AccessScopeHash: ref.AccessScopeHash})
		}
		recordsByID[item.MemoryID] = record
		recordOrder = append(recordOrder, item.MemoryID)
	}
	for _, item := range result.Conflicts {
		present := 0
		for _, memoryID := range item.MemoryIDs {
			if recordsByID[memoryID] != nil {
				present++
			}
		}
		if present == 0 {
			continue
		}
		if present != len(item.MemoryIDs) {
			// A conflict is an atomic recall group. If ranking, access checks, or
			// source revalidation removed one side, remove every side.
			for _, memoryID := range item.MemoryIDs {
				if recordsByID[memoryID] != nil {
					delete(recordsByID, memoryID)
					response.ExcludedCount++
				}
			}
			continue
		}
		response.Conflicts = append(response.Conflicts, &agentv1.ProjectMemoryConflict{ConflictId: item.ConflictID, Subject: item.Subject, Predicate: item.Predicate, MemoryIds: append([]string(nil), item.MemoryIDs...)})
	}
	identities := make([]string, 0, len(recordsByID))
	for _, memoryID := range recordOrder {
		record := recordsByID[memoryID]
		if record == nil {
			continue
		}
		response.Records = append(response.Records, record)
		identities = append(identities, fmt.Sprintf("%s@%d:%s", record.MemoryId, record.Version, record.ContentHash))
	}
	digest := sha256.Sum256([]byte(strings.Join(identities, "\x00")))
	response.SourceSetHash = fmt.Sprintf("sha256:%x", digest[:])
	return response, nil
}

func (s *Server) revalidateProjectMemory(ctx context.Context, input runcontrol.AgentRunInput, item runcontrol.ProjectMemoryVersion) bool {
	if item.AuthorityClass != runcontrol.MemorySourceVerified {
		return true
	}
	if len(item.SourceRefs) == 0 {
		return false
	}
	for _, ref := range item.SourceRefs {
		if ref.AccessScopeHash != "" && ref.AccessScopeHash != input.MemoryAccessScopeHash {
			return false
		}
		switch strings.ToUpper(strings.TrimSpace(ref.SourceKind)) {
		case "GITHUB", "GITHUB_REPOSITORY", "REPOSITORY_FILE":
			if ref.BindingID != s.bindingID || ref.SourceVersion != input.RepositoryRevision || ref.SourceVersion == "" {
				return false
			}
			path := strings.TrimSpace(ref.SourceID)
			if path == "" {
				path = strings.SplitN(strings.TrimSpace(ref.Locator), "#", 2)[0]
			}
			file, err := s.repository.Read(ctx, ref.SourceVersion, path)
			if err != nil || strings.TrimPrefix(file.ContentHash, "sha256:") != strings.TrimPrefix(ref.ContentHash, "sha256:") {
				return false
			}
		default:
			// This Gateway can only revalidate repository-backed sources. Other
			// source kinds fail closed until their authoritative adapter exists.
			return false
		}
	}
	return true
}

func (s *Server) ReadRepositoryTree(ctx context.Context, request *agentv1.ReadRepositoryTreeRequest) (*agentv1.ReadRepositoryTreeResponse, error) {
	if request == nil {
		return nil, status.Error(codes.InvalidArgument, "request is required")
	}
	if err := s.authorize(ctx, request.GetCapability(), request.GetBindingId(), request.GetRevision()); err != nil {
		return nil, err
	}
	paths, err := s.repository.Tree(ctx, request.GetRevision(), request.GetPrefix())
	if err != nil {
		return nil, repositoryStatus(err)
	}
	return &agentv1.ReadRepositoryTreeResponse{
		Tree: &agentv1.RepositoryTree{Paths: paths},
	}, nil
}

func (s *Server) ReadRepositoryFile(ctx context.Context, request *agentv1.ReadRepositoryFileRequest) (*agentv1.ReadRepositoryFileResponse, error) {
	if request == nil || strings.TrimSpace(request.GetPath()) == "" {
		return nil, status.Error(codes.InvalidArgument, "repository path is required")
	}
	if err := s.authorize(ctx, request.GetCapability(), request.GetBindingId(), request.GetRevision()); err != nil {
		return nil, err
	}
	file, err := s.repository.Read(ctx, request.GetRevision(), request.GetPath())
	if err != nil {
		return nil, repositoryStatus(err)
	}
	return &agentv1.ReadRepositoryFileResponse{
		File: &agentv1.RepositoryFile{
			Path:        file.Path,
			Content:     file.Content,
			ContentHash: file.ContentHash,
		},
	}, nil
}

func (s *Server) SearchRepository(ctx context.Context, request *agentv1.SearchRepositoryRequest) (*agentv1.SearchRepositoryResponse, error) {
	if request == nil || strings.TrimSpace(request.GetQuery()) == "" {
		return nil, status.Error(codes.InvalidArgument, "repository query is required")
	}
	if err := s.authorize(ctx, request.GetCapability(), request.GetBindingId(), request.GetRevision()); err != nil {
		return nil, err
	}
	hits, err := s.repository.Search(ctx, request.GetRevision(), request.GetQuery(), int(request.GetLimit()))
	if err != nil {
		return nil, repositoryStatus(err)
	}
	response := &agentv1.SearchRepositoryResponse{
		Hits: make([]*agentv1.RepositorySearchHit, 0, len(hits)),
	}
	for _, hit := range hits {
		response.Hits = append(response.Hits, &agentv1.RepositorySearchHit{
			Path:    hit.Path,
			Line:    int32(hit.Line),
			Snippet: hit.Snippet,
		})
	}
	return response, nil
}

func (s *Server) authorize(
	ctx context.Context,
	capability *agentv1.CapabilityLease,
	bindingID string,
	revision string,
) error {
	input, err := s.authorizeRun(ctx, capability)
	if err != nil {
		return err
	}
	if bindingID != s.bindingID || !revisionPattern.MatchString(revision) {
		return status.Error(codes.PermissionDenied, "repository binding or revision is not allowed")
	}
	if input.RepositoryBindingID != bindingID || input.RepositoryRevision != revision {
		return status.Error(codes.PermissionDenied, "repository capability is not bound to this Agent Run")
	}
	return nil
}

func (s *Server) authorizeRun(ctx context.Context, capability *agentv1.CapabilityLease) (runcontrol.AgentRunInput, error) {
	if !s.validServiceToken(ctx) {
		return runcontrol.AgentRunInput{}, status.Error(codes.Unauthenticated, "invalid capability service identity")
	}
	if capability == nil || capability.GetLease() == nil || capability.GetMeta() == nil {
		return runcontrol.AgentRunInput{}, status.Error(codes.InvalidArgument, "capability lease and request metadata are required")
	}
	if capability.GetMeta().GetRequestId() == "" || capability.GetMeta().GetCorrelationId() == "" {
		return runcontrol.AgentRunInput{}, status.Error(codes.InvalidArgument, "capability request identity is required")
	}
	lease, err := leaseFromProto(capability.GetLease())
	if err != nil {
		return runcontrol.AgentRunInput{}, status.Error(codes.InvalidArgument, err.Error())
	}
	input, err := s.store.GetRunContext(ctx, lease)
	if err != nil {
		if errors.Is(err, runcontrol.ErrLeaseLost) || errors.Is(err, runcontrol.ErrNotFound) {
			return runcontrol.AgentRunInput{}, status.Error(codes.PermissionDenied, "Agent Run lease is not active")
		}
		return runcontrol.AgentRunInput{}, status.Error(codes.Internal, "validate Agent Run lease")
	}
	if input.Run.TenantID == "" || input.Run.OwnerID == "" || input.Run.RunID != lease.RunID {
		return runcontrol.AgentRunInput{}, status.Error(codes.PermissionDenied, "capability is not bound to this Agent Run")
	}
	return input, nil
}

func (s *Server) validServiceToken(ctx context.Context) bool {
	values := metadata.ValueFromIncomingContext(ctx, "authorization")
	if len(values) != 1 || !strings.HasPrefix(values[0], "Bearer ") {
		return false
	}
	provided := strings.TrimPrefix(values[0], "Bearer ")
	return len(provided) == len(s.token) &&
		subtle.ConstantTimeCompare([]byte(provided), []byte(s.token)) == 1
}

func leaseFromProto(value *agentv1.LeaseContext) (runcontrol.LeaseContext, error) {
	if value.GetRunId() == "" || value.GetLeaseId() == "" || value.GetWorkerId() == "" || value.GetFencingToken() < 1 {
		return runcontrol.LeaseContext{}, fmt.Errorf("complete Agent Run lease is required")
	}
	expiresAt, err := time.Parse(time.RFC3339Nano, value.GetExpiresAt())
	if err != nil {
		return runcontrol.LeaseContext{}, fmt.Errorf("invalid Agent Run lease expiry")
	}
	return runcontrol.LeaseContext{
		RunID:        value.GetRunId(),
		LeaseID:      value.GetLeaseId(),
		WorkerID:     value.GetWorkerId(),
		FencingToken: value.GetFencingToken(),
		ExpiresAt:    expiresAt,
	}, nil
}

func repositoryStatus(err error) error {
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return status.Error(codes.DeadlineExceeded, "repository capability timed out")
	}
	message := err.Error()
	switch {
	case strings.Contains(message, "outside the snapshot"),
		strings.Contains(message, "escapes the snapshot"),
		strings.Contains(message, "query is empty"),
		strings.Contains(message, "limit must"):
		return status.Error(codes.InvalidArgument, message)
	case errors.Is(err, os.ErrNotExist), strings.Contains(message, "no such file"):
		return status.Error(codes.NotFound, "repository file was not found")
	case strings.Contains(message, "exceeds"):
		return status.Error(codes.ResourceExhausted, message)
	default:
		return status.Error(codes.Unavailable, "repository snapshot is unavailable")
	}
}

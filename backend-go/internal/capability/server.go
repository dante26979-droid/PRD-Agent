package capability

import (
	"context"
	"crypto/subtle"
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
	return &Server{
		store:      store,
		repository: repository,
		bindingID:  strings.TrimSpace(bindingID),
		token:      strings.TrimSpace(token),
	}, nil
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
	if !s.validServiceToken(ctx) {
		return status.Error(codes.Unauthenticated, "invalid capability service identity")
	}
	if capability == nil || capability.GetLease() == nil || capability.GetMeta() == nil {
		return status.Error(codes.InvalidArgument, "capability lease and request metadata are required")
	}
	if capability.GetMeta().GetRequestId() == "" || capability.GetMeta().GetCorrelationId() == "" {
		return status.Error(codes.InvalidArgument, "capability request identity is required")
	}
	if bindingID != s.bindingID || !revisionPattern.MatchString(revision) {
		return status.Error(codes.PermissionDenied, "repository binding or revision is not allowed")
	}
	lease, err := leaseFromProto(capability.GetLease())
	if err != nil {
		return status.Error(codes.InvalidArgument, err.Error())
	}
	input, err := s.store.GetRunContext(ctx, lease)
	if err != nil {
		if errors.Is(err, runcontrol.ErrLeaseLost) || errors.Is(err, runcontrol.ErrNotFound) {
			return status.Error(codes.PermissionDenied, "Agent Run lease is not active")
		}
		return status.Error(codes.Internal, "validate Agent Run lease")
	}
	if input.Run.TenantID == "" || input.Run.OwnerID == "" ||
		input.RepositoryBindingID != bindingID || input.RepositoryRevision != revision {
		return status.Error(codes.PermissionDenied, "repository capability is not bound to this Agent Run")
	}
	return nil
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

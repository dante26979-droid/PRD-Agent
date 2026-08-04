package httpapi

import (
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/gin-gonic/gin"
)

func (a *Router) projectMemoryStore(c *gin.Context) (runcontrol.ProjectMemoryStore, bool) {
	store, ok := a.store.(runcontrol.ProjectMemoryStore)
	if !ok {
		writeError(c, http.StatusNotImplemented, "PROJECT_MEMORY_UNAVAILABLE", "project memory is not configured")
	}
	return store, ok
}

func memoryIdempotencyKey(c *gin.Context) (string, bool) {
	key := strings.TrimSpace(c.GetHeader("Idempotency-Key"))
	if key == "" {
		writeError(c, http.StatusBadRequest, "MISSING_IDEMPOTENCY_KEY", "Idempotency-Key is required")
		return "", false
	}
	return key, true
}

type createMemorySpaceRequest struct {
	ProjectKey  string `json:"project_key" binding:"required,min=1,max=120"`
	DisplayName string `json:"display_name" binding:"required,min=1,max=240"`
}

func (a *Router) createMemorySpace(c *gin.Context) {
	store, ok := a.projectMemoryStore(c)
	if !ok {
		return
	}
	var request createMemorySpaceRequest
	if err := c.ShouldBindJSON(&request); err != nil {
		writeError(c, http.StatusBadRequest, "INVALID_REQUEST", err.Error())
		return
	}
	key, ok := memoryIdempotencyKey(c)
	if !ok {
		return
	}
	space, err := store.CreateMemorySpace(c.Request.Context(), runcontrol.CreateMemorySpaceCommand{TenantID: c.GetString("tenant_id"), OwnerID: c.GetString("owner_id"), ProjectKey: request.ProjectKey, DisplayName: request.DisplayName, IdempotencyKey: key})
	if err != nil {
		writeStoreError(c, err)
		return
	}
	c.JSON(http.StatusCreated, gin.H{"space": space})
}

func (a *Router) listMemorySpaces(c *gin.Context) {
	store, ok := a.projectMemoryStore(c)
	if !ok {
		return
	}
	items, err := store.ListMemorySpaces(c.Request.Context(), c.GetString("tenant_id"), c.GetString("owner_id"))
	if err != nil {
		writeStoreError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": items})
}

type proposeProjectMemoryRequest struct {
	MemoryType       runcontrol.ProjectMemoryType    `json:"memory_type" binding:"required"`
	Subject          string                          `json:"subject" binding:"required"`
	Predicate        string                          `json:"predicate" binding:"required"`
	Value            any                             `json:"value" binding:"required"`
	Statement        string                          `json:"statement" binding:"required"`
	AuthorityClass   runcontrol.MemoryAuthorityClass `json:"authority_class" binding:"required"`
	Tags             []string                        `json:"tags"`
	Sensitivity      string                          `json:"sensitivity"`
	SourceRefs       []runcontrol.MemorySourceRef    `json:"source_refs"`
	ReasonCode       string                          `json:"reason_code"`
	ProposedByRunID  string                          `json:"proposed_by_run_id"`
	ExtractorVersion string                          `json:"extractor_version"`
}

func (a *Router) proposeProjectMemory(c *gin.Context) {
	store, ok := a.projectMemoryStore(c)
	if !ok {
		return
	}
	var request proposeProjectMemoryRequest
	if err := c.ShouldBindJSON(&request); err != nil {
		writeError(c, http.StatusBadRequest, "INVALID_REQUEST", err.Error())
		return
	}
	key, ok := memoryIdempotencyKey(c)
	if !ok {
		return
	}
	item, err := store.ProposeProjectMemory(c.Request.Context(), runcontrol.ProposeMemoryCommand{TenantID: c.GetString("tenant_id"), OwnerID: c.GetString("owner_id"), SpaceID: c.Param("spaceID"), MemoryType: request.MemoryType, Subject: request.Subject, Predicate: request.Predicate, Value: request.Value, Statement: request.Statement, AuthorityClass: request.AuthorityClass, Tags: request.Tags, Sensitivity: request.Sensitivity, SourceRefs: request.SourceRefs, ReasonCode: request.ReasonCode, ProposedByRunID: request.ProposedByRunID, ExtractorVersion: request.ExtractorVersion, IdempotencyKey: key})
	if err != nil {
		writeStoreError(c, err)
		return
	}
	c.JSON(http.StatusCreated, gin.H{"candidate": item})
}

func (a *Router) listProjectMemoryCandidates(c *gin.Context) {
	store, ok := a.projectMemoryStore(c)
	if !ok {
		return
	}
	status := runcontrol.MemoryCandidateStatus(strings.ToUpper(strings.TrimSpace(c.Query("status"))))
	if status != "" && !status.Valid() {
		writeError(c, http.StatusBadRequest, "INVALID_REQUEST", "unknown project memory candidate status")
		return
	}
	items, err := store.ListProjectMemoryCandidates(c.Request.Context(), c.GetString("tenant_id"), c.GetString("owner_id"), c.Param("spaceID"), status)
	if err != nil {
		writeStoreError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": items})
}

type reviewMemoryCandidateRequest struct {
	ExpectedHash string `json:"expected_hash" binding:"required"`
}

func (a *Router) confirmProjectMemoryCandidate(c *gin.Context) {
	a.reviewProjectMemoryCandidate(c, true)
}

func (a *Router) rejectProjectMemoryCandidate(c *gin.Context) {
	a.reviewProjectMemoryCandidate(c, false)
}

func (a *Router) reviewProjectMemoryCandidate(c *gin.Context, confirm bool) {
	store, ok := a.projectMemoryStore(c)
	if !ok {
		return
	}
	var request reviewMemoryCandidateRequest
	if err := c.ShouldBindJSON(&request); err != nil {
		writeError(c, http.StatusBadRequest, "INVALID_REQUEST", err.Error())
		return
	}
	key, ok := memoryIdempotencyKey(c)
	if !ok {
		return
	}
	command := runcontrol.ReviewMemoryCandidateCommand{TenantID: c.GetString("tenant_id"), OwnerID: c.GetString("owner_id"), SpaceID: c.Param("spaceID"), CandidateID: c.Param("candidateID"), ExpectedHash: request.ExpectedHash, ActorRef: "user:" + c.GetString("owner_id"), IdempotencyKey: key}
	if confirm {
		record, err := store.ConfirmProjectMemoryCandidate(c.Request.Context(), command)
		if err != nil {
			writeStoreError(c, err)
			return
		}
		c.JSON(http.StatusOK, gin.H{"record": record})
		return
	}
	candidate, err := store.RejectProjectMemoryCandidate(c.Request.Context(), command)
	if err != nil {
		writeStoreError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"candidate": candidate})
}

func (a *Router) listProjectMemory(c *gin.Context) {
	store, ok := a.projectMemoryStore(c)
	if !ok {
		return
	}
	includeInactive, err := strconv.ParseBool(defaultQuery(c.Query("include_inactive"), "false"))
	if err != nil {
		writeError(c, http.StatusBadRequest, "INVALID_REQUEST", "include_inactive must be a boolean")
		return
	}
	items, err := store.ListProjectMemory(c.Request.Context(), c.GetString("tenant_id"), c.GetString("owner_id"), c.Param("spaceID"), includeInactive)
	if err != nil {
		writeStoreError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": items})
}

func (a *Router) getProjectMemoryHistory(c *gin.Context) {
	store, ok := a.projectMemoryStore(c)
	if !ok {
		return
	}
	items, err := store.GetProjectMemoryHistory(c.Request.Context(), c.GetString("tenant_id"), c.GetString("owner_id"), c.Param("spaceID"), c.Param("memoryID"))
	if err != nil {
		writeStoreError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": items})
}

func (a *Router) listProjectMemoryConflicts(c *gin.Context) {
	store, ok := a.projectMemoryStore(c)
	if !ok {
		return
	}
	includeResolved, err := strconv.ParseBool(defaultQuery(c.Query("include_resolved"), "false"))
	if err != nil {
		writeError(c, http.StatusBadRequest, "INVALID_REQUEST", "include_resolved must be a boolean")
		return
	}
	items, err := store.ListProjectMemoryConflicts(c.Request.Context(), c.GetString("tenant_id"), c.GetString("owner_id"), c.Param("spaceID"), includeResolved)
	if err != nil {
		writeStoreError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": items})
}

type searchProjectMemoryRequest struct {
	Query       string                         `json:"query"`
	Operation   string                         `json:"operation"`
	MemoryTypes []runcontrol.ProjectMemoryType `json:"memory_types"`
	Tags        []string                       `json:"tags"`
	Limit       int                            `json:"limit" binding:"omitempty,min=1,max=50"`
}

func (a *Router) searchProjectMemory(c *gin.Context) {
	store, ok := a.projectMemoryStore(c)
	if !ok {
		return
	}
	var request searchProjectMemoryRequest
	if err := c.ShouldBindJSON(&request); err != nil {
		writeError(c, http.StatusBadRequest, "INVALID_REQUEST", err.Error())
		return
	}
	spaces, err := store.ListMemorySpaces(c.Request.Context(), c.GetString("tenant_id"), c.GetString("owner_id"))
	if err != nil {
		writeStoreError(c, err)
		return
	}
	var assignment runcontrol.RunMemoryAssignment
	for _, space := range spaces {
		if space.SpaceID == c.Param("spaceID") {
			assignment = runcontrol.BuildRunMemoryAssignment("owner-memory-search", space, time.Now().UTC())
			break
		}
	}
	if assignment.SpaceID == "" {
		writeStoreError(c, runcontrol.ErrNotFound)
		return
	}
	result, err := store.SearchProjectMemory(c.Request.Context(), runcontrol.ProjectMemorySearchRequest{TenantID: c.GetString("tenant_id"), OwnerID: c.GetString("owner_id"), SpaceID: c.Param("spaceID"), Watermark: assignment.MemoryWatermark, AccessScopeHash: assignment.AccessScopeHash, Query: request.Query, Operation: request.Operation, MemoryTypes: request.MemoryTypes, Tags: request.Tags, Limit: request.Limit})
	if err != nil {
		writeStoreError(c, err)
		return
	}
	c.JSON(http.StatusOK, result)
}

type revokeProjectMemoryRequest struct {
	ExpectedVersion int64 `json:"expected_version" binding:"required,min=1"`
}

func (a *Router) revokeProjectMemory(c *gin.Context) {
	store, ok := a.projectMemoryStore(c)
	if !ok {
		return
	}
	var request revokeProjectMemoryRequest
	if err := c.ShouldBindJSON(&request); err != nil {
		writeError(c, http.StatusBadRequest, "INVALID_REQUEST", err.Error())
		return
	}
	key, ok := memoryIdempotencyKey(c)
	if !ok {
		return
	}
	record, err := store.RevokeProjectMemory(c.Request.Context(), runcontrol.RevokeMemoryCommand{TenantID: c.GetString("tenant_id"), OwnerID: c.GetString("owner_id"), SpaceID: c.Param("spaceID"), MemoryID: c.Param("memoryID"), ExpectedVersion: request.ExpectedVersion, ActorRef: "user:" + c.GetString("owner_id"), IdempotencyKey: key})
	if err != nil {
		writeStoreError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"record": record})
}

func defaultQuery(value, fallback string) string {
	if strings.TrimSpace(value) == "" {
		return fallback
	}
	return value
}

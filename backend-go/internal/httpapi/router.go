package httpapi

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/gin-gonic/gin"
)

var apiMetrics = newHTTPMetrics()

type metricKey struct {
	route  string
	method string
	status int
}

type latencyValue struct {
	count uint64
	sum   float64
}

type httpMetrics struct {
	mu       sync.RWMutex
	requests map[metricKey]uint64
	latency  map[metricKey]latencyValue
}

func newHTTPMetrics() *httpMetrics {
	return &httpMetrics{
		requests: make(map[metricKey]uint64),
		latency:  make(map[metricKey]latencyValue),
	}
}

func (m *httpMetrics) observe(route, method string, status int, duration time.Duration) {
	key := metricKey{route: route, method: method, status: status}
	latencyKey := metricKey{route: route, method: method}
	m.mu.Lock()
	m.requests[key]++
	value := m.latency[latencyKey]
	value.count++
	value.sum += duration.Seconds()
	m.latency[latencyKey] = value
	m.mu.Unlock()
}

func (m *httpMetrics) serve(c *gin.Context) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	var output strings.Builder
	output.WriteString("# HELP prd_agent_api_requests_total Public API requests by route, method and status.\n")
	output.WriteString("# TYPE prd_agent_api_requests_total counter\n")
	for key, count := range m.requests {
		fmt.Fprintf(&output, "prd_agent_api_requests_total{route=%q,method=%q,status=%q} %d\n", key.route, key.method, strconv.Itoa(key.status), count)
	}
	output.WriteString("# HELP prd_agent_api_request_duration_seconds Public API request latency.\n")
	output.WriteString("# TYPE prd_agent_api_request_duration_seconds summary\n")
	for key, value := range m.latency {
		fmt.Fprintf(&output, "prd_agent_api_request_duration_seconds_sum{route=%q,method=%q} %g\n", key.route, key.method, value.sum)
		fmt.Fprintf(&output, "prd_agent_api_request_duration_seconds_count{route=%q,method=%q} %d\n", key.route, key.method, value.count)
	}
	c.Data(http.StatusOK, "text/plain; version=0.0.4; charset=utf-8", []byte(output.String()))
}

type Router struct {
	store runcontrol.Store
}

func NewRouter(store runcontrol.Store) *gin.Engine {
	return NewRouterWithPrincipalResolver(store, DevPrincipalResolver{})
}

type PrincipalResolver interface {
	Resolve(request *http.Request) (tenantID, ownerID string, err error)
}

type DevPrincipalResolver struct{}

func (DevPrincipalResolver) Resolve(request *http.Request) (string, string, error) {
	ownerID := request.Header.Get("X-User-ID")
	if ownerID == "" {
		ownerID = "local-user"
	}
	tenantID := request.Header.Get("X-Tenant-ID")
	if tenantID == "" {
		tenantID = "local"
	}
	return tenantID, ownerID, nil
}

func NewRouterWithPrincipalResolver(store runcontrol.Store, resolver PrincipalResolver) *gin.Engine {
	return NewSecureRouter(store, resolver, "")
}

func NewSecureRouter(store runcontrol.Store, resolver PrincipalResolver, publicOrigin string) *gin.Engine {
	api := &Router{store: store}
	router := gin.New()
	router.Use(gin.Recovery())
	router.Use(metricsMiddleware())
	router.Use(originMiddleware(publicOrigin))
	router.GET("/metrics", apiMetrics.serve)
	router.GET("/api/v1/health/live", api.live)
	router.GET("/api/v1/health/ready", api.ready)

	group := router.Group("/api/v1")
	group.Use(principalMiddleware(resolver))
	group.GET("/me", api.me)
	group.GET("/tasks", api.listTasks)
	group.POST("/tasks/from-message", api.startTask)
	group.GET("/tasks/:taskID", api.getTask)
	group.GET("/tasks/:taskID/events", api.taskEvents)
	group.GET("/tasks/:taskID/runs", api.listRuns)
	group.POST("/tasks/:taskID/runs/:runID/stop", api.stopRun)
	group.POST("/tasks/:taskID/retry", api.retryTask)
	group.GET("/tasks/:taskID/draft", api.getDraft)
	group.GET("/tasks/:taskID/draft.md", api.getDraftMarkdown)
	group.GET("/tasks/:taskID/confirmation-units", api.listConfirmationUnits)
	group.POST("/tasks/:taskID/confirmation-units/:unitVersionID/confirm", api.confirmConfirmationUnit)
	group.POST("/tasks/:taskID/confirmation-units/:unitVersionID/reopen", api.reopenConfirmationUnit)
	group.GET("/tasks/:taskID/evidence", api.listEvidence)
	group.GET("/tasks/:taskID/attempts", api.listAttempts)
	group.POST("/tasks/:taskID/publish/feishu/preview", api.previewFeishuPublish)
	group.POST("/tasks/:taskID/publish/feishu", api.confirmFeishuPublish)
	group.GET("/tasks/:taskID/publishes", api.listPublishes)
	return router
}

func metricsMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		started := time.Now()
		c.Next()
		route := c.FullPath()
		if route == "" {
			route = "unmatched"
		}
		apiMetrics.observe(route, metricMethod(c.Request.Method), c.Writer.Status(), time.Since(started))
	}
}

func metricMethod(method string) string {
	switch method {
	case http.MethodGet, http.MethodHead, http.MethodPost, http.MethodPut,
		http.MethodPatch, http.MethodDelete, http.MethodOptions:
		return method
	default:
		return "OTHER"
	}
}

func originMiddleware(publicOrigin string) gin.HandlerFunc {
	expectedHost := ""
	if parsed, err := url.Parse(publicOrigin); err == nil {
		expectedHost = parsed.Host
	}
	return func(c *gin.Context) {
		switch c.Request.Method {
		case http.MethodPost, http.MethodPut, http.MethodPatch, http.MethodDelete:
			if publicOrigin != "" && (c.GetHeader("Origin") != publicOrigin || c.Request.Host != expectedHost) {
				writeError(c, http.StatusForbidden, "ORIGIN_REJECTED", "request origin is not allowed")
				c.Abort()
				return
			}
		}
		c.Next()
	}
}

func principalMiddleware(resolver PrincipalResolver) gin.HandlerFunc {
	return func(c *gin.Context) {
		tenantID, ownerID, err := resolver.Resolve(c.Request)
		if err != nil {
			writeError(c, http.StatusUnauthorized, "UNAUTHENTICATED", err.Error())
			c.Abort()
			return
		}
		c.Set("tenant_id", tenantID)
		c.Set("owner_id", ownerID)
		c.Next()
	}
}

func (a *Router) live(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{"status": "ok"})
}

func (a *Router) ready(c *gin.Context) {
	if err := a.store.Health(c.Request.Context()); err != nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"status": "not_ready"})
		return
	}
	c.JSON(http.StatusOK, gin.H{"status": "ready"})
}

func (a *Router) me(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{
		"tenant_id": c.GetString("tenant_id"),
		"user_id":   c.GetString("owner_id"),
	})
}

type startTaskRequest struct {
	Message string `json:"message" binding:"required,min=1,max=20000"`
}

func (a *Router) startTask(c *gin.Context) {
	var request startTaskRequest
	if err := c.ShouldBindJSON(&request); err != nil {
		writeError(c, http.StatusBadRequest, "INVALID_REQUEST", err.Error())
		return
	}
	key := c.GetHeader("Idempotency-Key")
	if key == "" {
		writeError(c, http.StatusBadRequest, "MISSING_IDEMPOTENCY_KEY", "Idempotency-Key is required")
		return
	}
	value, err := a.store.CreateTaskWithRun(c.Request.Context(), c.GetString("tenant_id"), c.GetString("owner_id"), request.Message, key)
	if err != nil {
		if errors.Is(err, runcontrol.ErrInvalidIdempotency) {
			writeError(c, http.StatusConflict, "IDEMPOTENCY_CONFLICT", err.Error())
			return
		}
		if errors.Is(err, runcontrol.ErrCapacityExhausted) {
			c.Header("Retry-After", "5")
			c.JSON(http.StatusTooManyRequests, gin.H{
				"error_code": "CAPACITY_EXHAUSTED",
				"message":    err.Error(),
				"retryable":  true,
			})
			return
		}
		writeError(c, http.StatusInternalServerError, "TASK_CREATE_FAILED", err.Error())
		return
	}
	c.JSON(http.StatusCreated, value)
}

func (a *Router) listTasks(c *gin.Context) {
	limit := 50
	if raw := c.Query("limit"); raw != "" {
		parsed, err := strconv.Atoi(raw)
		if err != nil || parsed < 1 || parsed > 100 {
			writeError(c, http.StatusBadRequest, "INVALID_LIMIT", "limit must be between 1 and 100")
			return
		}
		limit = parsed
	}
	items, err := a.store.ListTasks(c.Request.Context(), c.GetString("tenant_id"), c.GetString("owner_id"), limit)
	if err != nil {
		writeError(c, http.StatusInternalServerError, "TASK_LIST_FAILED", err.Error())
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": items})
}

func (a *Router) getTask(c *gin.Context) {
	task, err := a.store.GetTask(c.Request.Context(), c.GetString("tenant_id"), c.GetString("owner_id"), c.Param("taskID"))
	if err != nil {
		writeStoreError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"task": task})
}

func (a *Router) taskEvents(c *gin.Context) {
	after, err := eventCursor(c)
	if err != nil {
		writeError(c, http.StatusBadRequest, "INVALID_EVENT_CURSOR", err.Error())
		return
	}
	limit := runcontrol.DefaultEventPageSize
	if raw := c.Query("limit"); raw != "" {
		limit, err = strconv.Atoi(raw)
		if err != nil || limit < 1 || limit > runcontrol.MaxEventPageSize {
			writeError(c, http.StatusBadRequest, "INVALID_LIMIT", "limit must be between 1 and 1000")
			return
		}
	}
	events, err := a.store.ListTaskEvents(c.Request.Context(), c.GetString("tenant_id"), c.GetString("owner_id"), c.Param("taskID"), after, limit)
	if err != nil {
		writeStoreError(c, err)
		return
	}
	c.Header("Content-Type", "text/event-stream")
	c.Header("Cache-Control", "no-cache")
	c.Header("Connection", "keep-alive")
	c.Header("X-Accel-Buffering", "no")
	for _, event := range events {
		if _, err := fmt.Fprintf(c.Writer, "id: %d\nevent: %s\ndata: %s\n\n", event.Sequence, event.EventType, event.Payload); err != nil {
			return
		}
		c.Writer.Flush()
	}
	if len(events) == 0 {
		// The first implementation intentionally completes an empty poll with a
		// heartbeat. Clients reconnect with Last-Event-ID; this keeps local
		// validation deterministic while preserving SSE wire semantics.
		_, _ = c.Writer.WriteString(": heartbeat\n\n")
		c.Writer.Flush()
	}
}

func eventCursor(c *gin.Context) (int64, error) {
	raw := c.Query("after_sequence")
	if raw == "" {
		raw = c.GetHeader("Last-Event-ID")
	}
	if raw == "" {
		return 0, nil
	}
	sequence, err := strconv.ParseInt(raw, 10, 64)
	if err != nil || sequence < 0 {
		return 0, fmt.Errorf("cursor must be a non-negative sequence")
	}
	return sequence, nil
}

func (a *Router) listRuns(c *gin.Context) {
	runs, err := a.store.ListRuns(c.Request.Context(), c.GetString("tenant_id"), c.GetString("owner_id"), c.Param("taskID"))
	if err != nil {
		writeStoreError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": runs})
}

func (a *Router) stopRun(c *gin.Context) {
	if c.GetHeader("Idempotency-Key") == "" {
		writeError(c, http.StatusBadRequest, "MISSING_IDEMPOTENCY_KEY", "Idempotency-Key is required")
		return
	}
	run, err := a.store.StopRun(c.Request.Context(), c.GetString("tenant_id"), c.GetString("owner_id"), c.Param("taskID"), c.Param("runID"))
	if err != nil {
		writeStoreError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"run": run})
}

type retryTaskRequest struct {
	ExpectedTaskVersion int `json:"expected_task_version" binding:"required,min=1"`
}

func (a *Router) retryTask(c *gin.Context) {
	var request retryTaskRequest
	if err := c.ShouldBindJSON(&request); err != nil {
		writeError(c, http.StatusBadRequest, "INVALID_REQUEST", err.Error())
		return
	}
	key := c.GetHeader("Idempotency-Key")
	if key == "" {
		writeError(c, http.StatusBadRequest, "MISSING_IDEMPOTENCY_KEY", "Idempotency-Key is required")
		return
	}
	run, err := a.store.RetryTask(
		c.Request.Context(), c.GetString("tenant_id"), c.GetString("owner_id"),
		c.Param("taskID"), key, request.ExpectedTaskVersion,
	)
	if err != nil {
		writeMutationError(c, err)
		return
	}
	c.JSON(http.StatusCreated, gin.H{"run": run})
}

func (a *Router) getDraft(c *gin.Context) {
	draft, err := a.store.GetLatestDraft(
		c.Request.Context(), c.GetString("tenant_id"), c.GetString("owner_id"), c.Param("taskID"),
	)
	if err != nil {
		writeStoreError(c, err)
		return
	}
	var content any = string(draft.Content)
	if json.Valid(draft.Content) {
		content = json.RawMessage(draft.Content)
	}
	c.JSON(http.StatusOK, gin.H{"draft": draft, "content": content})
}

func (a *Router) getDraftMarkdown(c *gin.Context) {
	draft, err := a.store.GetLatestDraft(
		c.Request.Context(), c.GetString("tenant_id"), c.GetString("owner_id"), c.Param("taskID"),
	)
	if err != nil {
		writeStoreError(c, err)
		return
	}
	markdown := string(draft.Content)
	var document struct {
		Markdown string `json:"markdown"`
	}
	if json.Unmarshal(draft.Content, &document) == nil && document.Markdown != "" {
		markdown = document.Markdown
	}
	c.Header("Content-Disposition", `attachment; filename="working-draft.md"`)
	c.Data(http.StatusOK, "text/markdown; charset=utf-8", []byte(markdown))
}

func (a *Router) listConfirmationUnits(c *gin.Context) {
	items, err := a.store.ListConfirmationUnits(
		c.Request.Context(), c.GetString("tenant_id"), c.GetString("owner_id"), c.Param("taskID"),
	)
	if err != nil {
		writeStoreError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": items})
}

type confirmationDecisionRequest struct {
	ExpectedTaskVersion int    `json:"expected_task_version" binding:"required,min=1"`
	Feedback            string `json:"feedback"`
}

func (a *Router) confirmConfirmationUnit(c *gin.Context) {
	var request confirmationDecisionRequest
	if err := c.ShouldBindJSON(&request); err != nil {
		writeError(c, http.StatusBadRequest, "INVALID_REQUEST", err.Error())
		return
	}
	key := c.GetHeader("Idempotency-Key")
	if key == "" {
		writeError(c, http.StatusBadRequest, "MISSING_IDEMPOTENCY_KEY", "Idempotency-Key is required")
		return
	}
	result, err := a.store.ConfirmConfirmationUnit(
		c.Request.Context(), c.GetString("tenant_id"), c.GetString("owner_id"),
		c.Param("taskID"), c.Param("unitVersionID"), key, request.ExpectedTaskVersion,
	)
	if err != nil {
		writeMutationError(c, err)
		return
	}
	c.JSON(http.StatusOK, result)
}

func (a *Router) reopenConfirmationUnit(c *gin.Context) {
	var request confirmationDecisionRequest
	if err := c.ShouldBindJSON(&request); err != nil {
		writeError(c, http.StatusBadRequest, "INVALID_REQUEST", err.Error())
		return
	}
	key := c.GetHeader("Idempotency-Key")
	if key == "" {
		writeError(c, http.StatusBadRequest, "MISSING_IDEMPOTENCY_KEY", "Idempotency-Key is required")
		return
	}
	result, err := a.store.ReopenConfirmationUnit(
		c.Request.Context(), c.GetString("tenant_id"), c.GetString("owner_id"),
		c.Param("taskID"), c.Param("unitVersionID"), request.Feedback, key,
		request.ExpectedTaskVersion,
	)
	if err != nil {
		writeMutationError(c, err)
		return
	}
	c.JSON(http.StatusCreated, result)
}

func (a *Router) listEvidence(c *gin.Context) {
	limit, err := boundedLimit(c, 100)
	if err != nil {
		writeError(c, http.StatusBadRequest, "INVALID_LIMIT", err.Error())
		return
	}
	items, err := a.store.ListEvidence(
		c.Request.Context(), c.GetString("tenant_id"), c.GetString("owner_id"), c.Param("taskID"), limit,
	)
	if err != nil {
		writeStoreError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": items})
}

func (a *Router) listAttempts(c *gin.Context) {
	limit, err := boundedLimit(c, 100)
	if err != nil {
		writeError(c, http.StatusBadRequest, "INVALID_LIMIT", err.Error())
		return
	}
	items, err := a.store.ListModelAttempts(
		c.Request.Context(), c.GetString("tenant_id"), c.GetString("owner_id"), c.Param("taskID"), limit,
	)
	if err != nil {
		writeStoreError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": items})
}

type publishPreviewRequest struct {
	ExpectedTaskVersion int `json:"expected_task_version" binding:"required,min=1"`
}

func (a *Router) previewFeishuPublish(c *gin.Context) {
	var request publishPreviewRequest
	if err := c.ShouldBindJSON(&request); err != nil {
		writeError(c, http.StatusBadRequest, "INVALID_REQUEST", err.Error())
		return
	}
	key := c.GetHeader("Idempotency-Key")
	if key == "" {
		writeError(c, http.StatusBadRequest, "MISSING_IDEMPOTENCY_KEY", "Idempotency-Key is required")
		return
	}
	preview, err := a.store.CreatePublishPreview(
		c.Request.Context(), c.GetString("tenant_id"), c.GetString("owner_id"),
		c.Param("taskID"), key, request.ExpectedTaskVersion, time.Now().UTC(),
	)
	if err != nil {
		writeMutationError(c, err)
		return
	}
	c.JSON(http.StatusCreated, gin.H{"preview": preview})
}

type confirmPublishRequest struct {
	PublishID           string `json:"publish_id" binding:"required"`
	ConfirmationToken   string `json:"confirmation_token" binding:"required"`
	ExpectedTaskVersion int    `json:"expected_task_version" binding:"required,min=1"`
}

func (a *Router) confirmFeishuPublish(c *gin.Context) {
	var request confirmPublishRequest
	if err := c.ShouldBindJSON(&request); err != nil {
		writeError(c, http.StatusBadRequest, "INVALID_REQUEST", err.Error())
		return
	}
	key := c.GetHeader("Idempotency-Key")
	if key == "" {
		writeError(c, http.StatusBadRequest, "MISSING_IDEMPOTENCY_KEY", "Idempotency-Key is required")
		return
	}
	record, err := a.store.ConfirmPublish(
		c.Request.Context(), c.GetString("tenant_id"), c.GetString("owner_id"), c.Param("taskID"),
		request.PublishID, request.ConfirmationToken, key, request.ExpectedTaskVersion, time.Now().UTC(),
	)
	if err != nil {
		writeMutationError(c, err)
		return
	}
	c.JSON(http.StatusAccepted, gin.H{"publish": record})
}

func (a *Router) listPublishes(c *gin.Context) {
	limit, err := boundedLimit(c, 100)
	if err != nil {
		writeError(c, http.StatusBadRequest, "INVALID_LIMIT", err.Error())
		return
	}
	items, err := a.store.ListPublishes(
		c.Request.Context(), c.GetString("tenant_id"), c.GetString("owner_id"), c.Param("taskID"), limit,
	)
	if err != nil {
		writeStoreError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": items})
}

func boundedLimit(c *gin.Context, fallback int) (int, error) {
	if c.Query("limit") == "" {
		return fallback, nil
	}
	limit, err := strconv.Atoi(c.Query("limit"))
	if err != nil || limit < 1 || limit > 100 {
		return 0, fmt.Errorf("limit must be between 1 and 100")
	}
	return limit, nil
}

func writeMutationError(c *gin.Context, err error) {
	if errors.Is(err, runcontrol.ErrCapacityExhausted) {
		c.Header("Retry-After", "5")
		c.JSON(http.StatusTooManyRequests, gin.H{
			"error_code": "CAPACITY_EXHAUSTED", "message": err.Error(), "retryable": true,
		})
		return
	}
	if errors.Is(err, runcontrol.ErrInvalidIdempotency) {
		writeError(c, http.StatusConflict, "IDEMPOTENCY_CONFLICT", err.Error())
		return
	}
	if errors.Is(err, runcontrol.ErrTaskVersionConflict) {
		writeError(c, http.StatusConflict, "TASK_VERSION_CONFLICT", err.Error())
		return
	}
	if errors.Is(err, runcontrol.ErrInvalidRunStatus) {
		writeError(c, http.StatusConflict, "RUN_NOT_RETRYABLE", err.Error())
		return
	}
	writeStoreError(c, err)
}

func writeStoreError(c *gin.Context, err error) {
	if errors.Is(err, runcontrol.ErrNotFound) {
		writeError(c, http.StatusNotFound, "NOT_FOUND", "resource not found")
		return
	}
	if errors.Is(err, runcontrol.ErrInvalidPayload) {
		writeError(c, http.StatusBadRequest, "INVALID_PAYLOAD", err.Error())
		return
	}
	if errors.Is(err, runcontrol.ErrTaskVersionConflict) {
		writeError(c, http.StatusConflict, "TASK_VERSION_CONFLICT", err.Error())
		return
	}
	writeError(c, http.StatusInternalServerError, "STORAGE_ERROR", err.Error())
}

func writeError(c *gin.Context, status int, code, message string) {
	c.JSON(status, gin.H{"error_code": code, "message": message, "retryable": false})
}

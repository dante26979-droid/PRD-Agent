package httpapi

import (
	"errors"
	"fmt"
	"net/http"
	"strconv"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/gin-gonic/gin"
)

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
	api := &Router{store: store}
	router := gin.New()
	router.Use(gin.Recovery())
	router.GET("/api/v1/health/live", api.live)
	router.GET("/api/v1/health/ready", api.ready)
	router.GET("/api/v1/me", api.me)

	group := router.Group("/api/v1")
	group.Use(principalMiddleware(resolver))
	group.GET("/tasks", api.listTasks)
	group.POST("/tasks/from-message", api.startTask)
	group.GET("/tasks/:taskID", api.getTask)
	group.GET("/tasks/:taskID/events", api.taskEvents)
	group.GET("/tasks/:taskID/runs", api.listRuns)
	group.POST("/tasks/:taskID/runs/:runID/stop", api.stopRun)
	return router
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
	run, err := a.store.StopRun(c.Request.Context(), c.GetString("tenant_id"), c.GetString("owner_id"), c.Param("taskID"), c.Param("runID"))
	if err != nil {
		writeStoreError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"run": run})
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
	writeError(c, http.StatusInternalServerError, "STORAGE_ERROR", err.Error())
}

func writeError(c *gin.Context, status int, code, message string) {
	c.JSON(status, gin.H{"error_code": code, "message": message})
}

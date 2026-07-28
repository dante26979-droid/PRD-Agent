# Go 迁移剩余框架设计方案

> 状态：DIRECT RPC CORE IMPLEMENTED / REMAINING FRAMEWORK PROPOSED
> 日期：2026-07-28
> 前置条件：Go Control Plane Step 1、Agent Execution Seam Step 2 已完成核心垂直切片
> 约束：不引入 GORM；PostgreSQL 统一使用 `pgxpool` + 显式 SQL；本地验证不得依赖生产凭证

> 新增架构决策：Go 与 Python Agent 的最终执行链路采用 Go 主动调用 Python 的私有 gRPC；Go 使用有界 goroutine/RPC worker pool 管理连接和并发。Redis 仅保留为唤醒与恢复信号，不再作为 Python Agent 的主消费链路。详细设计见：[Go-Python 直接 Agent RPC 线程池设计](2026-07-28-go-python-direct-agent-rpc-pool-design.md)。

当前已落地 Direct RPC Core：`agent_worker.proto`、Python Worker Server、Go Agent RPC Pool、Go Dispatcher、dispatch 状态表、Maintenance 接入和内存流程测试。Public API 全量兼容、OIDC/mTLS、Capability/Provider、数据投影和 staging cutover 仍按本文后续阶段执行。

## 1. 目标与范围

Step 2 之后，Go 侧还剩下五类框架需要完成：

1. Public API 与 SSE 全量兼容；
2. OIDC、租户授权和 Agent RPC 内部身份认证；
3. Capability Gateway 与 Feishu/GitHub Provider Gateway；
4. Integration/Maintenance、Inbox/Outbox、重试和 reconciliation；
5. 旧 Python 表到 Go-owned Control Plane 的数据投影、验证和切流。

横向改造：Go Agent Dispatcher、Python `AgentWorkerService` 和 direct RPC pool。现有 Python → Go `AgentExecutionService` 作为 Step 2 过渡兼容路径保留，不能与 Go Push 路径同时处理同一 Run。

目标运行形态：

```text
Browser
  → Go Public API / SSE
  → Go Application Services
  → Go-owned PostgreSQL
  → Go Agent Dispatcher / RPC Worker Pool
  → Python AgentWorkerService
  → Go AgentExecutionStore / Capability Gateway
  → Fake 或真实 Feishu/GitHub Provider
```

本方案只设计 Go 框架、接口和本地验证边界，不在本阶段实现具体 Provider 业务逻辑，也不删除旧 Python 代码。

## 2. 总体模块结构

```text
backend-go/
├── cmd/
│   ├── api/                 # Public HTTP + SSE
│   ├── agent-rpc/           # Python Agent private gRPC
│   ├── maintenance/         # scheduler/outbox/recovery/reconciliation
│   └── migrate/             # ordered SQL migrations
├── internal/
│   ├── httpapi/              # transport adapter, schema, middleware
│   ├── identity/             # OIDC, principal, tenant ACL, internal auth
│   ├── application/          # commands/query services
│   ├── projection/           # task detail/list/SSE projections
│   ├── runcontrol/           # Run state, lease, queue, fencing
│   ├── agentexec/             # AgentExecutionService application seam
│   ├── agentpool/              # bounded gRPC connections and worker slots
│   ├── dispatcher/             # Go-owned Run dispatch and recovery
│   ├── capability/            # capability authorization and dispatch
│   ├── provider/
│   │   ├── github/            # repository read capabilities
│   │   └── feishu/            # catalog/source/export capabilities
│   ├── integration/           # sync, webhook, export saga
│   ├── transport/             # wake-up, gRPC pool, HTTP provider clients
│   ├── storage/               # pgx repositories and transactions
│   ├── audit/                 # immutable audit events
│   └── observability/         # logs, metrics, correlation IDs
└── db/
    ├── migrations/
    └── query/
```

### 2.1 Module依赖规则

```text
httpapi → application → {runcontrol, projection, capability, integration}
application → storage interfaces
storage implementation → pgxpool + explicit SQL
agentexec grpc adapter → agentexec Service
capability → provider interfaces
maintenance → application/integration/transport
provider → no HTTP handler and no Python import
```

禁止：

- Handler 直接执行 SQL；
- Python 直接访问 Provider 或 Go-owned PostgreSQL；
- Redis 作为业务事实源；
- Go 复制 Python Prompt 或 Agent Loop；
- Go/Python 同时写同一事实表；
- 使用 ORM 自动迁移或隐式级联写入。

## 3. Public API 与 SSE 框架

### 3.1 HTTP 分层

```text
HTTP Handler
  → Request validation / Principal extraction
  → Application Command or Query
  → Transaction boundary
  → Projection / Event response
```

Handler 只负责：

- JSON、Header、Path、Query 解析；
- OIDC Principal 读取；
- application error 到 HTTP error 的映射；
- SSE flush 和断开处理。

### 3.2 API 兼容范围

Go API 必须覆盖现有 Python 公共接口：

```text
GET  /api/v1/health/live
GET  /api/v1/health/ready
GET  /api/v1/me
GET  /api/v1/tasks
POST /api/v1/tasks/from-message
GET  /api/v1/tasks/{task_id}
GET  /api/v1/tasks/{task_id}/runs
POST /api/v1/tasks/{task_id}/runs/{run_id}/stop
POST /api/v1/tasks/{task_id}/runs/{run_id}/retry
POST /api/v1/tasks/{task_id}/messages
POST /api/v1/tasks/{task_id}/outline/confirm
POST /api/v1/tasks/{task_id}/units/{unit_id}/confirm
POST /api/v1/tasks/{task_id}/revisions/approve
POST /api/v1/tasks/{task_id}/finalize
POST /api/v1/tasks/{task_id}/reopen
POST /api/v1/tasks/{task_id}/exports/feishu/preview
POST /api/v1/tasks/{task_id}/exports/feishu
GET  /api/v1/tasks/{task_id}/exports
GET  /api/v1/tasks/{task_id}/events
```

响应兼容原则：

- 保持现有 JSON 字段名称和枚举值；
- 新字段只允许向后兼容地追加；
- 错误响应保留稳定 `error_code`、`message`、`retryable`；
- 命令请求必须带 `Idempotency-Key`；
- 会改变 Task 的请求必须带 `expected_task_version`；
- Go API 不同步等待 Python Agent 完成，返回已提交的 Run Projection。

### 3.3 Application Command

建议定义：

```go
type StartTaskCommand struct {
    TenantID       string
    OwnerID        string
    Message        string
    IdempotencyKey string
    CorrelationID  string
}

type ConfirmUnitCommand struct {
    TenantID          string
    OwnerID           string
    TaskID            string
    UnitID            string
    ExpectedTaskVersion int
    IdempotencyKey    string
}
```

每个 Command Service 必须在一个事务中完成：

```text
authorize → idempotency lookup → version/state check → state write
→ domain event → outbox → commit
```

### 3.4 SSE 设计

Go 需要建立 Go-owned 事件投影：

```text
go_task_events(
  event_id,
  task_id,
  tenant_id,
  owner_id,
  sequence,
  event_type,
  payload,
  occurred_at
)
```

SSE 必须支持：

- `after_sequence` 查询参数；
- `Last-Event-ID` 头；
- sequence 严格递增；
- 断线重连后从上次 sequence 补发；
- 跨租户 Task 返回 404，不泄露存在性；
- 心跳 comment 防止代理关闭连接；
- 客户端断开后停止轮询；
- event payload 不包含 Secret 和未脱敏 Provider 内容。

### 3.5 Agent 直接 RPC 派发

Public API 创建 Run 后不等待 Python 返回，也不把 Python 作为 Redis Stream 的主消费者。Go Dispatcher 从 PostgreSQL 读取 `QUEUED` Run，取得 Lease 后提交给 bounded Agent RPC pool；pool 调用 Python `AgentWorkerService.ExecuteRun`，以 stream 事件形式接收 Attempt、Evidence、Checkpoint、Draft 和终态。

Go 侧剩余实现必须包含：

- `agent_worker.proto` 及 Go/Python 生成代码；
- `agentpool` 的 WorkerSlot、连接复用、最大并发、熔断、健康检查和 drain；
- `dispatcher` 的 Lease 取得、dispatch attempt、超时、取消和 recovery；
- `go_agent_dispatches` 显式 SQL 表，记录 `UNKNOWN`，避免不确定结果被盲目重放；
- Python Agent Worker server adapter；
- direct RPC E2E、pool saturation、Worker crash 和 stale event 测试。

Go 的 goroutine pool 不是无限并发；默认一个 Python Worker 只执行一个 Run。gRPC 断线采用 at-least-once 语义，所有事件和 dispatch 必须可幂等重放。

## 4. Identity、Authorization 与内部 RPC 安全

### 4.1 Public API

- OIDC issuer discovery + JWKS signature verification；
- `sub` 映射内部 User；
- tenant membership 由 Go 查询，不能信任请求中的 tenant header；
- role/scope 由 Go 统一解析；
- 本地开发才允许 `PRD_AGENT_GO_ALLOW_DEV_PRINCIPAL=true`；
- production 禁止 X-User-ID/X-Tenant-ID 作为身份来源。

### 4.2 Agent RPC

最终方向由当前“Python 调 Go”调整为“Go 调 Python”：

```text
Go Dispatcher
  → bounded gRPC connection pool
  → Python AgentWorkerService.ExecuteRun
  → streamed AgentEvent
  → Go-owned Store
```

现有 `AgentExecutionService` 保留为 Step 2 兼容回调，迁移期间通过 feature flag 二选一。Worker ID 必须来自认证身份或可信服务元数据；请求体中的 Worker ID 只能作为一致性校验值，不得单独作为认证依据。Go pool、Python server 和 mTLS/workload identity 的完整要求见 direct RPC 设计文档。

### 4.3 Capability Authorization

每个 Capability Request 必须同时满足：

```text
valid internal worker identity
valid Run lease/fencing
Run belongs to requested tenant
binding belongs to tenant
revision is immutable
path/prefix is allowed
payload and result size within limit
```

## 5. Capability 与 Provider Gateway

### 5.1 Capability 接口

Python 只调用以下 Go-owned Capability：

```text
ReadRepositoryTree(binding_id, revision, prefix)
ReadRepositoryFile(binding_id, revision, path)
SearchRepository(binding_id, revision, query)
SearchPrdCatalog(query, access_scope)
FetchPrdSections(locator_ids)
CreateExportIntent(task_id, draft_version)
```

Go Capability Service 负责：

- 校验 Run Lease；
- 校验 Provider Binding 和 ACL；
- 调用 Provider interface；
- 限制路径、大小、超时和重试；
- 记录 integration attempt 和 audit event；
- 返回脱敏、固定 revision 的结果。

### 5.2 Provider 接口

```go
type RepositoryProvider interface {
    ReadTree(ctx context.Context, binding Binding, revision string, prefix string) (Tree, error)
    ReadFile(ctx context.Context, binding Binding, revision string, path string) (File, error)
    Search(ctx context.Context, binding Binding, revision string, query string) ([]Hit, error)
}

type FeishuProvider interface {
    FetchRevision(ctx context.Context, binding Binding, revision string) (SourceRevision, error)
    FetchSections(ctx context.Context, locators []Locator) ([]Section, error)
    CreateExport(ctx context.Context, intent ExportIntent) (ExternalResult, error)
}
```

Provider implementation 不得返回内部 OAuth token；credential 只在 Provider process memory 中短暂使用。

### 5.3 结果未知

外部调用必须记录：

```text
integration_call_attempt
request_hash
provider_request_id
started_at / ended_at
result: SUCCEEDED | FAILED | UNKNOWN
```

HTTP timeout 不等于外部操作失败。`UNKNOWN` 必须进入 reconciliation，不允许立即重复写入。

## 6. Integration / Maintenance 框架

Maintenance 分成四个互相独立的循环：

```text
AdmissionReconciler  → WAITING_CAPACITY → QUEUED
DispatchReconciler   → QUEUED → Go Agent RPC Pool
RunRecoveryScanner   → expired lease / stuck run → requeue or fail
ProviderReconciler   → UNKNOWN external call → query provider status
```

`OutboxPublisher` 仍可发布 Redis wake-up，但 Redis 不承载 Agent 执行事实；Dispatcher 必须能在 Redis 清空或不可用时直接扫描 PostgreSQL 恢复 Run。

每个循环必须：

- 有明确 batch size；
- 使用 `FOR UPDATE SKIP LOCKED`；
- 单条失败不阻塞整个 batch；
- 记录 attempt 和耗时；
- 有最大重试次数和 quarantine；
- 可被 context cancellation 停止；
- 不依赖进程内状态恢复业务事实。

## 7. 旧数据投影与切流

### 7.1 阶段

```text
Shadow schema
  → Backfill
  → Projection verification
  → Go read-only
  → One-command Go write ownership
  → Go public API cutover
  → Remove legacy writes
```

### 7.2 数据策略

- 先建立字段映射清单，不直接复用旧 ORM model；
- Backfill 使用显式 SQL、批次和 checkpoint；
- 每个批次记录 source count、target count、hash mismatch；
- 不复制 Published PRD 正文作为新的长期事实；
- Run、Task、Event、Idempotency、Provider Binding 的 owner/tenant 必须逐行验证；
- 投影期间只有旧系统或 Go 其中一个允许写入，禁止双写；
- 切换通过 feature flag 和短暂维护窗口完成；
- 回滚前先停止新写入，再等待 lease 过期，最后切回旧读写入口。

### 7.3 切流门禁

```text
mapping completeness = 100%
tenant/owner mismatch = 0
orphan run = 0
event sequence gap = 0
idempotency replay mismatch = 0
provider binding credential exposure = 0
recovery drill = passed
```

## 8. 可观测性与资源控制

每条链路统一记录：

```text
request_id
correlation_id
tenant_id
task_id
run_id
lease_id
fencing_token
provider_request_id
```

必须有的 metrics：

- HTTP request latency/status；
- Run admission wait time；
- active lease、expired lease、fencing rejection；
- Outbox pending/failed/attempt count；
- Agent RPC latency/error；
- Agent RPC pool active/inflight/saturated/healthy worker；
- dispatch `UNKNOWN`、retry、cancel 和 drain count；
- Provider latency/UNKNOWN ratio；
- SSE reconnect and sequence gap；
- PostgreSQL pool usage。

小服务器约束：

- Python Worker 默认 1 个并发 Run；
- Go Agent RPC pool 不缓存大 payload；
- Provider response 设置最大字节数；
- Outbox 和 recovery 使用小批次；
- LLM 内容保留在 Python，不复制完整正文到日志。

## 9. 实施顺序

1. Agent Worker Protobuf、Python server adapter 和 Go RPC pool；
2. Go Dispatcher、dispatch attempt 表和 direct RPC recovery；
3. Public API Projection 与 SSE 兼容层；
4. OIDC/内部 gRPC 认证拦截器；
5. Fake Provider + Capability Gateway；
6. Integration/Maintenance recovery；
7. 旧表 mapping/backfill/verification；
8. Go API shadow read；
9. 单命令切换和前端 base URL 切换；
10. 删除 Python Pull 生产入口和旧 Python public API 写路径。

每一步都必须保留旧入口回滚能力，但不得引入第二个事实写入方。

# Go 与 Python Agent 直接 RPC 线程池设计方案

> 状态：LOCAL E2E IMPLEMENTED / PRODUCTION GATES PENDING
> 日期：2026-07-28
> 适用范围：Go Control Plane 与 Python Agent Runtime 的执行链路
> 前置条件：Go Agent Execution Seam Step 2 已完成基础协议和租约实现

## 实现情况（2026-07-28）

已完成：

- `agent_worker.proto`、Go bindings 和 Python bindings；
- Python `AgentWorkerServer`、事件流、Worker identity、capacity、CancelRun 和 Health；
- Python 默认 Agent Loop factory、Deterministic/DeepSeek Model Adapter、Capability Client、Checkpoint/Quality/Cancel 门禁；
- Worker event 已补充 `occurred_at`、contract version 和 correlation id，Run Context 已补充 task version 和 Repository capability context；
- Go `agentpool`，包括连接复用、bounded inflight、saturation、drain、cancel 和 generated gRPC client adapter；
- Go `dispatcher`，包括 Lease、dispatch attempt、事件顺序校验、Attempt/Evidence/Checkpoint/Draft 回写、终态处理、`UNKNOWN` 标记和 Lease expiry recovery；
- `go_agent_dispatches` migration 和 PostgreSQL 显式 SQL；
- Maintenance direct dispatch/recovery 接入及 Python Worker Docker/Compose 入口；
- Go/Python 单元流程测试和内存 Control Plane 端到端测试；
- 独立 Compose 下 PostgreSQL、Redis、Go API/Maintenance 与 Python Worker 的真实 TCP E2E；
- 旧 Python Pull/Callback Runtime、Go `agent-rpc` 和 `legacy_worker_pull` 默认配置已删除。

仍需生产门禁：mTLS/workload identity、Capability Gateway 全量实现和 staging 故障演练。

## 1. 架构决策

Go 后端与 Python Agent 采用私有、版本化的 gRPC 直接调用。Go 负责派发，Python 负责执行 Agent Loop，Go 继续拥有 Task、Run、Lease、Evidence、Checkpoint、Draft 和事件事实。

这里的“线程池”在 Go 中实现为有界 goroutine worker pool，而不是为每个任务创建 OS 线程；gRPC `ClientConn` 本身支持并发调用，连接池只用于隔离 Worker、限制并发和故障摘除，不为每个请求重新拨号。

最终链路：

```text
Go Public API
  → PostgreSQL Task/Run/Outbox
  → Go Agent Dispatcher
  → bounded RPC worker pool
  → Python AgentWorkerService.ExecuteRun
  → streamed progress/result
  → Go-owned Run/Attempt/Evidence/Checkpoint/Draft/Event
```

Redis 只用于 Go 进程间唤醒、可丢弃通知和恢复加速，不再作为 Python Agent 的主消费队列。PostgreSQL 才是待派发 Run 的事实源。

## 2. 与当前代码的差异

当前仓库已完成最终派发方向：

1. `AgentWorkerService`：Python 暴露的执行服务；
2. Go `agentpool`：维护 Python Worker endpoint、gRPC 连接和有界并发；
3. Go `dispatcher`：从 PostgreSQL 领取可运行 Run，取得 Lease 后调用 Python；
4. `go_agent_dispatches`：记录 dispatch attempt、request hash 和结果未知状态；
5. Python `AgentWorkerServer`：把当前 `AgentRuntime.run_once` 封装为被 Go 调用的 RPC handler。

执行模式已收敛为：

```text
AGENT_DISPATCH_MODE=go_rpc_pool
```

Python Pull 模式已删除，避免双重 Acquire 和 fencing 竞争。

## 3. RPC 合同

新增 `contracts/proto/agent/v1/agent_worker.proto`，建议使用 server-streaming RPC：

```protobuf
service AgentWorkerService {
  rpc ExecuteRun(ExecuteRunRequest) returns (stream AgentEvent);
  rpc CancelRun(CancelRunRequest) returns (CancelRunResponse);
  rpc Health(WorkerHealthRequest) returns (WorkerHealthResponse);
}
```

### 3.1 ExecuteRun 请求

请求必须包含：

```text
contract_version
request_id
correlation_id
dispatch_id
run_id
worker_id
lease_id
fencing_token
lease_expires_at
run_context
```

Python 不从请求体推导 tenant/owner 权限；Context 中的 tenant/owner 仅用于 Agent 运行上下文和日志关联，权限仍由 Go 在派发前校验。

### 3.2 AgentEvent

事件按顺序发送：

```text
RUN_STARTED
MODEL_ATTEMPT
EVIDENCE_APPENDED
CHECKPOINT_SAVED
DRAFT_SUBMITTED
RUN_COMPLETED | RUN_FAILED
```

每个事件必须带：

```text
dispatch_id
run_id
event_sequence
event_id
occurred_at
payload
```

Go 收到事件后先做 contract、dispatch、lease/fencing 和 payload 校验，再写入 Go-owned Store。重复事件必须安全重放；未知事件进入 quarantine，不得直接修改 Run 状态。

### 3.3 长任务和取消

- `ExecuteRun` 使用 server-streaming，避免用一个大响应承载完整 Agent 结果；
- Go 为每次执行设置最大 deadline，但不使用短 HTTP 请求超时；
- Go 取消 context 时调用 `CancelRun`，并关闭执行 stream；
- Python 收到取消后停止模型/工具循环，不写入成功终态；
- LeaseSupervisor 的最终实现由 Go Dispatcher 负责，Python 只报告进度，不自行发放或递增 fencing token。

## 4. Go RPC Worker Pool

### 4.1 结构

```go
type AgentRPCPool interface {
    Submit(ctx context.Context, job AgentJob) (AgentResult, error)
    Drain(ctx context.Context) error
    Health(ctx context.Context) []WorkerHealth
}
```

每个 `WorkerSlot` 包含：

```text
worker_id
endpoint
grpc.ClientConn
active_count
max_inflight
health_state
last_success_at
last_error_at
```

建议初始配置：

```text
AGENT_RPC_POOL_SIZE=2
AGENT_RPC_MAX_INFLIGHT_PER_WORKER=1
AGENT_RPC_EXECUTE_TIMEOUT=30m
AGENT_RPC_CONNECT_TIMEOUT=5s
AGENT_RPC_DRAIN_TIMEOUT=30s
```

2 vCPU / 2GB 本地或小型部署默认只允许运行 1 个 Python Agent Run；增加 Worker 数量前必须重新评估模型并发、内存和 Provider 限流。

### 4.2 调度流程

```text
1. Dispatcher 从 PostgreSQL 找到 QUEUED Run
2. 从 pool 选择健康且未满载的 WorkerSlot
3. 在 Go 内部取得 Lease/Fencing Token
4. 写入 go_agent_dispatches（唯一 dispatch_id）
5. 调用 Python ExecuteRun
6. 逐事件应用到 Go-owned Store
7. 收到终态后更新 Run、Outbox 和 Task Event
8. 释放 WorkerSlot
```

Pool 满载时不得绕过 Queue Slot，也不得启动无限 goroutine；Run 保持 `QUEUED` 或进入 `WAITING_CAPACITY`，由 Dispatcher 下一轮重试。

### 4.3 连接和生命周期

- 连接在 Worker 注册或配置加载时建立；
- 不在每个任务内 `Dial`；
- gRPC keepalive、backoff 和连接状态由 pool 管理；
- Worker 连续失败达到阈值后熔断并摘除；
- 健康恢复后半开探测，成功后重新加入；
- Go 进程退出时停止领取新 Run，等待现有 stream 完成或取消；
- pool 不缓存完整 PRD、LLM 输出或大文件，只保留小型事件和状态。

## 5. 幂等、重试和故障语义

RPC 网络语义按 at-least-once 处理，不能假设 exactly-once。

### 5.1 Dispatch 记录

新增表 `go_agent_dispatches`：

```text
dispatch_id PRIMARY KEY
run_id
attempt_no
worker_id
request_hash
status: STARTED | RUNNING | SUCCEEDED | FAILED | UNKNOWN
started_at
finished_at
last_error
UNIQUE(run_id, attempt_no)
```

`dispatch_id` 同时进入 RPC metadata 和每个 AgentEvent。Go 在超时、连接断开或进程重启后不能盲目创建新 dispatch；必须先读取当前 Run/dispatch 状态，再决定恢复、等待 Lease 过期或创建下一 attempt。

### 5.2 重试规则

| 情况 | Go 处理 |
| --- | --- |
| 连接建立失败 | 不写成功，记录 `UNKNOWN`，Worker 熔断或退避 |
| Python 明确返回 retryable failure | 结束当前 dispatch，按 Run policy 创建下一 attempt |
| stream 中断且无终态 | 标记 `UNKNOWN`，等待 recovery scanner |
| stale fencing event | 丢弃事件并记录 fencing rejection |
| 重复 event_id/sequence | 幂等确认，不重复写入 |
| Provider 返回未知 | 由 Provider reconciliation 处理，不由 Agent pool 立即重放 |

## 6. 安全边界

- Go → Python 使用 mTLS 或等价的 workload identity；
- Worker ID 来自认证元数据，request body 中的 Worker ID 只做一致性校验；
- Python 不持有 PostgreSQL DSN、Provider OAuth Token 或用户 OIDC Token；
- Python 只接收当前 Run 所需的最小 Context；
- Go 在进入 pool 前校验 tenant、owner、Run、Lease 和 capability binding；
- 所有 RPC metadata 必须包含 contract version、request ID 和 correlation ID；
- payload 禁止写入 API Key、JWT、OAuth Token 和未脱敏 Provider 响应。

## 7. 对剩余 Go 模块的影响

### 7.1 Public API

`POST /tasks/from-message` 只创建 Task/Run 和 durable dispatch intent，不能同步等待 Agent 结果，返回 `202` 或现有兼容状态码。Task 状态通过查询和 SSE 获取。

### 7.2 Maintenance

Maintenance 不再负责“把 Python Worker 从 Redis Stream 拉起来”，而负责：

- 扫描 `QUEUED` Run 并唤醒 Dispatcher；
- 清理过期 Lease 和 `UNKNOWN` dispatch；
- 对没有健康 Worker 的 Run 维持等待状态；
- 处理 PostgreSQL Outbox、重启恢复和 quarantine。

### 7.3 Capability/Provider

Python 仍只能通过 Go-owned Capability Gateway 获取 Repository/PRD 数据；Agent RPC pool 不直接访问 Provider，也不传递 Provider credential。

### 7.4 数据投影与切流

切流前必须确保旧 Python Pull 路径完全停止；不能通过“双写 + 两套 Agent 调度”验证。Shadow read 可以保留，Agent execution 只能有一个 owner。

## 8. 实施顺序

1. 新增 `agent_worker.proto` 和跨语言生成代码；
2. Python 实现 `AgentWorkerService` server adapter，先复用现有 Agent Loop；
3. Go 实现 `agentpool`、Worker health 和 bounded dispatcher；
4. 新增 `go_agent_dispatches` 迁移和 recovery scanner；
5. 增加 Go Push → Python ExecuteRun 本地 E2E；
6. 将 Redis 从主消费链路降级为唤醒/恢复信号；
7. 灰度启用 `AGENT_DISPATCH_MODE=go_rpc_pool`；
8. 删除 Python Pull 生产入口和旧 AgentExecutionClient 运行时依赖；
9. 再继续 Reply/Confirm/Export、Provider、OIDC 和旧表切流。

## 9. 完成标准

- Go pool 不发生无限 goroutine、无限连接或无界请求体增长；
- 一个 Run 同时最多绑定一个有效 dispatch/lease；
- RPC 中断后不伪造成功，recovery 可以继续处理；
- 重复事件、重复 dispatch、stale fencing 都有可验证行为；
- Python 无数据库写权限和 Provider credential；
- Redis 停止时，已有/新建 Run 仍可由 PostgreSQL Dispatcher 恢复；
- pool saturation、Worker crash、Go restart、RPC timeout、Cancel 和 drain 均有测试覆盖；
- direct RPC 本地 E2E 和数据投影已通过；mTLS 与故障演练通过后方可生产切流。

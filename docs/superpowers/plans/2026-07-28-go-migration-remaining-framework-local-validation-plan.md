# 剩余 Go 框架本地验证方案

> 状态：DIRECT RPC CONTAINER E2E PASSED / PRODUCTION GATES PENDING
> 日期：2026-07-28
> 对应设计：`2026-07-28-go-migration-remaining-framework-design.md`
> 原则：本地验证使用 Fake Provider 和本地 OIDC/JWKS，不使用生产数据库、API Key、OAuth Token 或真实用户数据

> Agent 执行链路采用 [Go-Python 直接 Agent RPC 线程池设计](2026-07-28-go-python-direct-agent-rpc-pool-design.md)：Go Dispatcher 主动调用 Python Worker；Redis 只做 wake-up/recovery，不作为 Python Agent 主消费队列。

## 1. 本地验证拓扑

```mermaid
flowchart LR
    Browser["curl / local web"] --> API["go-api :8080"]
    API --> PG[("postgres :5432")]
    API --> Dispatcher["Go Dispatcher + RPC Pool"]
    Dispatcher --> Worker["python AgentWorkerService :9100"]
    Dispatcher --> PG
    API --> Redis["redis :6379 wake-up"]
    Maintenance["go-maintenance"] --> PG
    Maintenance --> Redis
    Worker --> LLM["Fake/Stub LLM"]
    Dispatcher --> Capability["go capability gateway"]
    Capability["go capability gateway"] --> FakeProvider["fake provider :18080"]
```

本地服务：

| 服务 | 来源 | 端口 | 是否必须 |
| --- | --- | --- | --- |
| PostgreSQL | `postgres:16-alpine` | 5432 | 是 |
| Redis | `redis:8-alpine` | 6379 | 是 |
| Go API | `backend-go/cmd/api` | 8080 | 是 |
| Go Dispatcher + RPC Pool | `backend-go/internal/dispatcher` + `agentpool` | embedded in Go | 是 |
| Python Agent Worker | `agent-python` `AgentWorkerService` | 9100 | 是 |
| Go Maintenance | `backend-go/cmd/maintenance` | 无 | 是 |
| Python Worker | `agent-python` | 无 | 是 |
| Fake OIDC/JWKS | local test fixture | 18081 | 认证验证时必须 |
| Fake Provider | local test fixture | 18080 | Gateway 验证时必须 |

核心容器链路使用 `infra/local/docker-compose.go.yml`，一键验证命令：

```bash
scripts/validate-go-agent-local.sh
```

验证器创建真实 Task/Run，并断言 Run 为 `SUCCEEDED`、Task version 为 2，
以及 `MODEL_ATTEMPT`、`CHECKPOINT_SAVED`、`DRAFT_SUBMITTED`、`RUN_COMPLETED`
四类投影事件均可从公共 SSE API 查询。

## 2. 本地环境约束

### 2.1 禁止读取的值

本地测试不得读取：

- `PRD_AGENT_LLM_API_KEY`；
- Feishu App Secret；
- GitHub PAT/OAuth Refresh Token；
- 生产 PostgreSQL DSN；
- 生产 OIDC issuer；
- 真实用户消息、PRD 正文或仓库内容。

### 2.2 本地替代值

```text
DATABASE_DSN=postgres://prd_agent:prd_agent@postgres:5432/prd_agent
BROKER_URL=redis://redis:6379/0
OIDC_ISSUER=http://fake-oidc:18081
OIDC_AUDIENCE=prd-agent-local
PROVIDER_BASE_URL=http://fake-provider:18080
AGENT_EXECUTION_BACKEND=go
PRD_AGENT_GO_AGENT_DISPATCH_MODE=go_rpc_pool
PRD_AGENT_GO_AGENT_RPC_WORKER_ENDPOINTS=python-agent:9100
PRD_AGENT_GO_AGENT_RPC_MAX_INFLIGHT_PER_WORKER=1
PRD_AGENT_GO_AGENT_DISPATCH_BATCH_SIZE=10
PRD_AGENT_GO_AGENT_RPC_CONNECT_TIMEOUT_SECONDS=5
PRD_AGENT_GO_AGENT_RPC_EXECUTE_TIMEOUT_SECONDS=1800
PRD_AGENT_AGENT_LOOP_FACTORY=your.module:build_loop
```

开发 header 只允许在显式 local profile 使用；OIDC/JWKS 测试必须覆盖 production 配置下 header 被拒绝。

## 3. 本地验证阶段

### Gate 0：静态与依赖验证

```bash
cd backend-go
gofmt -w .
go test ./...
go test -race ./internal/...
go vet ./...

cd ../contracts
buf lint
buf generate
go test ./...
```

验收：

- 无 GORM 依赖；
- bindings 可重新生成；
- Go/Python contract 文件来自同一 Proto；
- 生成文件不是手工修改的唯一来源。

### Gate 1：数据库迁移与健康检查

```bash
docker compose -f infra/local/docker-compose.go.yml up -d postgres redis
go run ./backend-go/cmd/migrate
go run ./backend-go/cmd/api
```

检查：

```bash
curl -fsS http://127.0.0.1:8080/api/v1/health/live
curl -fsS http://127.0.0.1:8080/api/v1/health/ready
```

验收：

- migration 按 `0001 → 0002 → 0003 → 0004` 顺序完成；
- 重复执行 migration 不产生错误；
- DB 不存在隐式 ORM 表；
- readiness 在 PostgreSQL 不可用时返回 503。
- `agent_worker.proto` 可重新生成 Go/Python bindings，且双方使用同一 `contract_version`。

### Gate 2：Task/Run 与 Admission

使用本地开发身份：

```bash
curl -X POST http://127.0.0.1:8080/api/v1/tasks/from-message \
  -H 'Content-Type: application/json' \
  -H 'X-Tenant-ID: tenant-local' \
  -H 'X-User-ID: user-local' \
  -H 'Idempotency-Key: start-1' \
  -d '{"message":"本地验证需求"}'
```

验证：

1. 相同幂等键相同请求返回同一 Task/Run。
2. 相同幂等键不同消息返回 409。
3. 全局或用户容量达到上限时进入 `WAITING_CAPACITY`。
4. Run 完成后 Maintenance 可提升等待 Run。
5. 跨 owner 读取返回 404。

### Gate 3：Agent Execution E2E

启动 Go API/Dispatcher、Python `AgentWorkerService` 和 Maintenance，验证：

```text
Go API creates QUEUED Run
      → Go Dispatcher selects WorkerSlot
      → Go acquires Lease/Fencing Token
      → Go RPC pool calls Python ExecuteRun
      → Python streams Attempt/Evidence/Checkpoint/Draft
      → Go applies Go-owned writes
      → CompleteRun / FailRun
```

检查 PostgreSQL：

- Run 状态正确进入 `RUNNING → SUCCEEDED`；
- checkpoint sequence 单调递增；
- 同一 attempt_key 不重复；
- evidence 去重；
- Task version 按 Draft Patch 递增；
- Go-owned 表由 Go 写入，Python 没有 DB DSN。
- 一个 WorkerSlot 同时最多执行一个 Run；pool 满载时不会无限创建 goroutine；
- Redis 停止时，Dispatcher 仍可直接扫描 PostgreSQL 继续派发。

### Gate 4：SSE 与 API 兼容

用固定 fixture 对比旧 Python API 与 Go API：

- JSON 字段名称和枚举值；
- 201/202/400/404/409/503 状态码；
- Idempotency replay；
- `Last-Event-ID` 断线重连；
- sequence gap 检测；
- 跨租户访问不泄露资源存在性。

建议用同一组请求录制两份响应，只比较允许变化的 ID、时间和 correlation 字段。

另外验证 direct RPC：Go 主动调用 Python，Python 不再从 Redis 拉取同一 Run；Step 2 的 Python → Go compatibility callback 只能在 legacy mode 使用。

### Gate 5：OIDC 与内部 RPC 安全

Fake OIDC/JWKS 生成三类 token：

1. 合法用户、合法 tenant；
2. 签名错误或过期 token；
3. 合法用户但无目标 tenant membership。

验证：

- 合法 token 可以访问授权 Task；
- 无 token 返回 401；
- 无 tenant membership 返回 403 或 404，按 API 合同确定；
- production 模式拒绝 X-User-ID/X-Tenant-ID；
- Go → Python Agent RPC 无有效内部身份时拒绝；
- request body Worker ID 与认证身份不一致时拒绝；
- Python 不能凭 request body 自行获取租约或修改 fencing token；
- Python stream 中断后 dispatch 进入 `UNKNOWN`，不能伪造成功。

### Gate 6：Fake Provider Gateway

Fake Provider 必须支持可控响应：

```text
200 success
401 credential rejected
403 binding denied
404 revision/path missing
429 rate limited
500 provider error
timeout / connection reset
success then response lost
```

验证：

- revision/path ACL 在 Go 侧执行；
- Python 只能拿到 Capability 结果，拿不到 credential；
- 429 按退避重试；
- timeout 被标记为 `UNKNOWN`，进入 reconciliation；
- success-then-response-lost 不会盲目重复创建 Feishu 文档；
- provider request hash 和 audit event 可关联到 run_id。

### Gate 7：故障演练

| 故障 | 注入方式 | 预期结果 |
| --- | --- | --- |
| Redis 清空 | `redis-cli FLUSHDB` | Go Dispatcher 仍能直接扫描 PostgreSQL 恢复 Run |
| Worker 崩溃 | kill Python process | Lease 过期，新 Worker 用更高 fencing 接管 |
| Go API 重启 | restart API | Task/Run/事件不丢失 |
| Agent Worker 不可用 | stop Python `AgentWorkerService` | Run 保留可恢复状态，不伪造成功 |
| Model timeout | Fake LLM delay | `FailRun(retryable=true)` |
| stale worker 写入 | 延迟旧 Worker request | 返回 `LEASE_LOST`，事实不变 |
| Provider timeout | Fake Provider delay | `UNKNOWN` + reconciliation |
| DB transaction abort | kill connection during write | 无半条 Draft/Evidence/Outbox |
| SSE disconnect | close client socket | Last-Event-ID 后续补发 |
| RPC pool 满载 | 设置 `MAX_INFLIGHT=1` 并提交多个 Run | 超出部分保持排队，不创建无限 goroutine |
| Agent RPC stream 中断 | kill Python Worker connection | dispatch=`UNKNOWN`，recovery 后再决定重试 |
| Go drain | stop Go API/Dispatcher | 停止领取新 Run，已有 stream 按 deadline 完成或取消 |

### Gate 8：数据投影与切流演练

使用脱敏 fixture 导入旧 Python 表：

1. 执行 mapping/backfill；
2. 记录 source/target counts；
3. 对 Task、Run、Event、Idempotency、Binding 做 hash 校验；
4. 启用 Go shadow read；
5. 比较 Python/Go projection；
6. 切换单个命令到 Go；
7. 回滚并确认旧入口可用；
8. 清理 fixture 数据。

任何 tenant/owner mismatch、event sequence gap、idempotency replay mismatch 都必须阻止切流。

## 4. 本地自动化脚本建议

建议新增：

```text
infra/local/docker-compose.go.yml
infra/local/fake-oidc/
infra/local/fake-provider/
infra/local/fake-agent-worker/
scripts/validate-go-migration.sh
scripts/validate-go-failure-drills.sh
tests/contract/
tests/e2e_go_migration/
tests/e2e_go_agent_rpc_pool/
```

`validate-go-migration.sh` 应按 Gate 0–6 顺序执行；故障演练单独执行，避免普通 PR 测试被故障注入污染。

## 5. 本地验证通过标准

- Gate 0–6 在无生产凭证环境全部通过；
- Gate 7 每个故障场景都有可观察恢复结果；
- Gate 8 的数据校验零 mismatch；
- Go race test 无数据竞争；
- API compatibility fixture 无非预期 schema 差异；
- Fake Provider 无未授权调用；
- Direct Agent RPC 的 pool saturation、stream 中断、cancel、drain 和恢复测试通过；
- 测试输出不包含 Secret、JWT、OAuth Token 或真实业务正文；
- 所有容器可以停止并重新启动，业务事实从 PostgreSQL 恢复；
- 只有在本地验证通过后，才进入 staging 灰度，不直接切生产。

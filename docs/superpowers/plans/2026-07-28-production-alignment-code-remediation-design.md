# PRD Agent 按新总设计方案的代码修复设计

> 状态：PROPOSED
> 日期：2026-07-28
> 适用基线：`f7ae861 feat: implement m0 agent core steps 3-10`
> 上位设计：`2026-07-28-feishu-github-rag-queue-architecture-design.md`
> 相关 ADR：`0001-external-systems-own-content.md`、`0002-remote-llm-only-in-production.md`、`0003-bounded-fair-queue-for-small-server.md`

## 1. 目标与非目标

### 1.1 目标

把当前代码从“FastAPI 请求内同步执行 Workflow”收敛到以下生产主路径：

```text
API 命令
  -> PostgreSQL 短事务
  -> Run Admission / Queue Slot
  -> Outbox
  -> Agent Worker / Integration Worker
  -> Checkpoint / Model Attempt / Evidence
  -> SSE 或轮询
```

同时完成以下设计约束：

- Feishu 是 Published PRD 正文的权威源；GitHub 是代码的权威源。
- PostgreSQL 是 Control Plane、目录、摘要、定位器和审计事实源，不再持续保存历史 PRD 全文。
- Redis 只做 Broker、唤醒和短期缓存，不承担容量事实和任务完成事实。
- 生产只允许远程 LLM API，API 进程不持有 LLM Secret。
- 所有外部副作用可幂等、可恢复、可审计，并受 Lease/Fencing 保护。

### 1.2 非目标

- 不引入 LangChain 或 LangGraph 重写内层 Agent Loop。
- 不增加本地推理模型、独立向量数据库或第二个搜索集群。
- 不在本轮实现多 Agent 并行执行。
- 不修改已经满足 V1.1 语义的领域节点，只为接入持久化执行边界做最小调整。

## 2. 当前代码缺口与修复对应关系

| 当前缺口 | 主要位置 | 修复 Step |
| --- | --- | --- |
| HTTP 请求直接执行模型和 Workflow | `api/app.py`、`workflow/service.py` | R3、R4 |
| Agent Worker 只有队列骨架，没有生产 handler | `production/queueing.py`、`production/publisher.py` | R3 |
| 没有 Queue Slot、公平调度和容量等待状态 | `production/dispatch.py`、`production/postgres_dispatch.py` | R2 |
| `prd_tasks` 只有 owner_id，缺少 tenant 隔离 | `domain/entities.py`、`infra/local/schema.sql` | R1 |
| repository_snapshots 被重复定义 | `infra/local/schema.sql` | R0、R1 |
| 历史 PRD 全文写入 PostgreSQL | `historical/ingest.py`、`storage/postgres_historical.py` | R6 |
| DeepSeek 调用无 Model Attempt 持久化 | `model_api/deepseek.py`、`model_api/models.py` | R4 |
| Feishu 导出未完全移出 API | `export/service.py`、`api/app.py` | R5 |
| Compose 缺少 Web、Worker、Ingress 和完整认证链路 | `infra/production/docker-compose.yml` | R7 |
| Idempotency-Key 每次重试都会重新生成 | `web/lib/api/client.ts` | R8 |
| SSE 无 sequence gap 和可靠恢复 | `web/components/task-workbench.tsx` | R8 |

## 3. 实施规则

每个 Step 必须遵循：

1. 先增加兼容结构，再切换读路径，最后删除旧路径。
2. 每个最小提交都能运行现有测试并保留一个可回滚状态。
3. 数据库长事务内不得调用 LLM、GitHub、Feishu 或 Redis。
4. Worker 结果提交必须校验 `run_id + lease_id + fencing_token`。
5. 外部系统 Adapter 不得直接修改领域状态，必须通过应用层命令和持久化 Attempt。
6. 测试验证外部行为、状态迁移、权限边界和故障恢复，不绑定内部函数调用次数。

## 4. 分 Step 代码修复方案

## R0：建立基线和安全迁移边界

### 代码修改

- 建立新的生产迁移目录和迁移运行器，禁止继续把 `schema.sql` 作为升级机制。
- 为两套 `repository_snapshots` 定义拆分后的正式表名：
  - `code_repository_snapshots`
  - `remote_repository_snapshots`
- 增加 migration version、checksum 和启动时的 schema compatibility 检查。
- 增加 fresh database、upgrade database、重复执行 migration 测试。
- 将当前未提交的代码与文档变更整理成可审计提交后再开始结构迁移。

### 最小提交序列

1. 增加 migration runner 和 version table。
2. 增加重复 Snapshot 定义的兼容迁移。
3. 增加 migration integration tests。
4. 增加部署前 dirty-worktree / migration gate。

### 完成标准

- 空数据库和已有数据库都能成功迁移。
- migration 重跑不会产生重复表或静默跳过结构。
- 失败迁移不会把应用启动到半兼容状态。

## R1：统一 Tenant、User 和 Provider Binding

### 代码修改

- 在 Task、Run、Working Draft、Evidence、Export、Integration Sync 上增加 `tenant_id`。
- 扩展领域实体和仓储方法，使 tenant 由 `UserPrincipal` 传入，禁止从客户端 payload 接收。
- 所有读取、更新、删除查询同时约束 tenant 和资源标识。
- 将生产默认 Repository Provider 改为显式 Binding；禁止使用本地项目目录、固定 prefix 或 `HEAD` 作为生产默认值。
- 将代码快照与远程 PRD 快照分成不同模块和存储模型。

### 最小提交序列

1. 增加 tenant 列、索引和兼容回填。
2. 扩展 UserPrincipal 到 application service 的调用链。
3. 给核心仓储查询增加 tenant 条件。
4. 增加 Provider Binding 和 immutable revision。
5. 删除生产路径的本地仓库默认值。

### 测试

- 跨租户读取、更新、SSE、导出均返回不可见或 not found。
- 同一仓库不同 commit 形成不同 Snapshot。
- 缺少生产 Provider Binding 时安全失败。

## R2：实现 Run Admission、Queue Slot 和公平调度

### Schema

增加以下控制面表或等价模型：

- `queue_slots`
- `user_run_usage`
- `scheduler_cursors`
- `run_admission_decisions`

为 `agent_runs` 增加：

- `WAITING_CAPACITY`
- `WAITING_PROVIDER`
- `lease_id`
- `fencing_token`
- `queue_position` 或可重建的排序字段

### 代码修改

- 在创建 Run 的同一事务内完成额度检查、Queue Slot 创建、Run 创建和 Outbox 创建。
- 在 PostgreSQL 中实现用户轮转 + 用户内 FIFO 调度。
- 保留 Redis 作为唤醒机制；Redis 丢失后由 Maintenance 从 PostgreSQL 重建信号。
- 明确 `WAITING_USER` 是否释放执行 slot，避免状态含义在 Worker 与 API 中不一致。

### 最小提交序列

1. 增加 Queue Slot schema 和 repository。
2. 增加原子 admission service。
3. 增加用户轮转 scheduler。
4. 增加容量等待和 provider 等待状态。
5. 接入 Outbox 与 Redis wake-up。

### 完成标准

- 并发创建不会超卖全局或用户额度。
- 单用户不能占满全部 runnable slot。
- Redis 清空后任务仍可恢复。
- 等待容量的任务不会被 Worker 误领取。

## R3：把 API 命令切换为异步 Run

### 代码修改

- `start/reply/confirm/retry/export` API 只执行鉴权、输入校验、短事务和事件返回。
- 新增 Agent Worker 生产入口，统一加载 Workflow、Model Adapter、Tool Registry 和 Store。
- 新增 Integration Worker 生产入口，隔离 Feishu/GitHub 写入和同步。
- 将 `WorkflowService` 拆成：
  - Command Handler：创建或修改 Run。
  - Run Executor：在 Worker 中执行一次可恢复步骤。
  - Checkpoint Repository：保存恢复点。
- API 不再直接调用 `invoke_validated`、DeepSeek、GitHub 或 Feishu。
- Worker 只根据 `run_id` 和 checkpoint 恢复，不从请求 payload 重建运行上下文。

### 最小提交序列

1. 增加 Worker entrypoint，但先执行一个 no-op handler。
2. 将 Start 命令改为 Run + Outbox，保留旧同步 executor 作为显式 local-only 兼容开关。
3. 将 Agent Loop 接入 Worker。
4. 将 Reply/Confirm/Retry 接入同一异步执行模型。
5. 删除 staging/production 的同步兼容路径。

### 完成标准

- API P95 不随 LLM 响应时间增长。
- Worker 停止时 API 仍可创建和排队任务。
- Worker 重启后可以从 checkpoint 继续。
- HTTP 重试不会重复创建 Run 或重复执行命令。

## R4：持久化 Model Attempt 和可恢复 LLM Loop

### Schema

增加 `model_attempts`，至少包含：

- `run_id`
- `attempt_key`
- `operation`
- `prompt_version`
- `provider_binding`
- `request_hash`
- `status`
- `response_metadata`
- `token_usage`
- `error_category`
- `started_at`、`completed_at`

### 代码修改

- 扩展 Model Adapter 的调用上下文：operation、attempt key、user identity 和 timeout。
- DeepSeek Client 只由 Agent Worker 构造；API 不挂载 Secret。
- 在调用前创建 Attempt，在返回后用 fencing token 更新 Attempt。
- 对 timeout、429、网络失败、非法 JSON、schema mismatch 分类处理。
- 使用稳定 attempt key 做重试去重；已成功的 Attempt 不重复发起远程请求。
- 保持 Model Loop 的轮次、Token、Tool 调用和无进展预算。

### 最小提交序列

1. 增加 Model Attempt schema 和 repository。
2. 为同步 DeepSeek 调用增加 Attempt 记录。
3. 将 Attempt 记录迁移到 Agent Worker。
4. 增加恢复扫描和超时 Attempt reconciliation。
5. 从 API 容器移除 LLM Secret。

### 完成标准

- 每一次模型调用均可按 run、operation、attempt key 审计。
- Worker 崩溃不会丢失模型调用状态。
- 重试不会无限循环，也不会重复成功调用。
- 旧的规则模型只允许 local/test 使用。

## R5：实现 Integration Worker 和 Feishu 写入 Saga

### Schema

增加或扩展：

- `integration_syncs`
- `integration_call_attempts`
- `export_intents`
- `provider_revisions`
- `inbox_messages`

### 代码修改

- Export Service 只创建 Export Intent，不直接执行 Feishu 写入。
- Integration Worker 领取 Intent，获得 lease，调用 Feishu Adapter，再提交结果。
- 为 create/update/confirm/publish 统一生成 request key 和 request hash。
- 对远端成功但响应丢失的情况进入 `RESULT_UNKNOWN`，先 reconcile 再决定是否重试。
- Webhook 通过 Inbox 去重、去抖，并转为 Integration Sync。
- 外部 Provider Adapter 不直接更新 Task 状态。

### 完成标准

- 重复导出不会重复创建文档。
- Feishu 网络超时不会触发盲目重试。
- 版本冲突和外部覆盖进入明确的冲突状态。
- Integration Worker 与 Agent Worker 可独立重启。

## R6：切换到 Feishu/GitHub RAG 主路径

### Schema

增加或调整：

- `prd_catalog_entries`
- `prd_source_revisions`
- `prd_section_locators`
- `retrieval_index_versions`
- `repository_bindings`

### 代码修改

- Feishu Adapter 负责按 catalog entry 和 revision 读取候选原文。
- PostgreSQL 只保存摘要、关键词、revision、hash、locator 和受控检索元数据。
- Retrieval 先进行 tenant/ACL 过滤，再执行候选排序。
- 排序显式考虑文档状态和最后修改时间，禁止废弃文档仅因最近编辑而升权。
- 旧 `historical/ingest.py` 和 `postgres_historical.py` 保留为迁移/评估工具，不再写入生产主表。
- 通过双读指标完成切换：旧 Corpus 与新 Feishu RAG 并行比较，稳定后关闭旧读路径。

### 完成标准

- Feishu revision 更新可触发目录增量更新。
- 无权限文档不会成为 Retrieval Hit。
- Agent 能得到候选 PRD 的 source revision 和 section locator。
- 生产代码不再新增历史 PRD 全文副本。

## R7：完成生产部署和资源边界

### 代码与部署修改

- `docker-compose.yml` 增加 Static Web、OIDC Proxy、Agent Worker、Integration Worker。
- 将 Publisher、Scheduler、Recovery Scanner 合并为单一 Maintenance 进程。
- API 只加入内部网络；公网只开放 TLS Ingress。
- Secret 只挂载到 Agent Worker 或 Integration Worker 的必要容器。
- 为 API、Worker、PostgreSQL、Redis 设置连接池、内存和并发上限。
- 增加 readiness：migration version、数据库连接、Redis 可选可用性、Provider 配置。
- 增加 queue depth、lease expiry、model latency、integration unknown result 指标。

### 完成标准

- 空服务器可以完成部署、迁移和回滚。
- API、SSE、静态 Web 通过同源认证链路工作。
- Redis 重启不造成业务事实丢失。
- PostgreSQL 有异机备份和恢复演练记录。

## R8：修复前端异步状态和命令幂等

### 代码修改

- Workbench 展示完整 Brief，包括 `open_questions`。
- Reopen 改为选择 Confirmation Unit 和填写 reason，不再自动提交全部 unit。
- API Client 为同一业务命令持久化并复用 Idempotency-Key。
- SSE 增加 `Last-Event-ID`、sequence gap 检测、断线重连和 terminal state 收口。
- SSE 恢复失败时采用带退避的状态查询，不永久关闭实时更新。
- API 类型继续从 OpenAPI 生成，并增加离线 schema drift 检查。
- 增加前端 lint、异步命令 E2E 和断线恢复测试。

### 完成标准

- 浏览器刷新、重复点击、响应丢失都不会重复执行命令。
- SSE 断线后可以恢复到最新事件或最终状态。
- 前端只根据公开事件映射渲染，不依赖内部事件 payload。

## R9：发布验收与旧路径下线

### 验收矩阵

- fresh install 与 upgrade migration。
- 跨租户读取、更新、SSE 和导出隔离。
- 并发 admission、用户公平性和 Queue Slot 不超卖。
- Worker kill/restart、lease expiry、fencing rejection。
- Redis 重启和 Outbox 重复投递。
- DeepSeek timeout、429、非法 JSON 和响应丢失。
- Feishu create/update 超时、Webhook 重复和 revision 冲突。
- GitHub commit 固定和代码读取权限。
- SSE gap、Last-Event-ID 恢复和浏览器刷新。
- 2 vCPU / 2GB 环境 24 小时稳定性与资源峰值。

### 下线条件

只有以下条件全部满足，才删除兼容代码：

1. API 同步 Workflow 路径已无 staging/production 调用方。
2. Agent Worker 的 Model Attempt 和 checkpoint 恢复测试通过。
3. Feishu/GitHub RAG 双读结果达到约定一致性阈值。
4. 所有导出调用都经过 Integration Worker。
5. 迁移和备份恢复已验证。

## 5. 代码模块职责

| 模块 | 责任 | 不负责 |
| --- | --- | --- |
| API Command Handler | 鉴权、校验、短事务、命令幂等 | LLM、长流程、外部写入 |
| Run Admission | 租户/用户配额、Queue Slot、公平顺序 | 执行模型或工具 |
| Agent Worker | Lease、Workflow、Model Attempt、Checkpoint | Feishu 写入 |
| Integration Worker | Feishu/GitHub 同步、写入 Saga、Webhook reconciliation | 生成 PRD 内容 |
| Model Adapter | Provider HTTP、超时、错误分类、脱敏 | Workflow 状态迁移 |
| Retrieval Module | 目录召回、权限过滤、定位器解析 | 保存 Published PRD 全文 |
| PostgreSQL Control Plane | 任务、运行、租户、队列、审计事实 | 充当 Feishu/GitHub 内容权威源 |
| Redis Adapter | Broker、唤醒、短期缓存 | 额度、顺序、完成事实 |
| Web Workbench | 命令提交、事件恢复、状态展示 | 自己推断 Workflow 状态 |

## 6. 测试决策

每个 Step 必须先补外部行为测试，再替换实现。测试重点如下：

- Repository：迁移、租户过滤、并发锁、幂等唯一约束。
- Admission：用户公平性、全局容量、Redis 丢失恢复。
- Worker：lease、heartbeat、fencing、checkpoint 恢复。
- Model Adapter：超时、429、非法响应、attempt 去重和 Secret 脱敏。
- Integration：远端成功/失败/未知、重复 webhook、版本冲突。
- Retrieval：ACL、revision、新鲜度和废弃文档排序。
- API：短事务、异步返回、命令重试和状态映射。
- Web：SSE gap、Last-Event-ID、断线重连、重复点击。
- Deployment：空环境启动、readiness、资源上限和恢复演练。

现有 Python 测试作为回归基线；新增测试优先放在对应模块的 `tests/` 目录，前端沿用 Vitest，并补充 Playwright E2E。禁止只通过 mock 验证 Worker 成功，至少需要一组 PostgreSQL 集成测试和一组故障注入测试。

## 7. 推荐最小提交序列

每个提交都应保持测试可运行：

1. `chore: add migration version and schema compatibility gate`
2. `fix: split repository snapshot ownership models`
3. `feat: propagate tenant scope through task and run storage`
4. `feat: add postgres-backed queue slots and admission`
5. `feat: add fair scheduler and capacity waiting states`
6. `refactor: add production agent worker entrypoint`
7. `refactor: move start command to outbox-backed async run`
8. `feat: persist model attempts and retry outcomes`
9. `refactor: isolate llm secret to agent worker`
10. `feat: add integration worker and export intents`
11. `feat: add feishu reconciliation and webhook inbox`
12. `feat: add source revision catalog and locator-based retrieval`
13. `chore: add production web, oidc proxy and worker services`
14. `fix: make web commands idempotent across retries`
15. `fix: recover sse streams from sequence gaps`
16. `test: add concurrency, failure injection and deployment gates`
17. `chore: remove synchronous production path and legacy corpus writes`

## 8. Out of scope

- 多 Agent 并行调度。
- 本地 LLM 或本地 Embedding 服务。
- 独立向量数据库、Kafka、Kubernetes 和自动扩缩容。
- Feishu/GitHub 权限系统本身的替换。
- 重新设计 V1.1 领域 Workflow 的业务语义。

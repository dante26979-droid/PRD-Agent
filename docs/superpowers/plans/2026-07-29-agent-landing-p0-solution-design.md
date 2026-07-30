# PRD Agent 少量用户稳定运行与 Agent 落地 P0 完整解决方案

> 状态：PROPOSED / READY FOR REVIEW  
> 日期：2026-07-29  
> 目标：少量用户稳定使用，后端可靠承接并发请求，单 Agent 可恢复地产出并发布 PRD  
> 部署档位：单机 Linux，2 vCPU / 2GB RAM / 40GB SSD  
> 身份边界：轻量代理认证，不要求企业 OIDC  
> Agent 并发：固定 `1`；多用户请求通过 PostgreSQL 公平排队  
> 飞书边界：复用已配置且具备 Docx/Wiki 写权限的应用，首版只写固定 Wiki Docx  
> 发布规则：本文 P0 Gate 全部通过后，才允许少量用户 Staging/Production

关联约束：

- `CONTEXT.md`
- `docs/adr/0001-external-systems-own-content.md`
- `docs/adr/0002-remote-llm-only-in-production.md`
- `docs/adr/0003-bounded-fair-queue-for-small-server.md`
- `2026-07-29-p0-server-deployment-closure-design.md`
- `2026-07-28-go-python-agent-boundary-migration-design.md`
- `2026-07-28-go-python-direct-agent-rpc-pool-design.md`

本文是当前阶段的 P0 实施基线。原服务器部署闭环方案继续作为长期安全和企业化
目标，但其中 OIDC、企业多租户、通用 Provider 平台、复杂 PRD Catalog 和大规模
数据切流不再阻塞本阶段 Agent 落地。

---

## 0. 执行结论

当前 P0 只解决一个核心问题：

> 少量用户能够稳定提交 PRD Task；后端在并发请求下不丢状态；一个 Python Agent
> 按公平队列逐个执行；任何模型、进程、网络或飞书失败都有持久、可恢复、可解释的
> 结果；成功的 Working Draft 可以经用户确认后写入已配置的飞书 Wiki Docx。

P0 必须完成七个闭环：

1. **轻量认证闭环**：HTTPS 入口认证少量用户，Go 不信任浏览器自由声明身份；
2. **并发与排队闭环**：API 并发、Agent 串行，PostgreSQL 提供容量、公平性和背压；
3. **Agent 执行闭环**：Model Attempt、Evidence、Checkpoint、Draft 和终态逐事件提交；
4. **中断恢复闭环**：Worker/API/Maintenance 重启、RPC 中断和取消都从持久状态恢复；
5. **结果交付闭环**：最小 Web/API 能查看状态、事件、Draft、Evidence、失败和重试；
6. **飞书发布闭环**：固定 Wiki Docx 的 Preview、确认、Revision 校验、写入和未知结果；
7. **部署运维闭环**：空服务器部署、Secret、TLS、备份、观测、故障注入和长稳。

P0 不追求：

- 同时执行多个 Agent Run；
- 企业 OIDC、RBAC 或复杂 Tenant Membership；
- 任意飞书空间、任意文档和通用 OAuth 连接管理；
- 自动解决所有 `RESULT_UNKNOWN`；
- Kubernetes、多机高可用、自动扩缩容；
- 在 PostgreSQL 长期保存 Published PRD 正文；
- 本地模型或 Provider 自动降级；
- 完整迁移旧 Python 产品的所有确认状态与历史数据。

---

## 1. 当前实现基线与差距

### 1.1 已经具备

- Go Control Plane 和 PostgreSQL Store；
- Task/Run 创建、查询、SSE、停止；
- Queue Slot、Lease、Heartbeat、Fencing Token；
- Go → Python Direct Agent RPC Pool；
- Python Remote LLM Runtime；
- Model Attempt、Evidence、Checkpoint、Draft 基础持久化；
- Maintenance 独立 Admission、Dispatch、Cancel、Recovery、Outbox 循环；
- Agent 事件边读取边落库；
- `UNKNOWN` 恢复预算和 `QUARANTINED`；
- Python gRPC 控制线程与取消；
- 旧 Python 实现中的飞书 Wiki Docx 写入、Preview、Revision 和保护逻辑；
- arm64/x86_64 容器架构问题修复；
- 本地端到端 Run 可到达 `SUCCEEDED`。

### 1.2 仍是 P0 缺口

- 当前事件是单向流，缺少 Go 提交后的显式 `EVENT_ACK`；
- Model Attempt 仍可能在实际 LLM 请求完成后才上报，不能完整防止重复计费；
- Agent RPC 只靠内部网络，缺少最小共享服务身份；
- Go API 与现有 Web 完整合同不一致；
- 缺少 Draft/Evidence/Retry 最小查询接口；
- 缺少轻量生产认证模式；
- 飞书成熟实现仍在旧 Python API，未接入 Go-owned Export Intent 和 Integration Worker；
- 缺少公平调度的确定性验证、队列硬上限和稳定 `Retry-After`；
- 缺少生产 Ingress/Web/Backup/Integration 完整 Compose；
- 缺少故障注入和 24 小时长稳。

### 1.3 已确认不再是外部阻塞

飞书应用已经配置机器人写入权限。P0 不要求重新创建飞书应用，但上线前必须验证：

- 应用已发布；
- 具备 Docx/Wiki API 读写权限，而不只是群消息权限；
- 应用对目标 Wiki Space 和 Docx 有编辑权；
- `APP_ID`、`APP_SECRET`、`DOCUMENT_HOST`、`WIKI_NODE_TOKEN` 有效；
- 同一目标文档完成一次测试写入和一次 Revision 冲突测试。

若以上验证通过，飞书剩余工作全部属于仓库内部实现。

---

## 2. 不可破坏的系统不变量

1. **编排事实只在 PostgreSQL**：Task、Run、Queue、Dispatch、Attempt、Checkpoint、
   Draft、Export Intent、Binding、事件和审计以 PostgreSQL 为准。
2. **Published PRD 属于飞书**：PostgreSQL 只保留恢复所需 Working Draft 和飞书
   Binding/Revision/Hash，不把 Published PRD 变成第二份长期权威正文。
3. **GitHub 拥有代码**：Repository Evidence 必须指向不可变 Commit 和 Locator。
4. **生产只调用远程 LLM**：DeepSeek 不可用时等待或失败，不切换本地模型语义。
5. **API 并发、Agent 串行**：用户数量不等于 Agent 并发度；2GB 档位固定执行一个 Run。
6. **身份不能由浏览器自由声明**：Go 只接受经过入口认证和内部 Secret 验证的用户身份。
7. **失去 Lease 不得提交**：Attempt、Evidence、Checkpoint、Draft、终态和 Export
   Intent 都必须持有当前 Fencing Token。
8. **命令原子提交**：鉴权、幂等、版本检查、业务写、事件和 Outbox 在同一事务完成。
9. **外部调用不持有事务**：LLM、GitHub、飞书和 RPC 等待期间不持有业务行锁。
10. **Redis 可销毁**：Redis 只做唤醒和短缓存，清空后 PostgreSQL 扫描可恢复。
11. **结果未知不盲目重试**：可能已经执行的飞书写入进入 `RESULT_UNKNOWN`。
12. **公开输出脱敏**：Secret、完整内部异常、External ID、路径和正文不得进入日志或 SSE。

---

## 3. P0 目标架构

```mermaid
flowchart LR
    Browser["少量用户浏览器"] --> Ingress["HTTPS Ingress<br/>Basic Auth 用户白名单"]
    Ingress --> Web["Next.js Web"]
    Ingress --> API["Go Public API"]

    API --> PG[("PostgreSQL<br/>唯一编排事实源")]
    API --> Redis[("Redis<br/>可丢弃唤醒")]

    Maintenance["Go Maintenance"] --> PG
    Maintenance --> Redis
    Maintenance --> AgentRPC["Agent RPC V2<br/>共享服务身份"]
    AgentRPC --> Agent["Python Agent Worker<br/>max_inflight=1"]

    Agent --> LLM["DeepSeek"]
    Agent --> Capability["Go Capability Gateway"]
    Capability --> GitHub["GitHub 固定 Commit"]
    Capability --> FeishuRead["飞书只读证据"]

    Integration["Go Integration Worker"] --> PG
    Integration --> FeishuWrite["固定 Wiki Docx"]

    Backup["异机加密备份"] <-- PG
```

### 3.1 常驻服务

| 服务 | P0 职责 |
| --- | --- |
| Ingress | TLS、Basic Auth、身份头清洗、内部代理 Secret、限流 |
| Web | 创建 Task、展示状态/SSE、Draft、Evidence、Retry、飞书确认 |
| Go API | 身份解析、短事务命令、查询、SSE |
| Go Maintenance | Admission、Dispatch、Cancel、Recovery、Outbox、Cleanup |
| Go Integration | Feishu Export Intent 领取、写入、核对和终态 |
| Python Agent | LLM、调查、Grounding、Working Draft、Checkpoint |
| PostgreSQL | 全部编排事实、恢复游标、事件、审计 |
| Redis | 唤醒、取消提示和短缓存 |
| Backup | Base Backup、WAL、恢复验证 |

### 3.2 网络边界

- 宿主机只公开 `80/443`；
- `80` 只跳转 HTTPS；
- API、Agent、PostgreSQL、Redis、Maintenance、Integration 不映射公网端口；
- Ingress 必须删除客户端传来的内部身份头，再写入自己的身份头；
- Agent RPC 和 Capability RPC 只在独立 Docker Internal Network；
- 出站只允许 DeepSeek、GitHub、飞书、证书签发和备份目标。

---

## 4. 少量用户轻量认证

### 4.1 认证模式

生产新增：

```text
PRD_AGENT_AUTH_MODE=proxy_allowlist
```

Ingress 使用 Basic Auth 用户文件，每个用户拥有独立用户名和密码 Hash。认证成功后：

```text
X-PRD-Authenticated-User: <canonical-user-id>
X-PRD-Authenticated-Tenant: default
X-PRD-Proxy-Secret: <random-secret>
```

Go Principal Resolver 必须：

1. 从只读文件加载 Proxy Secret；
2. 使用常量时间比较；
3. 校验用户名格式和允许列表；
4. 忽略 Body、Query 和浏览器原始 `X-User-ID/X-Tenant-ID`；
5. 所有业务接口和 `/me` 使用同一个 Middleware；
6. 未认证返回 `401`，跨 Owner 资源返回不可枚举 `404`。

### 4.2 CSRF/CORS

- 生产不允许跨域；
- 所有写请求校验 `Origin == Public Origin`；
- 写请求必须携带 `Idempotency-Key` 和 `Content-Type: application/json`；
- Ingress 拒绝异常 Host、重复内部身份头和超大 Header；
- Basic Auth 凭据只通过 HTTPS；
- 登录失败按 IP 和用户名限流，但日志不记录密码或 Authorization。

### 4.3 用户与并发边界

- P0 使用单一 Tenant：`default`；
- 用户表或配置允许列表最多几十个用户；
- 每个 Task 仍保存稳定 `owner_id`；
- 每用户最多一个 Runnable Run；
- 可配置每用户 Open Task 上限；
- OIDC、组织同步、角色体系和 Tenant Membership 延后到 P1；
- Principal Resolver 保留接口，未来替换 OIDC 不改变业务 Service。

### 4.4 内部服务身份

P0 在单机可信容器边界内采用高熵共享服务 Token：

```text
authorization: Bearer <service-token>
```

- Go Maintenance 和 Python Agent 从只读 Secret 文件加载；
- Python Agent 在解析 Run 内容前验证；
- Python → Capability 使用另一枚独立 Token；
- 两枚 Token 不复用、不进入日志、可独立轮换；
- 缺失或权限过宽时进程 Not Ready；
- mTLS/Workload Identity 作为 P1 安全增强，不阻塞本阶段少量用户上线。

---

## 5. 后端并发、Admission 与公平队列

### 5.1 并发模型

```text
N 个并发 HTTP/SSE 连接
        ↓
PostgreSQL 持久化 Task/Run
        ↓
每用户公平 Queue Slot
        ↓
1 个活动 Agent Run
```

API 请求不能直接等待 Agent。创建成功后立即返回 `QUEUED` 或
`WAITING_CAPACITY`。

### 5.2 建议默认值

| 配置 | 默认 |
| --- | ---: |
| Agent 活动并发 | 1 |
| Integration 活动并发 | 1 |
| 每用户 Runnable Run | 1 |
| 全局已 Admission Run | 30 |
| 全局等待队列 | 100 |
| 每用户 Open Task | 5 |
| Dispatch 扫描批次 | 10 |
| SSE 每用户连接 | 3 |
| API 请求体 | 256 KB |
| Draft 最大值 | 1 MB |
| Checkpoint 最大值 | 256 KB |
| Evidence 每 Run | 100 |

### 5.3 原子 Admission

创建 Task 的事务必须：

1. 校验用户与 Idempotency Key；
2. 锁定全局和用户 Admission 计数；
3. 创建 PRD Task；
4. 创建 Agent Run；
5. 容量足够时创建 Queue Slot 并置为 `QUEUED`；
6. 容量不足时置为 `WAITING_CAPACITY`；
7. 写 Task Event 和 Outbox；
8. 一次提交。

### 5.4 公平调度

禁止只按全局 `created_at` 长期偏向高频用户。调度顺序：

1. 过滤没有其他活动 Run 的 Owner；
2. 选择 `last_dispatched_at` 最早的 Owner；
3. 在该 Owner 内选择最早 Run；
4. 成功领取后更新 Owner 调度游标；
5. 同分时按 `run_id` 稳定排序。

### 5.5 背压

- 队列达到硬上限时返回 `429 CAPACITY_EXHAUSTED`；
- 返回稳定 `Retry-After`，不泄露其他用户信息；
- PostgreSQL 连接池等待超时返回安全 `503`；
- Redis 不可用时继续数据库扫描；
- Agent 饱和只延迟 Dispatch，不创建额外 goroutine；
- 内存或磁盘到安全水位时停止新 Run，保留查询、取消和运维接口。

---

## 6. Agent RPC V2、事件确认与恢复

### 6.1 为什么需要 V2

当前边读边落库已避免整批丢失，但 Python 不知道某事件是否真正提交。网络在提交后、
响应前断开时，Worker 无法判断是否可继续。P0 需要双向确认。

### 6.2 协议

新增 `AgentWorkerServiceV2.ExecuteRun(stream RunFrame)`。

Go → Python：

```text
START_RUN
EVENT_ACK(event_id, committed_sequence)
CANCEL_RUN
LEASE_LOST
DRAIN
```

Python → Go：

```text
RUN_STARTED
MODEL_ATTEMPT_PLANNED
MODEL_ATTEMPT_FINISHED
EVIDENCE_APPENDED
CHECKPOINT_SAVED
DRAFT_SUBMITTED
RUN_COMPLETED
RUN_FAILED
```

规则：

1. Python 每次只允许一个未确认事件；
2. Go 在一个 PostgreSQL 事务内提交业务事实、Event Inbox、Task Event；
3. 事务成功后发送 `EVENT_ACK`；
4. Python 收到 ACK 后才执行下一有副作用步骤；
5. 同一个 `event_id` 重发必须返回原 committed sequence；
6. 相同 `event_id` 不同 payload hash 进入 `CORRUPT/QUARANTINED`；
7. 未知事件类型、Sequence Gap 或 Contract 不兼容安全失败。

### 6.3 Model Attempt 防重复计费

LLM 请求前：

1. Python 生成稳定 `attempt_key`、request hash；
2. 发送 `MODEL_ATTEMPT_PLANNED`；
3. Go 写入 `PLANNED` 并 ACK；
4. Python 才调用 DeepSeek；
5. 完成后发送 `MODEL_ATTEMPT_FINISHED`；
6. Go 更新同一 Attempt。

若在调用后、完成事件前崩溃：

- Attempt 保持 `PLANNED/RESULT_UNKNOWN`；
- 不自动无限重放；
- 只按模型重试预算执行一次受控恢复；
- 达预算后进入 `FAILED_RETRYABLE` 或人工终止；
- 不切换 Deterministic/Heuristic Model。

### 6.4 Checkpoint

Checkpoint 必须包含：

- schema version；
- Agent Run ID；
- workflow version；
- 下一节点；
- 已确认 Attempt Keys；
- Evidence IDs/Hashes；
- Draft Version/Hash；
- 已用模型/工具/时间预算；
- Repository Snapshot；
- 取消观察序列。

加载时任一身份、版本或 Hash 不匹配进入 `INCOMPATIBLE`，禁止静默从头运行。

### 6.5 取消

- 用户停止先在 PostgreSQL 原子写 `STOPPING` 和取消序列；
- Cancel 独立控制线程立即通知 Python；
- Python 在模型调用前后、Capability 前后、节点边界检查；
- 丢失通知时 Python 通过 Heartbeat 响应观察取消序列；
- 停止后禁止提交新的 Evidence、Draft 和外部写入 Intent；
- 已经无法撤销的 LLM 请求只允许记录 Attempt 结果，不再推进业务状态；
- 最终进入 `STOPPED`。

### 6.6 RPC 中断与 Worker Crash

- Stream 中断后 Dispatch 进入 `UNKNOWN`；
- 已 ACK 事件不回滚；
- 等待 Lease 到期后从最后 committed sequence 恢复；
- 旧 Fencing Token 的写入影响 0 行；
- `UNKNOWN` 自动恢复最多 3 次；
- 超预算 Dispatch 进入 `QUARANTINED`，Run 进入有界失败状态；
- 人工 Retry 必须创建新 Run/Attempt，保留旧审计链。

---

## 7. Agent Loop 与 Capability 最小闭环

### 7.1 Agent 节点

```text
Acquire Context
→ Restore Checkpoint
→ Resolve Information Needs
→ Fetch Repository/Feishu Evidence
→ Plan Model Attempt
→ Call DeepSeek
→ Validate Structured Result
→ Ground Claims
→ Save Checkpoint
→ Submit Working Draft
→ Complete or Wait
```

### 7.2 预算

| 预算 | P0 默认 |
| --- | ---: |
| Run 最大逻辑 Attempt | 3 |
| 单 Attempt Transport Retry | 2 |
| Agent 最大迭代 | 8 |
| Run Token Budget | 24,000 |
| 单次输出 Token | 4,096 |
| LLM Timeout | 90 秒 |
| Run 总执行 Timeout | 30 分钟 |
| Capability 每次 Timeout | 15 秒 |

所有预算必须写入 Checkpoint，重启后不能归零。

### 7.3 最小 Capability

P0 只实现 Agent 真正需要的只读能力：

- `SearchRepository`；
- `ReadRepositoryFile`；
- `ReadFeishuReference`；
- `SearchHistoricalPrd` 可在无 Catalog 时返回明确“不支持”，不得伪造结果。

约束：

- GitHub Repository 固定到不可变 Commit；
- 路径规范化并限制仓库范围；
- 响应大小、文件数和总字节有界；
- 飞书读取校验目标 Wiki Node 和 Revision；
- Provider Secret 只在 Go Capability/Integration 侧；
- Python Agent 只得到经过裁剪的 Evidence，不得到 Provider Token；
- Provider 失败映射稳定错误类别，不跨 RPC 泄露原始响应。

### 7.4 无 Evidence 时

- 用户任务未绑定仓库时，可以生成明确标注为“基于用户输入”的 Draft；
- 任务要求代码事实但 GitHub 不可用时进入 `WAITING_PROVIDER`；
- 飞书历史资料不可用时不得把模型常识描述为当前业务事实；
- Grounding 未通过时 Draft 可以保存，但不能标记为可发布。

---

## 8. 最小产品状态与 API

### 8.1 PRD Task 状态

```text
DRAFT
→ RUNNING
→ REVIEWABLE
→ PUBLISHING
→ PUBLISHED
```

异常分支：

```text
WAITING_CAPACITY
WAITING_PROVIDER
FAILED_RETRYABLE
FAILED
RESULT_UNKNOWN
QUARANTINED
STOPPED
```

### 8.2 Agent Run 状态

```text
WAITING_CAPACITY → QUEUED → RUNNING → SUCCEEDED
                              ├────→ STOPPING → STOPPED
                              ├────→ FAILED_RETRYABLE
                              ├────→ FAILED
                              └────→ QUARANTINED
```

### 8.3 P0 API

| Method | Path | 作用 |
| --- | --- | --- |
| GET | `/api/v1/health/live` | 进程存活 |
| GET | `/api/v1/health/ready` | DB、Schema、Identity 配置 |
| GET | `/api/v1/me` | 当前轻量认证用户 |
| POST | `/api/v1/tasks/from-message` | 创建 PRD Task |
| GET | `/api/v1/tasks` | Owner 范围列表 |
| GET | `/api/v1/tasks/{task_id}` | Task Snapshot |
| GET | `/api/v1/tasks/{task_id}/runs` | Run 历史 |
| GET | `/api/v1/tasks/{task_id}/events` | SSE |
| POST | `/api/v1/tasks/{task_id}/runs/{run_id}/stop` | 停止 |
| POST | `/api/v1/tasks/{task_id}/retry` | 创建恢复 Run |
| GET | `/api/v1/tasks/{task_id}/draft` | 当前 Working Draft |
| GET | `/api/v1/tasks/{task_id}/draft.md` | Markdown 下载 |
| GET | `/api/v1/tasks/{task_id}/evidence` | Evidence 摘要和 Locator |
| GET | `/api/v1/tasks/{task_id}/attempts` | 脱敏 Attempt 状态 |
| POST | `/api/v1/tasks/{task_id}/publish/feishu/preview` | 发布预览 |
| POST | `/api/v1/tasks/{task_id}/publish/feishu` | 确认发布 Intent |
| GET | `/api/v1/tasks/{task_id}/publishes` | 发布历史 |

原 `/exports/feishu` 可作为兼容 Alias，但新代码以 `/publish/feishu` 为规范术语。

### 8.4 API 规则

- 所有 POST 必须有 `Idempotency-Key`；
- 更新必须有 `expected_task_version`；
- Owner 不匹配返回 `404`；
- 错误包含稳定 `error_code`、`message`、`retryable`；
- 容量错误返回 `Retry-After`；
- 响应不暴露飞书 External ID、Provider Token、内部 Stack；
- 同 Key 同 Payload 返回原结果；
- 同 Key 不同 Payload 返回 `409 IDEMPOTENCY_CONFLICT`。

### 8.5 SSE

- Task 内 Sequence 单调递增；
- 支持 `Last-Event-ID`；
- 事件只包含公开白名单字段；
- 断线重连从游标继续；
- Cursor 过期返回稳定错误，Web 拉 Snapshot 后恢复；
- SSE 不保持数据库事务或独占连接；
- 单用户连接数有界。

---

## 9. PostgreSQL 最小 Schema

### 9.1 核心表

```text
go_control_tasks
go_agent_runs
go_queue_slots
go_agent_leases
go_agent_dispatches
go_agent_event_inbox
go_model_attempts
go_evidence
go_checkpoints
go_working_drafts
go_task_events
go_command_idempotency
go_outbox
go_user_schedule_cursor
```

### 9.2 飞书表

```text
go_feishu_bindings
go_publish_previews
go_publish_intents
go_provider_attempts
go_provider_reconciliations
```

### 9.3 关键唯一约束

- `(owner_id, idempotency_key, command_type)`；
- `(run_id, attempt_key)`；
- `(run_id, checkpoint_sequence)`；
- `(dispatch_id, event_id)`；
- `(task_id, event_sequence)`；
- `(task_id, draft_version)`；
- `(task_id, provider)`；
- `(publish_intent_id, provider_attempt_no)`；
- 同一时刻每个 Owner 最多一个活动 Queue Slot；
- 同一 Run 最多一个有效 Lease。

### 9.4 事务边界

以下必须原子：

- Task + Run + Admission + Task Event + Outbox；
- Agent Event Inbox + 对应业务事实 + Task Event；
- Stop + Cancellation Event + Outbox；
- Retry Run + Admission；
- Publish Preview；
- Publish Intent + Audit + Outbox；
- Provider 结果 + Binding + Task 状态 + Task Event。

飞书 HTTP 调用必须发生在事务外。

---

## 10. 飞书固定 Wiki 发布方案

### 10.1 P0 范围

复用现有 `FeishuWikiDocumentGateway` 语义：

- 只配置一个 `WIKI_NODE_TOKEN`；
- 解析到一个 Docx；
- Create 语义实际是覆盖该固定 Docx；
- 用户链接保持 Wiki URL；
- 不允许用户输入任意 Host、Node Token 或 External ID；
- 不做多空间浏览和 OAuth 连接管理。

### 10.2 Secret

Integration Worker 独占：

```text
PRD_AGENT_FEISHU_APP_ID_FILE
PRD_AGENT_FEISHU_APP_SECRET_FILE
PRD_AGENT_FEISHU_DOCUMENT_HOST
PRD_AGENT_FEISHU_WIKI_NODE_TOKEN_FILE
PRD_AGENT_EXPORT_CONFIRMATION_SECRET_FILE
PRD_AGENT_EXTERNAL_ID_ENCRYPTION_KEY_FILE
```

Python Agent 镜像和环境中不得出现这些 Secret。

### 10.3 Preview

Preview 固定：

- Task ID/Version；
- Draft Version/Hash；
- 目标 Binding；
- Provider Revision；
- 发布模式；
- 过期时间；
- 确认 Token。

任何字段变化后旧 Preview 失效。

### 10.4 执行

1. API 校验 Preview、用户和 Task Version；
2. 原子创建 `PENDING` Publish Intent；
3. Integration Worker 领取 Lease；
4. 读取当前飞书 Revision；
5. Revision 不同则 `RESOURCE_CHANGED`；
6. 写入结构化块；
7. 重新读取元数据或内容 Hash；
8. 原子保存 Binding/Revision/Hash 和 `SUCCEEDED`；
9. Task 进入 `PUBLISHED`。

### 10.5 结果未知

若飞书可能已经写入但响应丢失：

- Provider Attempt 进入 `RESULT_UNKNOWN`；
- Publish Intent 进入 `RECONCILING`；
- 禁止用户再次发布；
- Integration Worker 读取固定 Wiki Node 的 Revision/Hash 核对；
- 能确认一致则补记 `SUCCEEDED`；
- 能确认未写入且 Provider 明确未执行时才允许重试；
- 无法确认进入 `MANUAL_REVIEW`；
- UI 显示“正在核对”，不显示“失败请重试”。

固定 Wiki Node 使核对比任意 Create 更可靠，是少量用户 P0 的关键简化。

---

## 11. Maintenance 与 Integration 容错

### 11.1 Maintenance 独立循环

```text
admission promoter
dispatch supervisor
cancel scanner
unknown recovery scanner
outbox publisher
retention cleanup
watchdog/readiness
```

每个循环：

- 独立 ticker；
- 独立 batch 上限；
- panic boundary；
- 超时和退避；
- 指标；
- 一个循环失败不阻止其他循环。

### 11.2 启动恢复

1. 验证 Schema；
2. 验证 PostgreSQL 写入；
3. 释放过期 Outbox Claim；
4. 扫描过期 Lease；
5. 扫描 `STARTED/RUNNING/UNKNOWN` Dispatch；
6. 扫描 `STOPPING`；
7. 从 PostgreSQL 重建 Redis 唤醒；
8. 才开始领取新 Run。

### 11.3 Integration Worker

- 单并发；
- 只领取已提交 Publish Intent；
- 使用 Lease 和 Provider Attempt；
- Provider 429/5xx 只做明确未执行的有界重试；
- 可能已执行时直接 `RESULT_UNKNOWN`；
- 进程重启从 PostgreSQL 扫描；
- 不从 Redis 判断终态。

### 11.4 优雅关闭

- Ingress 先停止新流量；
- API 完成短请求；
- Maintenance 停止新领取；
- Agent 收到 `DRAIN`；
- 活动 RPC 在 deadline 内完成或取消；
- Integration 不启动新 Provider 调用；
- 未完成事实依靠 Lease expiry 接管；
- 不强行把未知外部写入标记为失败。

---

## 12. 配置与安全失败

### 12.1 必需配置

```text
PRD_AGENT_ENVIRONMENT=production
PRD_AGENT_AUTH_MODE=proxy_allowlist
PRD_AGENT_PUBLIC_ORIGIN=https://prd.example.com
PRD_AGENT_PROXY_SECRET_FILE=/run/secrets/proxy_secret
PRD_AGENT_AGENT_RPC_TOKEN_FILE=/run/secrets/agent_rpc_token
PRD_AGENT_CAPABILITY_RPC_TOKEN_FILE=/run/secrets/capability_rpc_token
PRD_AGENT_DATABASE_DSN_FILE=/run/secrets/database_dsn
PRD_AGENT_LLM_PROVIDER=deepseek
PRD_AGENT_LLM_API_KEY_FILE=/run/secrets/deepseek_api_key
```

发布飞书时再要求第 10.2 节 Secret。

### 12.2 生产拒绝启动

- Memory Store；
- Dev Principal；
- 空或不可读 Secret；
- Secret 权限过宽；
- 非 HTTPS Public Origin/Provider URL；
- 未知 Environment/Auth Mode；
- Schema 不兼容；
- 本地/规则模型；
- Agent RPC 无 Token；
- 数据库 DSN 缺失；
- 飞书发布启用但凭据不完整；
- 任意 CORS。

### 12.3 Secret 原则

- 只读文件挂载；
- 不进入镜像、Git、环境转储；
- 日志统一脱敏；
- API、Agent、Integration 使用最小不同 Secret 集；
- 轮换支持短暂 N/N+1；
- Secret Marker 测试覆盖日志、SSE、DB、Redis。

---

## 13. 生产 Compose 与资源预算

### 13.1 完整服务

```text
ingress
web
go-migrate
go-api
go-maintenance
go-integration
python-agent
postgres
redis
backup-agent
```

### 13.2 2GB 预算

| 组件 | 限制 |
| --- | ---: |
| PostgreSQL | 512 MB |
| Redis | 96 MB |
| Go API | 160 MB |
| Go Maintenance | 160 MB |
| Go Integration/Capability | 128 MB |
| Python Agent | 448 MB |
| Web + Ingress | 256 MB |
| Backup + OS 余量 | 288 MB |

总数据库连接默认不超过 20。

### 13.3 容器基线

- 固定镜像 Digest；
- 非 root；
- 只读根文件系统；
- `no-new-privileges`；
- Drop All Capabilities；
- `restart: unless-stopped`；
- 资源、PID 和日志轮转限制；
- Health/Readiness；
- 内部网络；
- 临时目录 `noexec,nosuid`；
- SBOM 和高危依赖扫描；
- Go 二进制架构与镜像目标架构一致。

---

## 14. 观测、告警与备份

### 14.1 最小指标

- API 请求量、延迟、状态码；
- DB Pool used/wait/timeout；
- Queue depth、Oldest Run、每 Owner 等待；
- Dispatch running/unknown/quarantined；
- Lease lost、Heartbeat failure；
- LLM latency、token、attempt status；
- Capability latency/status；
- Draft submitted；
- Publish pending/result_unknown/manual_review；
- SSE connection/reconnect/gap；
- Process restart、OOM、内存、磁盘；
- Backup age 和恢复验证时间。

Label 不包含 User、邮箱、仓库名、飞书 URL、正文、Token 或 External ID。

### 14.2 P0 告警

- API/Maintenance/Agent/Integration 退出；
- DB Not Ready；
- `RESULT_UNKNOWN`；
- `QUARANTINED/CORRUPT`；
- Queue Oldest Age 超过 SLO；
- 磁盘 > 90%；
- 内存 > 85%；
- 最近备份 > 24 小时；
- Secret Marker 泄漏；
- 飞书应用鉴权失败。

### 14.3 备份

- 每日加密 Base Backup；
- WAL 至少每 5 分钟上传异机存储；
- RPO ≤ 15 分钟；
- RTO ≤ 4 小时；
- 7 日、4 周、3 月保留；
- 每月隔离恢复；
- Redis 不备份业务事实；
- Secret 与数据库备份分开；
- 备份超过 24 小时或恢复演练失败阻止发布。

---

## 15. 完整故障与兜底矩阵

| 故障 | 自动处理 | 用户状态 | 最终兜底 |
| --- | --- | --- | --- |
| Ingress/API 退出 | Supervisor 重启 | 短暂重试 | 回滚镜像 |
| PostgreSQL 不可用 | 所有写安全失败 | 维护中 | 恢复/PITR |
| Redis 清空 | DB 扫描重建 | 进度延迟 | 重启 Redis |
| Maintenance 退出 | 重启并扫描 | 排队暂停 | Lease 后接管 |
| Agent Worker 退出 | Dispatch UNKNOWN | 正在恢复 | 3 次后隔离 |
| RPC 提交后断线 | 重发同 event_id | 无感/恢复中 | Event Inbox 幂等 |
| Lease 丢失 | 拒绝旧 Token | 正在恢复 | 新 Worker 接管 |
| Cancel 通知丢失 | Heartbeat/DB 观察 | STOPPING | Fencing 阻止提交 |
| DeepSeek 429/5xx | 有界退避 | 等待模型 | Retry/Fail |
| LLM 结果未知 | Attempt UNKNOWN | 恢复中 | 人工终止 |
| GitHub 读取失败 | 固定 Commit 重试 | WAITING_PROVIDER | 更新授权 |
| 飞书读取失败 | 有界重试 | WAITING_PROVIDER | 更新权限 |
| 飞书 Revision 变化 | 拒绝覆盖 | RESOURCE_CHANGED | 重新 Preview |
| 飞书写入响应丢失 | Reconcile | 正在核对 | MANUAL_REVIEW |
| SSE 断线 | Last-Event-ID | 自动恢复 | Snapshot Reload |
| SSE Gap | 停止增量 | 自动刷新 | 全量 Snapshot |
| Queue 满 | 拒绝 Admission | 稍后重试 | 管理员释放/扩容 |
| 磁盘 > 90% | 停止新 Run | 只读/取消可用 | 清理或扩盘 |
| 备份失败 | 告警并阻止发布 | 无直接影响 | 修复后恢复演练 |

---

## 16. 实施切片

### Slice 0：冻结 P0 范围

- 本文作为当前 P0；
- OIDC、多租户、通用 Provider 平台转 P1；
- 固定一个飞书 Wiki Node；
- 固定 Agent/Integration 并发为 1。

### Slice 1：轻量认证

- `proxy_allowlist` Principal Resolver；
- Proxy Secret 文件；
- 用户白名单；
- Origin/Host 校验；
- Ingress Basic Auth；
- 身份伪造与跨 Owner 测试。

### Slice 2：Agent RPC V2

- 双向 Frame；
- Event Inbox；
- ACK 后继续；
- Planned/Finished Model Attempt；
- Capability/Agent Service Token；
- Stream 断线回归。

### Slice 3：公平队列和恢复

- Owner Schedule Cursor；
- 全局/用户硬上限；
- Retry-After；
- 取消序列；
- 启动恢复；
- 3 次 Recovery/Quarantine。

### Slice 4：最小 Agent 产品 API

- Retry；
- Draft JSON/Markdown；
- Evidence；
- Attempt；
- Snapshot/SSE Gap；
- Web 简化为创建、进度、Draft、失败、Retry。

### Slice 5：Capability

- GitHub 固定 Commit Search/Read；
- 飞书固定 Node Read；
- 超时/大小/路径/权限限制；
- Python 不持有 Provider Secret。

### Slice 6：飞书 Publish

- 迁移/复用 Renderer 和 Wiki Gateway；
- Preview；
- Publish Intent；
- Integration Worker；
- Revision；
- Result Unknown/Reconcile；
- Web 发布确认。

### Slice 7：生产清单

- Ingress/Web/Integration/Backup；
- 内部网络；
- Secret；
- 资源限制；
- Readiness；
- 日志和指标。

### Slice 8：故障门禁

- Kill API/Maintenance/Agent/Integration；
- Redis flush；
- RPC 中断；
- DeepSeek timeout；
- 飞书 success-then-response-lost；
- Backup restore；
- 24 小时混合负载。

---

## 17. 验收 Gate

### Gate 0：静态质量

- Go test/race/vet/checkptr；
- Python test/typecheck/lint；
- Web test/typecheck/build；
- Proto lint/generate/diff；
- 标准 CI 零 Skip/XFail；
- 镜像架构、非 root、SBOM、依赖扫描通过。

### Gate 1：认证与安全

- 公网只有 80/443；
- 未认证请求 401；
- 伪造身份头无效；
- Proxy/Service Secret 缺失拒绝 Ready；
- 跨 Owner 资源不可枚举；
- Origin/Host、CORS、请求体限制通过；
- Secret Marker 为 0。

### Gate 2：并发与队列

- 并发创建同 Key 只产生一个 Task；
- 每 Owner 只有一个 Runnable Run；
- 高频用户不能饿死低频用户；
- 队列硬上限和 Retry-After；
- Redis flush 不丢 Run；
- 50 个并发 API/SSE 连接不导致 OOM。

### Gate 3：Agent 执行

- `MODEL_ATTEMPT_PLANNED` ACK 后才调用 LLM；
- 每个事件 ACK 后才继续；
- Event 重发不重复；
- Worker crash 从最后 ACK 恢复；
- stale fencing 写入 0 行；
- Checkpoint 预算不归零；
- Cancel 在控制线程和节点边界生效。

### Gate 4：最小产品

- 创建、列表、详情、事件；
- Draft 查看和 Markdown 下载；
- Evidence/Attempt；
- Stop/Retry；
- SSE 断线和 Gap；
- 所有响应 Owner 隔离。

### Gate 5：飞书

- 现有应用 Token 获取成功；
- 固定 Wiki Node 解析成功；
- 测试写入成功；
- Revision 冲突拒绝覆盖；
- Preview 变化失效；
- 写入响应丢失不重复写；
- Result Unknown 可核对和人工处理；
- External ID/Secret 不进入 API、日志或 SSE。

### Gate 6：故障恢复

- Kill 每个进程后自动恢复；
- PostgreSQL 短暂中断安全关闭；
- Redis 清空恢复；
- RPC 中断；
- LLM 429/timeout；
- Outbox publish/mark 边界 Kill；
- 最终状态符合故障矩阵。

### Gate 7：部署与长稳

- 空服务器可重复部署；
- API/Agent/Maintenance/Integration 健康；
- 2 vCPU/2GB 24 小时无 OOM；
- 队列、连接、内存、磁盘稳定；
- 备份成功且隔离恢复通过；
- 少量真实用户完成创建、Draft、Retry 和飞书发布。

---

## 18. 发布与回滚

### 18.1 发布顺序

1. 备份数据库；
2. 部署 Expand Migration；
3. 部署 Ingress/Auth/Web；
4. 部署 Go API；
5. 部署 Maintenance；
6. 部署 Agent Worker；
7. 部署 Integration Worker；
8. 运行内部 Smoke；
9. 启用一个测试用户；
10. 观察后逐个加入允许列表。

### 18.2 自动停止扩大范围

- 非预期 5xx/409；
- Owner 隔离错误；
- Event Gap；
- Idempotency mismatch；
- `UNKNOWN/QUARANTINED` 超阈值；
- Agent 反复退出；
- 飞书 `RESULT_UNKNOWN`；
- 备份陈旧；
- 内存/磁盘超阈值。

### 18.3 回滚

1. Ingress 进入维护页；
2. 停止新 Task；
3. Drain Agent 和 Integration；
4. 保留已 ACK 的数据库事实；
5. 保留已完成或未知的飞书写入，不自动逆向删除；
6. 回滚应用镜像；
7. 只在 Schema 向后兼容时恢复旧实例；
8. 运行一致性检查后重新开放。

---

## 19. 外部输入清单

### 已具备或可复用

- 飞书应用及机器人写入权限；
- DeepSeek Secret 文件；
- 单机本地 Docker 验证环境。

### 上线前仍需提供

- 域名和 DNS；
- TLS 签发条件；
- 少量用户用户名和密码 Hash；
- Proxy/Agent/Capability 高熵 Secret；
- PostgreSQL Password/DSN Secret；
- 飞书 App ID/Secret、目标 Host/Node Token 的生产挂载；
- GitHub 凭据或明确 P0 不启用 Repository Capability；
- 异机备份存储；
- 告警接收渠道；
- 一个真实用户 Smoke 和人工飞书核对负责人。

不要求企业身份平台。

---

## 20. 当前实现状态与 Definition of Done

### 已完成

- Maintenance SIGSEGV 根因修复；
- 容器架构契约测试；
- Maintenance 循环隔离；
- Agent 事件即时持久化；
- Cancel 控制线程；
- Unknown Recovery Budget；
- Production 配置部分 fail-closed；
- API/Agent Healthcheck；
- 本地 Task → Agent → Draft → SUCCEEDED 端到端。

### 仍需完成

- `proxy_allowlist`；
- Agent/Capability Service Token；
- RPC V2 ACK；
- Planned Model Attempt；
- 公平 Owner 调度；
- 最小 Draft/Evidence/Retry API；
- Go Capability；
- Go Integration + 固定飞书 Wiki Publish；
- 完整生产 Compose/Ingress/Backup/Observability；
- 零 Skip、故障注入和 24 小时长稳。

### P0 完成定义

只有同时满足以下条件才宣告完成：

1. 少量用户经 HTTPS 认证后稳定使用；
2. 并发请求不会绕过 Owner 隔离、幂等或队列；
3. 同时只执行一个 Agent Run；
4. 任意进程重启不丢已 ACK 事实；
5. LLM、Capability 和飞书失败有有界状态；
6. Working Draft、Evidence、Attempt 和失败原因可查看；
7. 用户可停止和重试；
8. 已确认 Draft 可安全发布到固定飞书 Wiki Docx；
9. 2 vCPU/2GB 连续运行 24 小时无 OOM、死锁和无限重放；
10. 最近备份成功且已完成隔离恢复。

在此之前，系统状态统一为：

```text
NO-GO FOR UNATTENDED PRODUCTION
```

---

## 21. P1 延后项

- OIDC/PKCE/JWKS；
- 多 Tenant Membership 和企业 RBAC；
- mTLS/Workload Identity；
- 多 Agent Worker；
- 多飞书空间和通用 Provider Binding；
- Feishu Webhook/Catalog/完整历史 PRD RAG；
- 自动向量索引切换；
- GitHub OAuth 自助连接；
- 复杂 Outline/Confirmation Unit 产品状态机；
- 大规模旧 Python 数据 Shadow Read/Cutover；
- Kubernetes 和多机高可用。

这些延后项不得破坏 P0 已建立的 Principal、Capability、Provider、Store 和 RPC
接口边界，以保证未来升级不需要重写 Agent 核心。

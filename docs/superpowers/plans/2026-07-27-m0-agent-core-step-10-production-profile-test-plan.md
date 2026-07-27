# PRD Agent 最后一步测试计划：Step 10 Production Profile

> 文档状态：完整测试设计，待实现
> 设计日期：2026-07-27
> 对应设计：`2026-07-27-m0-agent-core-step-10-production-profile-design.md`
> 测试范围：OIDC、租户隔离、PostgreSQL、Outbox、Redis、Celery、多 Worker、Checkpoint、恢复、部署、备份、告警和容量
> 用例总数：176 个（不含 Step 1～9 已有回归）

## 1. 测试目标

本计划需要证明：

1. 异步 Production Profile 与同步 Portfolio Core 的业务结果和状态约束一致。
2. OIDC、多用户、多 API、多 Worker 不会造成跨租户或跨任务泄漏。
3. API、Publisher、Broker、Worker 或 Provider 在任意关键崩溃点失败后，已提交事实不丢失，未提交结果不伪造成功。
4. at-least-once 投递、消息乱序和租约接管不会重复业务节点或外部副作用。
5. Redis 丢失、Worker 重启和 Graph 升级不改变 PostgreSQL 中的业务事实。
6. 停止、删除、SSE 恢复、OAuth Refresh 和 Webhook 在并发下闭环。
7. 部署、迁移、回滚、备份、恢复、告警和容量具备可执行证据。
8. Step 1～9 的 Grounding、确认、固定 Commit、绑定覆盖与凭证边界无回归。

## 2. 测试原则

1. **最终断言业务事实**：不以 Mock 调用次数代替数据库状态、唯一约束和外部 Fake 状态。
2. **默认假设重复投递**：任何 Celery 消息都可能重复、延迟、乱序或在确认前丢失连接。
3. **故障发生在精确边界**：事务提交前后、Broker Publish 前后、Provider 写入前后分别注入。
4. **Redis 可销毁**：所有 P0 恢复场景都必须验证清空 Redis 后仍可从 PostgreSQL 恢复。
5. **安全失败关闭**：鉴权、审计、Secret、版本或租约不确定时，不继续高风险写入。
6. **真实依赖分层**：标准 CI 使用真实 PostgreSQL/Redis 和 Fake OIDC/Provider；Staging 再运行真实受限集成。
7. **P0 零 Skip/XFail**：缺少真实 Provider 凭证只允许跳过 Sandbox，不允许跳过 Fake/Contract 主路径。
8. **隐私检查覆盖 Artifact**：日志、Trace、Metrics、Queue、数据库、测试报告和失败截图都扫描敏感 Marker。

## 3. 测试层级

| 层级 | 依赖 | 目标 |
| --- | --- | --- |
| Unit | 纯函数/Fake Clock | Policy、JWT Claim、租约、幂等、版本和退避 |
| Contract | Fake OIDC/Provider/Broker | 外部协议、消息 Schema、Webhook |
| Integration | 真实 PostgreSQL/Redis | 事务、锁、Outbox、Inbox、租约和恢复 |
| Worker | Celery Worker Harness | 重投、超时、Kill、Queue 路由 |
| API/SSE | FastAPI 多实例 | 鉴权、短事务、游标和并发 |
| Browser E2E | Next.js + 全栈 | 登录到 PRD、停止、恢复和导出 |
| Security | DAST/依赖/镜像 | 越权、CSRF、SSRF、Secret、供应链 |
| Chaos/DR | 可控故障环境 | Redis/Worker/DB/网络、PITR |
| Load/Soak | Staging | SLO、容量、连接和内存稳定性 |
| Sandbox | 真实受限资源 | OIDC、GitHub/GitLab、飞书最终验证 |

## 4. 固定测试环境

### 4.1 CI 拓扑

```text
2 × FastAPI
2 × Outbox Publisher
2 × Agent Worker
2 × Read Integration Worker
1 × Write Integration Worker
1 × Scheduler/Reconciler
1 × PostgreSQL
1 × Redis
1 × Fake OIDC
1 × Fake GitHub/GitLab/Feishu/Model
```

### 4.2 固定身份

| 主体 | Tenant | 角色/Scope |
| --- | --- | --- |
| Alice | tenant-a | 普通用户，完整 task scope |
| Bob | tenant-a | 普通用户，无 Alice 资源权限 |
| Carol | tenant-b | 普通用户 |
| Ops | ops-tenant | 只读运维元数据，无正文 |
| Agent Worker SA | internal | 仅执行 Run |
| Integration Worker SA | internal | 仅访问受控 Provider |

### 4.3 敏感 Marker

每轮测试随机生成：

- Access/Refresh/ID Token Marker。
- Cookie、CSRF、Authorization Marker。
- Repository Secret、PRD PII 和飞书 External ID Marker。
- Secret Manager Value 和 Credential Ref Marker。

所有测试结束后扫描允许的存储面；允许内部 `credential_ref`，不允许解析后的 Secret。

### 4.4 Fake 能力

Fake Broker/Provider 支持：

- 成功前失败、成功后丢响应、延迟、乱序、重复和部分写入。
- 401、403、404、409、429、5xx、Timeout 和非法响应。
- 可移动 Ref、远程 Revision、Webhook 重放和签名错误。
- Worker Kill、暂停、网络分区、时钟推进和租约过期。

## 5. 进入与退出标准

### 5.1 进入

- Step 1～9 P0 回归通过。
- 测试迁移可从空库和 N-1 Schema 执行。
- Fake OIDC/Provider 固定版本。
- 所有异步等待有最大时限，不使用无界 Sleep。

### 5.2 退出

- 本文 176 个用例全部实现。
- P0/P1 标准 CI 零 Skip/XFail。
- 真实 PostgreSQL/Redis、至少 2 API/2 Worker 下通过。
- Chaos/Load/DR 在 Staging 达到门禁。
- 敏感 Marker 泄漏数为 0。
- 所有失败均有关联 ID、稳定错误和 Runbook。
- 全量 Step 1～10 + Eval 无关键回归。

## 6. 公共与配置用例（S10-COMMON，8 个）

| ID | 级别 | 前置条件 | 操作 | 断言 |
| --- | --- | --- | --- | --- |
| S10-COMMON-001 | P0 | Production 配置缺少数据库 DSN | 启动 API/Worker | Startup 失败；不进入 Ready；错误不打印 DSN 密码 |
| S10-COMMON-002 | P0 | 配置包含未知环境名或 Profile | 启动任一进程 | 严格拒绝；不回退为 development |
| S10-COMMON-003 | P0 | Celery Payload 使用未知版本 | 投递任务 | Worker 安全拒绝并告警；Run 不变；不反序列化任意对象 |
| S10-COMMON-004 | P0 | Graph/Prompt/Tool 版本配置完整 | 创建 Run | 四类版本固定到 Run；后续全局配置变化不改变运行中 Run |
| S10-COMMON-005 | P1 | 两个实例配置完全一致 | 读取公开配置摘要 | Hash 一致；摘要不含 Secret |
| S10-COMMON-006 | P0 | 生产配置启用 Debug/任意 CORS/不安全 Cookie | 启动 | 配置校验失败关闭 |
| S10-COMMON-007 | P1 | Fake Clock 跨越 UTC 日期/DST | 创建租约、Token 和 TTL | 全部使用 UTC；无负 TTL 或重复调度 |
| S10-COMMON-008 | P0 | 日志、Trace、Metrics、Queue 和 DB 已清空 | 执行成功/失败全路径并扫描 Marker | Secret/正文/External ID 明文出现次数为 0 |

## 7. OIDC、Token 与会话用例（S10-AUTH，14 个）

| ID | 级别 | 前置条件 | 操作 | 断言 |
| --- | --- | --- | --- | --- |
| S10-AUTH-001 | P0 | Fake OIDC 正常 | 完成 Authorization Code + PKCE 登录 | 创建内部用户/会话；Cookie Secure、HttpOnly、SameSite；返回原安全页面 |
| S10-AUTH-002 | P0 | 正常授权事务 | 回调使用错误 `state` | 拒绝；不兑换 Token；不创建会话 |
| S10-AUTH-003 | P0 | 正常授权事务 | 回调使用错误 PKCE verifier | 拒绝；无会话；安全审计记录失败 |
| S10-AUTH-004 | P0 | ID Token 签名被篡改 | 调用 API | 401；无数据库业务查询 |
| S10-AUTH-005 | P0 | Token `iss` 不在 Allowlist | 调用 API | 401；不按 Claim 创建用户 |
| S10-AUTH-006 | P0 | Token `aud` 不匹配 API | 调用 API | 401 |
| S10-AUTH-007 | P0 | Token 已过期或 `nbf` 在未来 | 调用 API | 401；只允许配置的小范围时钟偏差 |
| S10-AUTH-008 | P0 | Token nonce 与授权事务不同 | 完成回调 | 拒绝 Login CSRF/Code Injection |
| S10-AUTH-009 | P0 | Header 指定 `alg=none` 或非允许算法 | 调用 API | 401；不降级验签 |
| S10-AUTH-010 | P1 | OIDC 轮换 JWKS，出现新 `kid` | 首次使用新 Key | 有界刷新一次后成功；刷新失败不接受未知 Key |
| S10-AUTH-011 | P1 | JWKS 服务短暂不可用、缓存 Key 未过期 | 使用旧有效 Key | 在缓存策略内成功；产生依赖降级指标 |
| S10-AUTH-012 | P0 | 用户登录后内部账户被禁用 | 再调用读写 API | 统一拒绝；活动会话不可继续写 |
| S10-AUTH-013 | P0 | 用户完成登录 | 登出后重放旧 Cookie | 401；会话撤销审计存在 |
| S10-AUTH-014 | P1 | 用户登录/刷新/提权 | 比较前后 Session ID | Session 轮换；旧 ID 失效；无 Session Fixation |

## 8. Tenant、Owner 与 Scope 用例（S10-TENANT，10 个）

| ID | 级别 | 前置条件 | 操作 | 断言 |
| --- | --- | --- | --- | --- |
| S10-TENANT-001 | P0 | Alice/Bob 同 Tenant，各有任务 | Bob 读取 Alice Task ID | 404/统一不可枚举错误；正文为 0 字节 |
| S10-TENANT-002 | P0 | Alice/Carol 跨 Tenant | Carol 读取 Alice Task/Message/Document | 全部拒绝；响应差异不泄露存在性 |
| S10-TENANT-003 | P0 | Alice 有 Evidence/Fact/Unknown | Bob 猜测各内部 ID | 全部拒绝；Provider 调用 0 |
| S10-TENANT-004 | P0 | Alice 有 Repository Binding | Bob 发起调查并提交 Alice Binding ID | Provider 调用 0；稳定拒绝 |
| S10-TENANT-005 | P0 | Alice 有 Export Intent/Binding | Bob 执行/覆盖 | 飞书调用 0；不泄露标题、URL、External ID |
| S10-TENANT-006 | P0 | 请求正文伪造 `owner_id/tenant_id` | Alice 提交命令 | Schema 拒绝未知字段或忽略为 Principal；不能改归属 |
| S10-TENANT-007 | P0 | Ops 只有 operations scope | Ops 查询 Run 运维信息和 Task Detail | 可看状态/计数；不可看消息、PRD、Evidence 摘录 |
| S10-TENANT-008 | P0 | Agent Worker SA | 直接调用用户管理/连接 API | 403；Service Principal Scope 生效 |
| S10-TENANT-009 | P1 | 同 Tenant 100 个用户 | 并发分页任务列表 | 每页只含当前 owner；Cursor 不可跨 owner 重放 |
| S10-TENANT-010 | P0 | 删除 Alice 用户映射再重建同邮箱不同 Subject | 登录 | 生成不同内部映射；邮箱不作为授权主键 |

## 9. PostgreSQL、连接池与迁移用例（S10-DB，10 个）

| ID | 级别 | 前置条件 | 操作 | 断言 |
| --- | --- | --- | --- | --- |
| S10-DB-001 | P0 | 2 API + 多并发请求 | 执行读写与 SSE | 每请求/每 Poll 独立连接；无共享事务污染 |
| S10-DB-002 | P0 | Provider Fake 阻塞 10 秒 | 执行工具/导出 | 阻塞期间无业务写事务和行锁 |
| S10-DB-003 | P0 | 命令将写 Task、Event、Outbox | 在每条 SQL 后注入异常 | 三者全提交或全回滚，不出现孤儿 |
| S10-DB-004 | P0 | 同 Task Version | 10 个并发状态命令 | 仅一个成功；其余 409；最终版本连续 |
| S10-DB-005 | P0 | 同 Task 无活动 Run | 10 个并发创建 Run | 唯一约束保证一个活动 Run |
| S10-DB-006 | P1 | 连接池接近上限 | 发起更多请求 | 有界等待后 503/稳定错误；不无限挂起 |
| S10-DB-007 | P1 | 执行超时 SQL/锁等待 | 调用接口 | `statement_timeout/lock_timeout` 生效；事务回滚 |
| S10-DB-008 | P0 | 空数据库 | 执行完整迁移 | Schema、索引、约束和版本正确；可启动所有进程 |
| S10-DB-009 | P0 | N-1 生产样本 Schema | 执行 Expand + 回填 | 旧代码仍可读写；新列计数/Hash 校验通过 |
| S10-DB-010 | P0 | Schema 低于/高于应用支持范围 | Readiness | 返回 Not Ready；Liveness 仍正常 |

## 10. Outbox 用例（S10-OUTBOX，14 个）

| ID | 级别 | 前置条件 | 操作 | 断言 |
| --- | --- | --- | --- | --- |
| S10-OUTBOX-001 | P0 | 新建任务命令成功 | 查询业务表与 Outbox | 同事务存在一个版本化 Run 命令 |
| S10-OUTBOX-002 | P0 | 事务在 Outbox Insert 前失败 | 重试命令 | 无业务半状态；重试只产生一个逻辑 Run |
| S10-OUTBOX-003 | P0 | Outbox 已提交、未发布 | Kill API | Publisher 最终投递；客户端同 Key 重试得到原结果 |
| S10-OUTBOX-004 | P0 | 两个 Publisher | 并发领取 100 行 | 每行同一时刻一个租约；全部最终发布 |
| S10-OUTBOX-005 | P0 | Publisher 领取后 Kill | 推进租约时间 | 另一 Publisher 接管；消息不丢 |
| S10-OUTBOX-006 | P0 | Broker 接收后 Publisher 在标记前 Kill | 恢复 Publisher | 允许重复发布；业务结果只生效一次 |
| S10-OUTBOX-007 | P1 | Broker 连续失败 3 次后恢复 | 执行 Publisher | 有界指数退避；恢复后发布；不忙循环 |
| S10-OUTBOX-008 | P0 | Broker 永久失败超过上限 | 推进时间 | 消息进入 Quarantine；告警；业务 Run 仍为可恢复状态 |
| S10-OUTBOX-009 | P0 | Outbox Payload 包含正文/Token 字段尝试 | 保存/发布 | Schema 拒绝；敏感内容不进入 Broker |
| S10-OUTBOX-010 | P0 | 同 Aggregate 的 v2 命令先于 v1 到达 | 发布并消费 | Worker 按业务版本处理；不依赖 Broker 顺序破坏状态 |
| S10-OUTBOX-011 | P1 | Outbox `available_at` 在未来 | Publisher 扫描 | 到期前不领取；到期后恰好进入候选 |
| S10-OUTBOX-012 | P1 | 100 万已发布行 + 100 未发布行 | 执行扫描 | 使用索引；查询计划和延迟在预算内 |
| S10-OUTBOX-013 | P0 | Publisher 使用过期 lease_owner 标记完成 | 更新 published_at | 更新 0 行；不能覆盖新 Publisher 状态 |
| S10-OUTBOX-014 | P1 | 清理已发布 Outbox | 执行 Retention Job | 仅清理超过保留期行；未发布/隔离行保留 |

## 11. Celery、Inbox 与队列用例（S10-QUEUE，10 个）

| ID | 级别 | 前置条件 | 操作 | 断言 |
| --- | --- | --- | --- | --- |
| S10-QUEUE-001 | P0 | 合法 RunCommand v1 | 投递 `agent.run` | Agent Worker 消费；只按 run_id 取业务状态 |
| S10-QUEUE-002 | P0 | 同 message_id 重复 20 次 | 并发投递 | Inbox/Run 状态保证一个逻辑处理；全部安全确认 |
| S10-QUEUE-003 | P0 | 不同 message_id 唤醒同一 run_id | 并发投递 | 租约只允许一个执行者；不重复节点 |
| S10-QUEUE-004 | P0 | Payload 多出正文、Token 或 Pickle | 投递 | 严格 Schema/Serializer 拒绝；不执行代码 |
| S10-QUEUE-005 | P0 | Read/Write/Agent/Eval 四类任务 | 投递 | 路由到正确 Queue；错误 Worker 不消费 |
| S10-QUEUE-006 | P1 | 长 Agent Task 与短 Read Task 混合 | 持续投递 | Queue 隔离；短任务不被长任务无限阻塞 |
| S10-QUEUE-007 | P0 | Worker 完成业务提交、ACK 前 Kill | 重投 | 返回已保存结果；不重复副作用 |
| S10-QUEUE-008 | P0 | Worker 收到消息、业务提交前 Kill | 重投 | 未提交结果不存在；接管后安全重做 |
| S10-QUEUE-009 | P1 | Task 超过 soft/hard time limit | 执行 | 保存标准超时/租约可恢复状态；无僵尸执行 |
| S10-QUEUE-010 | P1 | Broker 连接抖动 | 连续发布/消费 | 有界重连；指标可见；无无限日志风暴 |

## 12. Worker Lease、Heartbeat 与 Fencing 用例（S10-LEASE，12 个）

| ID | 级别 | 前置条件 | 操作 | 断言 |
| --- | --- | --- | --- | --- |
| S10-LEASE-001 | P0 | Run 可执行且无租约 | Worker A 领取 | 创建租约、token=1、attempt+1 |
| S10-LEASE-002 | P0 | A 持有有效租约 | Worker B 尝试领取 | B 不执行；不增加 Provider 调用 |
| S10-LEASE-003 | P0 | A 正常运行 | 周期 Heartbeat | expires_at 前移；业务 Task Version 不变化 |
| S10-LEASE-004 | P0 | A 网络暂停至租约过期 | B 接管 | token 单调增加；B 从已提交边界恢复 |
| S10-LEASE-005 | P0 | B 已 token=2 提交 | A 恢复并以 token=1 提交 | 更新 0 行；A 丢弃结果 |
| S10-LEASE-006 | P0 | A 失去租约但 Provider 调用已发出 | Provider 返回成功 | 保存 Attempt 待核对；不能直接覆盖 B |
| S10-LEASE-007 | P1 | 数据库心跳短暂失败但未过宽限 | Worker 行为 | 停止新副作用，重试心跳；不立即标 Run Failed |
| S10-LEASE-008 | P0 | 100 Worker 争抢同 Run | 同时领取 | 一个获胜；无死锁；token 连续 |
| S10-LEASE-009 | P1 | Worker Clock 偏差 ±30 秒 | 领取/心跳 | 以数据库时间判断租约；无提前接管 |
| S10-LEASE-010 | P0 | Run 已 STOPPING/DELETED | Worker 领取 | 拒绝；不执行模型或工具 |
| S10-LEASE-011 | P0 | Run 已终态 | 重复唤醒 | 幂等返回；不新建租约 |
| S10-LEASE-012 | P1 | 租约表有大量历史行 | Scheduler 扫描 | 只领取过期活动资源；查询延迟在预算内 |

## 13. Graph Checkpoint 与版本用例（S10-GRAPH，10 个）

| ID | 级别 | 前置条件 | 操作 | 断言 |
| --- | --- | --- | --- | --- |
| S10-GRAPH-001 | P0 | 创建 Task/Run | 生成 thread_id | 使用 task + graph_name + major_version，稳定可重建 |
| S10-GRAPH-002 | P0 | Graph 在大纲确认 Interrupt | Kill/重启 Worker | 仍等待同一用户确认；不生成正文 |
| S10-GRAPH-003 | P0 | 单元确认后 Checkpoint 写失败 | 恢复 | 业务确认事实保留；从安全业务边界重建 |
| S10-GRAPH-004 | P0 | Checkpoint 成功但业务事务失败 | 恢复 | 不把 Checkpoint 当业务成功；重做未提交节点 |
| S10-GRAPH-005 | P0 | 已成功 Tool Action 有持久化签名 | 从旧 Checkpoint 恢复 | 不重复 Tool；复用已保存结果 |
| S10-GRAPH-006 | P0 | Graph minor 版本声明兼容 | 用新 Worker 恢复旧 Checkpoint | 合同输出一致；版本记录更新符合策略 |
| S10-GRAPH-007 | P0 | Graph major 版本不兼容 | 尝试直接恢复 | 明确阻塞/迁移；不静默反序列化 |
| S10-GRAPH-008 | P1 | 完成/停止/失败超过 30 天 | 清理 Job | 仅保留确认边界/最后失败元数据；活动 Checkpoint 不删 |
| S10-GRAPH-009 | P0 | 删除 Task | 执行清理 | Checkpoint 正文删除；最小审计不含正文 |
| S10-GRAPH-010 | P1 | Checkpoint 含未知字段/损坏数据 | 恢复 | 安全失败并进入可诊断状态；不执行任意对象 |

## 14. 停止、取消与删除用例（S10-CANCEL，8 个）

| ID | 级别 | 前置条件 | 操作 | 断言 |
| --- | --- | --- | --- | --- |
| S10-CANCEL-001 | P0 | Run 在节点间 | 用户停止 | DB 先写 cancellation；Worker 进入 STOPPED；无后续节点 |
| S10-CANCEL-002 | P0 | Model 正在流式生成 | 用户停止 | 中断或丢弃未提交输出；已确认内容保留 |
| S10-CANCEL-003 | P0 | Read Tool 正在执行且不可立即取消 | 用户停止 | 显示 STOPPING；Tool 返回后不继续 |
| S10-CANCEL-004 | P0 | 飞书写入已发送、结果未知 | 用户停止 | 进入核对/人工复核；不伪造 STOPPED 无副作用 |
| S10-CANCEL-005 | P0 | Redis Cancel 信号丢失 | 用户停止 | Worker 下一次 DB 检查仍停止 |
| S10-CANCEL-006 | P0 | 相同 Stop 命令重复 10 次 | 并发提交 | 同一结果；单一状态迁移和审计 |
| S10-CANCEL-007 | P0 | Task 正在运行 | 用户确认删除 | 阻止新租约，完成取消和清理；飞书文档不删除 |
| S10-CANCEL-008 | P0 | 删除与 Worker 提交结果竞争 | 同时执行 | 删除/fencing/version 规则确定结果；正文不复活 |

## 15. SSE 与事件恢复用例（S10-SSE，10 个）

| ID | 级别 | 前置条件 | 操作 | 断言 |
| --- | --- | --- | --- | --- |
| S10-SSE-001 | P0 | Alice Task 有事件 | Alice 连接任意 API 实例 | 按 sequence 接收公开白名单事件 |
| S10-SSE-002 | P0 | Bob 猜 Alice Task | 建立 SSE | 连接前拒绝；事件字节为 0 |
| S10-SSE-003 | P0 | 客户端收到 sequence=10 后断线 | 用 Last-Event-ID=10 重连另一 API | 从 11 继续；无重复业务应用 |
| S10-SSE-004 | P0 | 中间事件 11 被客户端丢弃，收到 12 | Gap 检测 | 暂停增量，重拉快照，再恢复 |
| S10-SSE-005 | P0 | Cursor 超过事件保留期 | 重连 | 返回 `EVENT_CURSOR_EXPIRED`；客户端完整重同步 |
| S10-SSE-006 | P0 | Redis 清空/不可用 | 继续产生事件 | 退化 DB Poll；事件仍可重放 |
| S10-SSE-007 | P1 | 100 个并发 SSE + 普通 API | 持续 5 分钟 | SSE 不长期占用事务；普通 API 延迟在预算 |
| S10-SSE-008 | P1 | 无事件 | 保持连接 | 发送心跳；心跳不进入业务事件表 |
| S10-SSE-009 | P0 | 内部事件含 Token/Path/External ID | 发布公开事件 | Payload 白名单移除私有字段 |
| S10-SSE-010 | P1 | API 滚动重启 | 客户端自动重连 | 使用 Cursor 恢复；不重复命令或 Run |

## 16. 外部连接、OAuth、Secret 与 Webhook 用例（S10-OAUTH，12 个）

| ID | 级别 | 前置条件 | 操作 | 断言 |
| --- | --- | --- | --- | --- |
| S10-OAUTH-001 | P0 | Alice 已登录 | 发起 GitHub OAuth | 创建单 owner 短期 state/PKCE 事务；响应不含 Secret |
| S10-OAUTH-002 | P0 | Alice 发起授权 | Bob 使用 Alice state 回调 | 拒绝；不创建 Connection |
| S10-OAUTH-003 | P0 | 正常 Provider 回调 | 完成 Token 兑换 | Token Value 只进入 Secret Manager；DB 仅保存 credential_ref |
| S10-OAUTH-004 | P0 | Secret Manager 写成功、DB 提交失败 | 重试回调/清理 | 无可用孤儿 Connection；临时 Secret 可核对清理 |
| S10-OAUTH-005 | P0 | DB Connection 已保存、响应丢失 | 相同授权事务重试 | 返回同一 Connection；不创建多个 Token |
| S10-OAUTH-006 | P0 | Access Token 过期 | 两个 Worker 同时刷新 | Refresh Lease 只允许一次 Provider Refresh；双方得到一致新 Ref |
| S10-OAUTH-007 | P0 | Provider 返回 `invalid_grant` | 执行刷新 | Connection 进入 REAUTH_REQUIRED；不无限重试 |
| S10-OAUTH-008 | P0 | Alice/Bob 各有连接 | Alice 提交 Bob connection_id | Provider 调用 0；不可枚举拒绝 |
| S10-OAUTH-009 | P0 | Alice 断开连接且有历史 Binding | DELETE Connection | 禁止新调用；不删除 GitHub/飞书外部资源；历史审计最小保留 |
| S10-OAUTH-010 | P0 | Webhook 使用错误签名/过期时间 | POST 回调 | 拒绝；不查询/修改业务 Binding |
| S10-OAUTH-011 | P0 | 同 Provider Delivery ID 重放 20 次 | 并发发送 | 唯一去重；一次逻辑处理 |
| S10-OAUTH-012 | P0 | 合法签名但 Payload 指向未知/其他 owner 资源 | 发送 Webhook | 不创建自由 Binding；不泄露资源；记录安全计数 |

## 17. 安全与审计用例（S10-SEC，12 个）

| ID | 级别 | 前置条件 | 操作 | 断言 |
| --- | --- | --- | --- | --- |
| S10-SEC-001 | P0 | Cookie 会话有效但无 CSRF Token | 调用写接口 | 403；读接口不受错误 CSRF 影响 |
| S10-SEC-002 | P0 | CORS 来源不在 Allowlist | 发送预检和带凭证请求 | 不返回允许凭证 Header；业务不执行 |
| S10-SEC-003 | P0 | 登录回调 `return_to` 为外部 URL | 完成登录 | 只跳转内部 Allowlist；阻止 Open Redirect |
| S10-SEC-004 | P0 | Repository/PRD 含 Prompt Injection | 执行 Production Worker | 仍只调用白名单工具；无 Secret、写代码或任意网络 |
| S10-SEC-005 | P0 | 用户提交任意 Provider URL/内网 IP | 调用连接/仓库/导出 API | Schema/Binding 拒绝；无 SSRF |
| S10-SEC-006 | P0 | Celery Broker 注入 Pickle/任意模块对象 | Worker 消费 | Serializer 拒绝；无代码执行 |
| S10-SEC-007 | P0 | Trace 自动采集配置尝试抓 Authorization/Cookie | 执行请求 | Header 被禁采或脱敏；Marker 为 0 |
| S10-SEC-008 | P0 | Provider 返回含 Token/堆栈的错误 | 触发 API、Worker、SSE、日志 | 只出现稳定错误码和 Correlation ID |
| S10-SEC-009 | P0 | 高风险导出操作发生且 Audit Store 失败 | 执行 | 失败关闭；Provider 调用 0；产生运维告警 |
| S10-SEC-010 | P1 | 审计查询由 Ops 执行 | 查看日志 | 包含主体 Hash、内部目标、动作、结果和时间；无正文/External ID |
| S10-SEC-011 | P0 | 临时远程仓库含可执行文件和 Symlink | Worker 建立快照 | 只读/noexec/配额/路径策略生效；不能逃逸 |
| S10-SEC-012 | P0 | 构建镜像 | 扫描用户、能力、SBOM、签名和依赖 | 非 root、只读根、无高危未豁免漏洞；SBOM/签名可验证 |

## 18. 可观测性与告警用例（S10-OBS，8 个）

| ID | 级别 | 前置条件 | 操作 | 断言 |
| --- | --- | --- | --- | --- |
| S10-OBS-001 | P1 | 完整 Run 成功 | 查询 Trace | HTTP→Outbox→Celery→Lease→Graph→Provider→Event 可关联 |
| S10-OBS-002 | P1 | 同消息重复投递 | 查询 Trace | 使用 Span Link 表示重复；不伪造单次 exactly-once |
| S10-OBS-003 | P0 | 多 Tenant 并发运行 | 检查 Metrics Labels | 无 owner、邮箱、仓库名、External ID、URL、Token 或正文 |
| S10-OBS-004 | P1 | Outbox 最老年龄超过阈值 | 推进监控周期 | 告警触发一次；包含 Dashboard/Runbook 链接 |
| S10-OBS-005 | P1 | Queue 深度持续增长后恢复 | 观察告警 | 达阈值触发；恢复窗口后自动解除；无抖动风暴 |
| S10-OBS-006 | P1 | Worker 发生租约接管与 fencing 拒绝 | 查看指标/日志 | 两类指标分别准确；可定位内部 run_id |
| S10-OBS-007 | P0 | 审计、日志或 Trace Exporter 不可用 | 执行普通读/高风险写 | 普通业务按策略降级；高风险审计失败关闭；告警明确 |
| S10-OBS-008 | P1 | 人工触发每条 P0 告警 | 执行 Runbook 演练 | Owner、严重级别、确认和解除步骤可执行并记录 |

## 19. 部署、迁移、兼容与回滚用例（S10-DEPLOY，10 个）

| ID | 级别 | 前置条件 | 操作 | 断言 |
| --- | --- | --- | --- | --- |
| S10-DEPLOY-001 | P0 | N 版本运行中 | 执行 Expand Migration | N API/Worker 持续可用；无破坏性锁超预算 |
| S10-DEPLOY-002 | P0 | N/N+1 API 同时存在 | 混合请求 | OpenAPI 合同兼容；幂等和版本约束一致 |
| S10-DEPLOY-003 | P0 | N/N+1 Worker 同时存在 | 处理旧/新 Payload | 已声明版本正确路由；未知版本安全拒绝并告警 |
| S10-DEPLOY-004 | P0 | 旧 Graph Run 活动中 | 部署新 Graph Major | 旧 Run 由兼容 Worker 完成或明确迁移；不静默升级 |
| S10-DEPLOY-005 | P0 | 100 个活动 Run | 滚动重启 Worker | 优雅退出或租约接管；无丢失/重复外部写入 |
| S10-DEPLOY-006 | P0 | 新 API 已部署但错误率超阈值 | 回滚应用 | 旧版本兼容 Expand Schema；业务数据不回滚丢失 |
| S10-DEPLOY-007 | P0 | 回填任务中途 Kill | 恢复回填 | 从持久化游标继续；重复执行幂等；校验计数一致 |
| S10-DEPLOY-008 | P0 | 仍存在 N 实例 | 尝试 Contract Migration | 门禁阻止删除旧字段 |
| S10-DEPLOY-009 | P1 | Liveness/Readiness/Startup Probe 配置 | 分别断 DB、Redis、Provider | 各组件按依赖正确 Ready/Live；Provider 故障不杀 API Liveness |
| S10-DEPLOY-010 | P0 | Production Manifest | 执行策略测试 | 资源限制、非 root、只读文件系统、Network Policy、Secret Ref 和镜像固定生效 |

## 20. 备份、灾难恢复与 Chaos 用例（S10-DR，8 个）

| ID | 级别 | 前置条件 | 操作 | 断言 |
| --- | --- | --- | --- | --- |
| S10-DR-001 | P0 | 有已完成、运行中、等待确认和导出未知任务 | 执行 PostgreSQL 备份并恢复隔离环境 | Schema/行计数/Hash/owner 隔离正确 |
| S10-DR-002 | P0 | 记录恢复点后继续写入 | 执行 PITR 到目标时间 | 实测 RPO ≤15 分钟；恢复点后数据不伪造存在 |
| S10-DR-003 | P0 | Redis 含 Broker/Cancel 临时状态 | 完全清空并重建 Redis | PostgreSQL 业务状态不变；可恢复 Run 重建；完成 Run 不重跑 |
| S10-DR-004 | P0 | Agent Worker 执行节点 | Kill -9 Worker | 租约过期后 95% 在 60 秒内接管；旧 Worker 不回写 |
| S10-DR-005 | P0 | Publisher 已 Publish 未 Mark | Kill Publisher | 重复消息安全；Outbox 最终 published |
| S10-DR-006 | P0 | Integration Worker 调用飞书成功后网络断开 | 恢复 | 一个远程文档；Run Reconcile/Manual Review 符合能力 |
| S10-DR-007 | P1 | PostgreSQL 主库短暂不可用 | 持续 API/Worker 流量 | 写请求失败关闭；不执行未记录副作用；恢复后可重试 |
| S10-DR-008 | P0 | 完整恢复演练 | 按 Runbook 从备份恢复并切换测试流量 | 实测 RTO ≤4 小时；证据、偏差和改进项归档 |

## 21. 性能、容量与长稳用例（S10-PERF，10 个）

| ID | 级别 | 前置条件 | 操作 | 断言 |
| --- | --- | --- | --- | --- |
| S10-PERF-001 | P1 | Staging 代表性数据 | 100 RPS 读取 15 分钟 | p95 <500ms；错误率/Pool 等待在预算 |
| S10-PERF-002 | P1 | 同上 | 20 RPS 命令接收 | p95 <1s；每命令业务+Outbox 原子 |
| S10-PERF-003 | P1 | 1000 个待派发 Outbox | 运行 2 Publisher | p95 派发 <3s、p99 <10s；无饥饿 |
| S10-PERF-004 | P1 | 100 并发 SSE + 任务更新 | 运行 30 分钟 | p95 可见 <3s；连接池无泄漏 |
| S10-PERF-005 | P1 | 50 并发 Agent Run | 运行固定轻量模型 Fake | 单任务单租约；Queue 可预测；无 OOM |
| S10-PERF-006 | P1 | Provider 429 限速 | 100 调查请求 | Token Bucket/退避生效；无 Provider 风暴 |
| S10-PERF-007 | P1 | 100 万 Task/Event/Outbox 历史行 | 执行列表、恢复和清理查询 | 查询计划使用目标索引；延迟预算通过 |
| S10-PERF-008 | P1 | 24 小时混合负载 | Soak Test | 内存、连接、线程、租约和 Queue 无持续泄漏 |
| S10-PERF-009 | P1 | 逐步增加并发至饱和 | 测试 Backpressure | 达容量后有界拒绝/排队；无级联崩溃 |
| S10-PERF-010 | P1 | 当前容量结果 | 扩容 API/Worker/Publisher | 吞吐按预期提升；数据库/Provider 瓶颈被记录 |

## 22. 全链路 E2E 用例（S10-E2E，10 个）

| ID | 级别 | 前置条件 | 操作 | 断言 |
| --- | --- | --- | --- | --- |
| S10-E2E-001 | P0 | Alice 未登录 | 登录→创建任务→澄清→确认大纲→逐单元确认→完成 | 状态、事件、PRD 和用户确认全链路正确 |
| S10-E2E-002 | P0 | Alice Task 正在后台运行 | 关闭浏览器→换 API 实例重新打开 | 不重复 Run；恢复最新进度和待确认内容 |
| S10-E2E-003 | P0 | Alice/Bob 同时登录 | 并行完成各自任务 | 对话、草稿、Evidence、SSE 和成本完全隔离 |
| S10-E2E-004 | P0 | Run 正在模型/工具节点 | 用户停止→补充要求→重试 | 已确认内容保留；新 Run 从合法边界继续 |
| S10-E2E-005 | P0 | 必需调查中 Worker Kill | 等待恢复 | 新 Worker 接管；固定 Commit/Evidence 不漂移 |
| S10-E2E-006 | P0 | Completed Task + 飞书连接 | 预览→明确创建确认→响应丢失→重试 | 一个 Binding/远程文档；安全链接可见 |
| S10-E2E-007 | P0 | 已绑定飞书且远程 Revision 变化 | 覆盖预览→执行 | RESOURCE_CHANGED；不覆盖人工修改 |
| S10-E2E-008 | P0 | API 滚动重启 + SSE 断线 | 持续操作任务 | 客户端 Cursor 恢复；无事件 Gap 未处理 |
| S10-E2E-009 | P0 | Alice 删除正在运行且已导出的任务 | 二次确认删除 | Run 停止、产品数据清理、飞书文档保留 |
| S10-E2E-010 | P0 | Redis 清空、Worker/Publisher 重启 | 完成一份包含历史 PRD+远程代码调查的 PRD | 业务最终完成；Grounding、来源、版本和确认无回归 |

## 23. 故障注入矩阵

每个关键副作用至少在以下位置注入一次：

| 编号 | 边界 | 预期 |
| --- | --- | --- |
| F-01 | 业务写前 | 无业务/Outbox 记录 |
| F-02 | 业务写后、Outbox 写前 | 同事务回滚 |
| F-03 | Outbox 提交后、HTTP 响应前 | 客户端幂等重试返回原结果 |
| F-04 | Publisher 领取后、Publish 前 | 租约过期重领 |
| F-05 | Publish 后、Mark 前 | 重复投递安全 |
| F-06 | Worker 领取后、Checkpoint 前 | 新 Worker 重做未提交节点 |
| F-07 | Checkpoint 后、业务提交前 | 以业务事实判断未成功 |
| F-08 | 业务提交后、ACK 前 | 重投返回保存结果 |
| F-09 | Provider 写入前 | 可安全重试 |
| F-10 | Provider 写入后、响应前 | Reconcile/Manual Review，不盲重试 |
| F-11 | Binding 写入后、Event 前 | 事务/恢复保持一致 |
| F-12 | Stop/Delete 与结果提交竞争 | 版本/fencing 决定，正文不复活 |

## 24. 需求追踪矩阵

| 设计能力 | 测试用例 |
| --- | --- |
| OIDC/会话 | AUTH-001～014、SEC-001～003 |
| tenant + owner 隔离 | TENANT-001～010、OAUTH-008、SSE-002 |
| 短事务与连接池 | DB-001～007、PERF-001～004 |
| Outbox | OUTBOX-001～014、DR-005 |
| Celery/Inbox | QUEUE-001～010 |
| 租约与 Fencing | LEASE-001～012、DR-004 |
| Graph 恢复/版本 | GRAPH-001～010、DEPLOY-004 |
| 停止与删除 | CANCEL-001～008、E2E-004/009 |
| SSE 恢复 | SSE-001～010、E2E-008 |
| OAuth/Secret/Webhook | OAUTH-001～012 |
| 安全/审计 | COMMON-008、SEC-001～012 |
| 可观测/告警 | OBS-001～008 |
| 部署/迁移 | DB-008～010、DEPLOY-001～010 |
| 备份/灾备 | DR-001～008 |
| SLO/容量 | PERF-001～010 |
| 业务全链路 | E2E-001～010 |

## 25. CI 与发布门禁

### 25.1 每次提交

- Python Unit/Contract/Integration。
- Web Unit/Component。
- OpenAPI Drift。
- Migration Lint/Empty DB Migration。
- Secret Marker Scan。
- Queue Payload Schema。
- P0 Fake OIDC/Provider。

### 25.2 合并门禁

- 真实 PostgreSQL/Redis 多实例测试。
- Celery 重复投递、Worker Kill 和租约接管。
- P0 Browser E2E。
- 安全测试与依赖/镜像扫描。
- Step 1～9 全量回归。

### 25.3 Staging 发布门禁

- 真实 OIDC 和受限 Provider Sandbox。
- N/N+1 滚动发布与回滚。
- Load/Soak。
- Alert/Runbook 演练。
- Backup Restore 与 Redis 重建。
- 固定 Eval/Ablation。

### 25.4 Production 发布门禁

- 所有 P0/P1 通过，无未批准 Skip/XFail。
- 数据迁移校验和回滚路径通过。
- 当前容量与 SLO 证据。
- 24 小时内备份成功且最近恢复演练有效。
- 已知风险、Owner、截止时间和回退条件明确。

## 26. 测试 Artifact

每次发布保留：

- Git Commit、镜像 Digest、Schema 版本。
- Graph/Prompt/Tool/Payload 版本。
- 测试环境拓扑与配置 Hash。
- Unit/Integration/E2E/Security/Chaos/Load 报告。
- Eval 报告与模型版本。
- 故障注入时间线和最终数据库断言。
- Migration 校验、Backup/Restore 结果。
- Dashboard、告警和 Runbook 演练记录。
- Secret Marker 扫描报告。

Artifact 不保存真实 Token、用户正文、完整代码或 Provider 原始响应。

## 27. 完成判定

Step 10 测试完成必须同时满足：

1. 176 个用例全部实现，并与自动化测试路径建立映射。
2. 标准 CI 的 P0/P1 零 Skip/XFail。
3. Staging 的真实 OIDC/Provider、Chaos、Load 和 Restore 门禁通过。
4. 重复投递、Worker 脑裂、Redis 丢失和外部响应未知均无重复业务副作用。
5. 跨租户访问和敏感 Marker 泄漏为 0。
6. 初始 SLO、RPO、RTO 有真实测量结果。
7. Step 1～9 的业务、Grounding、人机确认和外部绑定合同无回归。
8. Production Readiness Review 给出可发布结论。

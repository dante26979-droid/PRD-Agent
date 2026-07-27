# M0 Agent Core 第七步测试方案：FastAPI、SSE 与 Next.js 工作台

> 文档状态：已验证  
> 版本：0.1  
> 日期：2026-07-24  
> 对应设计：[M0 Agent Core 第七步设计方案：可演示 Web 工作台](./2026-07-24-m0-agent-core-step-7-web-presentation-design.md)  
> 回归基线：Step 1～6 真实 PostgreSQL 完整项目 `105 passed`、零 Skip

## 1. 测试目标

本方案证明 Step 7 的 Web 层没有破坏已经完成的核心 Workflow，并验证：

1. API 不能绕过 owner、版本、hash、幂等和确认门禁。
2. 公共 Read Model 稳定、完整且不泄露内部数据。
3. SSE 可按游标重放，断线后不会丢失或重复业务操作。
4. Next.js 只执行服务端返回的可用动作。
5. 从首条消息到最终 PRD 的浏览器链路可在真实 PostgreSQL 上完成。
6. 错误、空状态、并发更新、网络中断和恶意 Markdown 均可安全恢复。
7. Step 1～6 的 105 个测试继续全部通过。

## 2. 测试分层

| 层级 | 被测对象 | 主要依赖 |
| --- | --- | --- |
| 纯单元 | Mapper、Action Resolver、错误映射、游标、事件映射 | 无数据库、无网络 |
| API 服务 | FastAPI Router、依赖、Schema、中间件 | Memory Store + Stub Model |
| Repository 合同 | owner、列表、游标事件 | Memory / PostgreSQL 参数化 |
| API 集成 | FastAPI + 真实 PostgreSQL | PostgreSQL + Stub Model |
| 前端组件 | React 组件、状态和可访问性 | Vitest + Testing Library + MSW |
| 浏览器 E2E | Next.js + FastAPI + PostgreSQL | Playwright + Stub Model |
| 非功能 | 安全、性能、构建、断线恢复 | Production Build |

普通测试禁止调用真实模型或外部网络。完整流程使用固定 Scripted Model，使 Outline、Unit、Grounding 和 Quality 结果可重复。

## 3. 建议测试目录

```text
tests/
  api/
    test_health.py
    test_task_queries.py
    test_task_commands.py
    test_error_contract.py
    test_owner_scope.py
    test_event_stream.py
    test_openapi.py
  application/
    test_public_mapper.py
    test_action_resolver.py
  storage/
    test_web_query_contract.py
    test_postgres_web_integration.py
  web-e2e/
    test_complete_prd.py

web/
  components/**/*.test.tsx
  lib/**/*.test.ts
  e2e/*.spec.ts
```

## 4. 测试环境矩阵

| 环境 | 后端 | 数据库 | 前端 | 用途 |
| --- | --- | --- | --- | --- |
| Unit | 纯函数/TestClient | 无或 Memory | jsdom | 快速反馈 |
| Contract | Repository | Memory + PostgreSQL 16 | 无 | 行为一致 |
| Integration | Uvicorn/FastAPI | PostgreSQL 16 | 无 | 事务和 HTTP |
| E2E | Uvicorn | PostgreSQL 16 | Next.js Production | 用户链路 |

最低浏览器门禁：

- Chromium：P0 全量。
- Firefox：主流程 Smoke。
- WebKit：主流程 Smoke。
- 1280×800 桌面和 390×844 移动视口。

## 5. Fixture 与测试数据

### 5.1 固定身份

- `local-user`：默认 Portfolio 用户。
- `other-user`：越权测试用户。
- API 测试通过依赖覆盖注入 principal，禁止从请求体读取 actor。

### 5.2 固定任务

至少准备：

- `clarifying-task`
- `outline-review-task`
- `unit-confirmation-task`
- `grounding-blocked-task`
- `quality-blocked-task`
- `final-review-task`
- `completed-task`
- `failed-task`
- `other-user-task`

### 5.3 时间与 ID

- 后端注入 Fake Clock 和 ID Factory。
- 前端冻结系统时间。
- 游标和事件 sequence 使用确定值。
- 测试不得依赖真实等待；SSE Poller 使用可推进时钟。

## 6. 公共 Read Model 单测

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S7-RM-001 | CLARIFYING + WAITING_USER | `display_status=NEEDS_INPUT` | P0 |
| S7-RM-002 | Run 为 RUNNING | `display_status=IN_PROGRESS` | P0 |
| S7-RM-003 | Outline 待确认 | `AWAITING_CONFIRMATION` | P0 |
| S7-RM-004 | Unit 待确认 | 只返回当前 Unit 的确认动作 | P0 |
| S7-RM-005 | Task COMPLETED | `COMPLETED` 且只有 Reopen 动作 | P0 |
| S7-RM-006 | Task/Run FAILED | `FAILED`，不伪造 Retry 动作 | P0 |
| S7-RM-007 | 多个 Outline 历史版本 | 公开模型只选当前非 Superseded 版本 | P0 |
| S7-RM-008 | 多个 Document Version | 当前文档、version、hash 一致 | P0 |
| S7-RM-009 | Grounding Blocked | 返回公开 Issue 数量和摘要 | P0 |
| S7-RM-010 | Quality 有 Blocker/Error | Finalize 不在 `available_actions` | P0 |
| S7-RM-011 | Quality 可确认 | Finalize 携带精确 document/version/hash | P0 |
| S7-RM-012 | Revision Plan 可批准 | action 只含服务端允许的 issue/unit IDs | P0 |
| S7-RM-013 | Investigation 含完整工具参数 | 公开模型不返回敏感参数和 Token | P0 |
| S7-RM-014 | Evidence 含绝对路径 | 只返回允许的相对 Locator | P0 |
| S7-RM-015 | 内部对象新增未知字段 | 公共 Schema 不意外透传 | P1 |
| S7-RM-016 | 未知枚举 | 显示状态可降级，动作列表为空 | P0 |

## 7. Action Resolver 单测

| ID | 场景 | 可用动作 | 优先级 |
| --- | --- | --- | --- |
| S7-AR-001 | 需要澄清 | `SEND_MESSAGE` | P0 |
| S7-AR-002 | Outline 待确认 | `SEND_MESSAGE`, `CONFIRM_OUTLINE` | P0 |
| S7-AR-003 | Outline 已确认、Unit 正在生成 | 无确认动作 | P0 |
| S7-AR-004 | 当前 Unit 待确认 | `CONFIRM_UNIT` | P0 |
| S7-AR-005 | 非当前 Unit 待确认数据损坏 | 不提供确认动作并记录错误 | P0 |
| S7-AR-006 | Grounding Blocked | 不提供 `CONFIRM_UNIT` | P0 |
| S7-AR-007 | Quality Revision 待批准 | `APPROVE_REVISION_PLAN` | P0 |
| S7-AR-008 | Final Review 无开放问题 | `FINALIZE_PRD` | P0 |
| S7-AR-009 | Completed | `REOPEN_PRD` | P0 |
| S7-AR-010 | Failed/Stopped | P0 不提供未实现动作 | P0 |
| S7-AR-011 | Task version 改变 | 新动作使用新版本 | P0 |
| S7-AR-012 | Action Resolver 与领域 Policy 冲突 | 测试失败，不以前端规则兜底 | P0 |

## 8. Repository owner、列表和事件合同测试

以下用例必须对 Memory 和 PostgreSQL 使用同一参数化测试：

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S7-RP-001 | 创建任务 | owner 为命令 principal | P0 |
| S7-RP-002 | 查询自己的 Task | 返回快照 | P0 |
| S7-RP-003 | 查询其他 owner 的 Task | `NotFound` | P0 |
| S7-RP-004 | 列表混有两个 owner | 只返回当前 owner | P0 |
| S7-RP-005 | 列表默认排序 | `updated_at, task_id` 倒序稳定 | P0 |
| S7-RP-006 | 相同更新时间 | task_id 决定稳定次序 | P1 |
| S7-RP-007 | 下一页游标 | 无重复、无遗漏 | P0 |
| S7-RP-008 | limit=0/负数/超上限 | 拒绝或规范化为合同值 | P0 |
| S7-RP-009 | 不透明游标损坏 | 稳定的 Invalid Cursor 错误 | P0 |
| S7-RP-010 | 事件 after_sequence=0 | 返回最早事件 | P0 |
| S7-RP-011 | 事件断点续读 | 只返回 sequence 更大的事件 | P0 |
| S7-RP-012 | 事件顺序 | 严格递增且单任务唯一 | P0 |
| S7-RP-013 | 其他 owner 查询事件 | `NotFound` | P0 |
| S7-RP-014 | 事件批次 limit | 正确截断并可继续 | P1 |
| S7-RP-015 | 任务与事件并发写 | sequence 无重复 | P0 |
| S7-RP-016 | owner 迁移重复执行 | 幂等且现有数据为 local-user | P0 |

## 9. FastAPI 健康、中间件与 Schema 测试

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S7-API-001 | Live | 200，不访问模型 | P0 |
| S7-API-002 | DB 可用且迁移正确 | Ready 200 | P0 |
| S7-API-003 | DB 不可用 | Ready 503，Live 仍 200 | P0 |
| S7-API-004 | 请求不带 X-Request-ID | 服务端生成并回传 | P1 |
| S7-API-005 | 请求带合法 X-Request-ID | 保留或映射并回传 | P1 |
| S7-API-006 | 超长/非法 Request ID | 重新生成，不注入日志 | P0 |
| S7-API-007 | 未知 JSON 字段 | 422 | P1 |
| S7-API-008 | 空 Body/错误 Content-Type | 稳定 4xx Error Schema | P0 |
| S7-API-009 | OpenAPI 生成 | 所有 P0 路由和错误模型存在 | P0 |
| S7-API-010 | OpenAPI Client 重生成 | Git diff 为空 | P0 |

## 10. 任务查询 API 测试

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S7-Q-001 | 无任务列表 | 200、items 为空、next_cursor 为空 | P0 |
| S7-Q-002 | 有 21 个任务默认 limit=20 | 返回 20 和 next_cursor | P0 |
| S7-Q-003 | 翻到末页 | 剩余项且 next_cursor 为空 | P0 |
| S7-Q-004 | 任务更新后重新查询首页 | 更新任务置顶 | P1 |
| S7-Q-005 | 获取自己的 TaskDetail | 全部公开分区正确 | P0 |
| S7-Q-006 | Task 不存在 | 404 `RESOURCE_NOT_FOUND` | P0 |
| S7-Q-007 | Task 属于其他用户 | 同样返回 404 | P0 |
| S7-Q-008 | Task 有内部异常字段 | 不泄露堆栈/绝对路径 | P0 |
| S7-Q-009 | 文档内容较大 | 响应 Schema 有界且成功 | P1 |
| S7-Q-010 | 数据库读取失败 | 500/503 使用统一错误合同 | P0 |

## 11. 命令 API 测试

### 11.1 通用门禁

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S7-CM-001 | 缺少 Idempotency-Key | 422/400，不执行命令 | P0 |
| S7-CM-002 | 空 Idempotency-Key | 拒绝 | P0 |
| S7-CM-003 | 同键同输入重放 | 返回首次结果，不重复写入 | P0 |
| S7-CM-004 | 同键不同输入 | 409 `IDEMPOTENCY_CONFLICT` | P0 |
| S7-CM-005 | 旧 expected version | 409 `TASK_VERSION_CONFLICT` | P0 |
| S7-CM-006 | 非法状态命令 | 409 `INVALID_TRANSITION` | P0 |
| S7-CM-007 | 请求体伪造 actor_id | Schema 拒绝或忽略，使用 principal | P0 |
| S7-CM-008 | 命令其他用户 Task | 404 且无任何写入 | P0 |
| S7-CM-009 | 核心服务抛未知异常 | 500 脱敏，事务回滚 | P0 |
| S7-CM-010 | 成功命令 | 响应为最新 TaskDetail 和版本 | P0 |

### 11.2 逐命令场景

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S7-CM-011 | 首条消息为空 | 不创建 Task | P0 |
| S7-CM-012 | 首条消息合法 | 原子创建 Task/Message/Run/Event | P0 |
| S7-CM-013 | 澄清回复 | 新消息、Brief Version 和状态正确 | P0 |
| S7-CM-014 | Outline 确认正确版本 | 推进并返回首个 Unit | P0 |
| S7-CM-015 | 确认旧 Outline Version | 409，无 Unit 被推进 | P0 |
| S7-CM-016 | 当前 Unit 确认 | 生成 Section 并推进下一 Unit | P0 |
| S7-CM-017 | 确认非当前 Unit | 409，无 Section 写入 | P0 |
| S7-CM-018 | Grounding Blocked Unit | 不能通过 API 确认 | P0 |
| S7-CM-019 | 批准 Revision | 只重开允许的 Unit 闭包 | P0 |
| S7-CM-020 | 伪造 issue/unit IDs | 409/422，无修订 | P0 |
| S7-CM-021 | Finalize 正确 document/hash | Task COMPLETED | P0 |
| S7-CM-022 | Finalize 旧 document/hash | 409，Task 不完成 | P0 |
| S7-CM-023 | 有开放 Blocker Finalize | 409 | P0 |
| S7-CM-024 | Completed Task Reopen | 新版本链且历史文档不变 | P0 |
| S7-CM-025 | Reopen 无 Unit 或空 reason | 422/409 | P0 |

## 12. 错误契约与脱敏测试

| ID | 输入/故障 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S7-ER-001 | Pydantic 校验失败 | 422、字段位置可读 | P0 |
| S7-ER-002 | `NotFound` | 404 稳定码 | P0 |
| S7-ER-003 | `InvalidTransition` | 409 稳定码 | P0 |
| S7-ER-004 | `VersionConflict` | 409 且 `retryable=true` | P0 |
| S7-ER-005 | `IdempotencyConflict` | 409 且不可自动重放新输入 | P0 |
| S7-ER-006 | 模型异常含 Prompt | 响应不含 Prompt | P0 |
| S7-ER-007 | PostgreSQL 异常含 DSN | 响应和普通日志不含 DSN | P0 |
| S7-ER-008 | 工具异常含绝对路径 | 响应只含公开摘要 | P0 |
| S7-ER-009 | 未捕获异常 | correlation_id 可用于定位 | P1 |
| S7-ER-010 | 404 自有/他人资源 | 响应不可区分资源存在性 | P0 |

## 13. SSE 与轮询测试

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S7-SSE-001 | 无 Last-Event-ID | 从指定/default 游标开始 | P0 |
| S7-SSE-002 | after_sequence=3 | 首个事件 sequence > 3 | P0 |
| S7-SSE-003 | 同时提供 Header 和 Query | 按文档优先级且可预测 | P1 |
| S7-SSE-004 | 存量 150 个事件 | 分批重放，顺序完整 | P0 |
| S7-SSE-005 | 新事件写入 | 最迟轮询周期内发出 | P0 |
| S7-SSE-006 | 空闲连接 | 15 秒 Heartbeat | P1 |
| S7-SSE-007 | 客户端断开 | Generator 及时退出，无泄漏 | P0 |
| S7-SSE-008 | 重连相同游标 | 客户端去重，无重复 UI 卡片 | P0 |
| S7-SSE-009 | 中间 sequence 缺口 | 客户端刷新 TaskDetail | P0 |
| S7-SSE-010 | 未知内部事件 | 不透传私有 payload，快照仍可恢复 | P0 |
| S7-SSE-011 | 其他 owner 连接 | 404/断开，不发送事件 | P0 |
| S7-SSE-012 | Event payload 含私有字段 | Mapper 移除 | P0 |
| S7-SSE-013 | DB 短暂失败 | 关闭/重试，不发送伪事件 | P0 |
| S7-SSE-014 | SSE 连续失败三次 | Web 切换 3 秒轮询 | P0 |
| S7-SSE-015 | 轮询期间版本未变 | 不重复渲染/Toast | P1 |
| S7-SSE-016 | 网络恢复 | 恢复 SSE 并带最新 sequence | P0 |
| S7-SSE-017 | 页面隐藏后恢复 | 补拉事件和快照 | P1 |
| S7-SSE-018 | 10 个并发本地连接 | 无乱序、无跨任务串流 | P1 |

SSE 测试不得使用真实 `sleep(15)`；通过 Fake Clock 或可配置 Heartbeat/Poll Interval 推进。

## 14. 前端组件测试

### 14.1 TaskSidebar

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S7-WEB-001 | 列表加载 | Skeleton，不阻塞新建入口 | P1 |
| S7-WEB-002 | 空列表 | 空状态和新建按钮 | P0 |
| S7-WEB-003 | 列表错误 | 错误文案和重试 | P0 |
| S7-WEB-004 | 标题过长 | 省略且可访问名称完整 | P1 |
| S7-WEB-005 | 点击任务 | 路由切换并标记选中 | P0 |
| S7-WEB-006 | 不同状态 | Badge 与服务端 display_status 一致 | P0 |
| S7-WEB-007 | 加载下一页 | 保持已有项且无重复 | P1 |

### 14.2 工作流组件

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S7-WEB-008 | Brief 有开放问题 | 清晰展示并允许回复 | P0 |
| S7-WEB-009 | 三级 Outline | 层级语义和 Unit 分组正确 | P0 |
| S7-WEB-010 | Outline 无确认 Action | 不渲染确认按钮 | P0 |
| S7-WEB-011 | Unit 待确认 | 内容、状态、确认动作正确 | P0 |
| S7-WEB-012 | Grounding Blocked | 警告可读且确认不可用 | P0 |
| S7-WEB-013 | Quality Issues | 严重级别、影响范围和批准动作正确 | P0 |
| S7-WEB-014 | 命令提交中 | 相同动作禁用，其他任务可切换 | P1 |
| S7-WEB-015 | 命令成功 | 使用响应快照更新页面 | P0 |
| S7-WEB-016 | 409 版本冲突 | 保留输入、刷新并提示 | P0 |
| S7-WEB-017 | 500/网络错误 | 内容不丢失，可手动重试 | P0 |
| S7-WEB-018 | 重复成功响应 | 无重复消息/Toast | P1 |

### 14.3 PRD 与安全 Markdown

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S7-WEB-019 | 标题/列表/表格/代码 | 正确渲染 | P0 |
| S7-WEB-020 | `<script>` | 不执行且不生成危险节点 | P0 |
| S7-WEB-021 | `javascript:` 链接 | 被移除或禁用 | P0 |
| S7-WEB-022 | `<img onerror>` | 事件属性不可执行 | P0 |
| S7-WEB-023 | 外部链接 | 新窗口策略安全 | P1 |
| S7-WEB-024 | Evidence Appendix | 锚点导航和 Locator 可读 | P0 |
| S7-WEB-025 | 超长代码/URL | 不破坏布局 | P1 |

## 15. 可访问性与响应式测试

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S7-A11Y-001 | 仅键盘创建任务 | 可完成 | P0 |
| S7-A11Y-002 | 仅键盘确认 Outline/Unit | 可完成 | P0 |
| S7-A11Y-003 | Dialog 打开/关闭 | 焦点锁定并回到触发按钮 | P0 |
| S7-A11Y-004 | 状态变化 | `aria-live` 简洁播报，不泄露全文 | P1 |
| S7-A11Y-005 | 状态仅靠颜色区分 | 测试失败 | P0 |
| S7-A11Y-006 | 标题层级 | 页面结构连续 | P1 |
| S7-A11Y-007 | 390px 视口 | 页签可用、无水平页面溢出 | P0 |
| S7-A11Y-008 | 200% Zoom | 主要动作可见且不重叠 | P1 |
| S7-A11Y-009 | prefers-reduced-motion | 关闭非必要动画 | P2 |
| S7-A11Y-010 | axe 扫描 | P0 页面无 serious/critical 问题 | P0 |

## 16. 浏览器 E2E

| ID | 完整场景 | 核心断言 | 优先级 |
| --- | --- | --- | --- |
| S7-E2E-001 | 新建任务 → 澄清 → 大纲 | 任务置顶、消息恢复、Outline 可确认 | P0 |
| S7-E2E-002 | 大纲 → 多 Unit → Final Review | 每次只有当前 Unit 可确认 | P0 |
| S7-E2E-003 | 完整 Finalize | 正确 hash，Task COMPLETED | P0 |
| S7-E2E-004 | Quality Blocker → Revision → 新文档 | 旧文档不覆盖、问题关闭后可完成 | P0 |
| S7-E2E-005 | Grounding Blocked | Unit 不能确认，公开原因可见 | P0 |
| S7-E2E-006 | Completed → Reopen | 选中 Unit 重开，历史文档保留 | P0 |
| S7-E2E-007 | 页面刷新 | 从 PostgreSQL 恢复相同状态 | P0 |
| S7-E2E-008 | SSE 中断 → 轮询 → 恢复 | 无丢失和重复业务动作 | P0 |
| S7-E2E-009 | 两标签页并发确认 | 一个成功，一个 409 并刷新 | P0 |
| S7-E2E-010 | 切换两个任务 | 消息、PRD、事件完全隔离 | P0 |
| S7-E2E-011 | other-user 直接访问 URL | 404，不短暂渲染数据 | P0 |
| S7-E2E-012 | FastAPI 重启 | 已保存 Task 恢复，页面可重连 | P1 |

E2E 必须使用 Next.js Production Build；开发服务器只用于本地调试，不能作为发布门禁。

## 17. 故障注入与失败用例

| ID | 故障点 | 预期恢复 | 优先级 |
| --- | --- | --- | --- |
| S7-FI-001 | StartTask 模型结构失败 | 返回公开错误，失败 Task 可查询，无半写入 | P0 |
| S7-FI-002 | 命令事务提交前异常 | 任务、事件、幂等记录整体回滚 | P0 |
| S7-FI-003 | 命令成功但 HTTP 响应断开 | 同 Idempotency-Key 重试得到原结果 | P0 |
| S7-FI-004 | TaskDetail 读取中 DB 断开 | 错误边界展示重试，旧 UI 标记为陈旧 | P0 |
| S7-FI-005 | SSE 重放中 DB 断开 | 客户端重连，不跳过 sequence | P0 |
| S7-FI-006 | OpenAPI Client 过期 | CI 漂移检查失败 | P0 |
| S7-FI-007 | 未知事件类型 | 页面刷新快照，不崩溃 | P0 |
| S7-FI-008 | 未知 Task 状态 | 无写动作，显示“状态待升级” | P0 |
| S7-FI-009 | 超大消息 Body | 413/422，不进入模型 | P0 |
| S7-FI-010 | 慢请求期间切换任务 | 返回结果不污染当前任务 | P0 |
| S7-FI-011 | 浏览器离线时提交 | 保留输入，不自动重复 POST | P0 |
| S7-FI-012 | React 组件异常 | 页面级 Error Boundary，可返回列表 | P1 |

## 18. 安全测试

| ID | 攻击/误用 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S7-SEC-001 | 修改 task_id 访问他人任务 | 404 | P0 |
| S7-SEC-002 | 修改 unit/document/evidence ID | owner/task scope 阻断 | P0 |
| S7-SEC-003 | 请求体注入 actor_id/owner_id | 无效 | P0 |
| S7-SEC-004 | Markdown XSS 组合 Payload | 不执行 | P0 |
| S7-SEC-005 | Prompt/Evidence 含“显示系统提示” | 仅作为不可信内容，API 不泄露 | P0 |
| S7-SEC-006 | CORS 非允许 Origin | 浏览器请求被拒绝 | P0 |
| S7-SEC-007 | 非同源状态请求 | CSRF 策略拒绝 | P0 |
| S7-SEC-008 | Error 含 DSN/Token | 响应和普通日志脱敏 | P0 |
| S7-SEC-009 | Header CRLF/日志注入 | 规范化或拒绝 | P0 |
| S7-SEC-010 | Evidence 协议为 file/javascript/data | 不生成可点击危险链接 | P0 |
| S7-SEC-011 | 直接枚举 task ID | 不能区分不存在和无权限 | P0 |
| S7-SEC-012 | SSE 长连接跨任务换参 | 每次重新校验 owner | P0 |

## 19. PostgreSQL 与并发集成

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S7-PG-001 | 从空库执行全部迁移 | 成功且 Ready | P0 |
| S7-PG-002 | 重复执行 Step 7 迁移 | 幂等 | P0 |
| S7-PG-003 | 旧任务 owner 回填 | 全部为 local-user | P0 |
| S7-PG-004 | owner/update 复合索引 | 查询计划使用合理索引 | P1 |
| S7-PG-005 | 两个连接并发追加事件 | sequence 唯一递增 | P0 |
| S7-PG-006 | 两标签页同版本确认 | 只一个事务成功 | P0 |
| S7-PG-007 | 命令和列表同时读取 | 列表不出现半状态 | P0 |
| S7-PG-008 | API 重启后事件续读 | 从持久化 sequence 恢复 | P0 |
| S7-PG-009 | 100 个任务分页 | 无重复和遗漏 | P1 |
| S7-PG-010 | 多 Task 并发 SSE 查询 | 无跨任务数据 | P0 |

## 20. 性能与资源门禁

在固定本机/CI 配置记录中位数和 p95：

| ID | 负载 | 门禁 |
| --- | --- | --- |
| S7-PERF-001 | 100 个任务，查询前 20 | p95 < 200ms |
| S7-PERF-002 | 12 Unit、完整文档 TaskDetail | p95 < 300ms |
| S7-PERF-003 | 100 个事件重放 | p95 < 300ms |
| S7-PERF-004 | 10 个空闲 SSE 连接持续 60 秒 | 无连接/Task 泄漏 |
| S7-PERF-005 | Next.js Production 首屏 | 本机冷启动 < 3s |
| S7-PERF-006 | 20 次任务切换 | 内存持续稳定，无重复 Listener |

性能失败先作为 P1 门禁；owner 隔离、顺序、幂等或资源泄漏失败始终为 P0。

## 21. 回归与构建命令

实际实现时在仓库中固定脚本，建议门禁为：

```bash
pytest -q
pytest -q tests/api tests/storage/test_web_query_contract.py
pytest -q tests/storage/test_postgres_web_integration.py
npm --prefix web run lint
npm --prefix web run typecheck
npm --prefix web run test
npm --prefix web run build
npm --prefix web run test:e2e
```

要求：

- Python 全量至少保持 `105 passed + Step 7 新增测试`。
- 发布门禁零 Skip、零 XFail。
- 前端 lint/typecheck/unit/build 全部通过。
- Playwright P0 全部通过。
- PostgreSQL 集成不可用时 CI 应失败，不得自动回退 Memory 后报告成功。
- 不把真实模型、外部网络和开发服务器作为测试前置。

## 22. 测试执行顺序

1. Read Model、Action Resolver、错误映射纯单元测试。
2. owner、任务列表、事件游标 Repository 合同。
3. FastAPI 查询和命令合同。
4. SSE 重放、重连和降级。
5. 前端组件、Markdown 安全和可访问性。
6. 真实 PostgreSQL API 集成。
7. Next.js Production Build 浏览器 E2E。
8. 故障注入、安全和并发。
9. Step 1～6 全量回归与性能记录。

## 23. 发布阻断规则

出现以下任一情况，Step 7 不得标记完成：

- 任意跨 owner 数据泄露。
- API 可绕过 expected version、Document hash 或幂等键。
- 前端出现服务端未授权的动作。
- SSE 丢事件后无法通过快照恢复。
- Markdown 可执行脚本或危险协议。
- 真实 PostgreSQL 合同与 Memory 不一致。
- Production Build 或 P0 E2E 失败。
- Step 1～6 出现新增失败或发布门禁 Skip。
- 错误响应泄露堆栈、DSN、Token、绝对路径或模型私有内容。

## 24. 测试结果记录模板

```text
日期：
Commit：
Python：
Node：
PostgreSQL：
Browser：

Step 1～6 回归：
Step 7 后端单元/API：
Repository 合同：
真实 PostgreSQL 集成：
前端 Unit/Lint/Typecheck：
Next.js Production Build：
Playwright E2E：
安全/故障注入：
性能：
Skip/XFail：

失败用例：
修复 Commit：
复测结果：
最终结论：PASS / FAIL
```

## 25. 完成判定

只有同时满足以下条件，Step 7 才通过测试：

1. 设计文档 Definition of Done 全部完成。
2. 本方案全部 P0 用例通过。
3. P1 未通过项有明确原因且不影响安全、正确性和 30 秒 Demo。
4. 真实 PostgreSQL 完整链路通过。
5. Step 1～6 的 105 个测试无回归。
6. 前端 Production Build 和三浏览器 Smoke 通过。
7. 测试结果写入本文件的实现记录或独立验证报告。

以下为初始计划范围；实际已执行的门禁与结果见第 26 节。

## 26. 2026-07-26 实际验证结果

| 门禁 | 结果 |
| --- | --- |
| Python 全量 + 真实 PostgreSQL | `119 passed`，零 Skip |
| Step 7 API/Read Model/owner 测试 | 已包含在 Python 全量 |
| PostgreSQL owner/列表/事件游标 | `1 passed` |
| Vitest 前端组件与 Markdown 安全 | `4 passed` |
| TypeScript strict typecheck | 通过 |
| Next.js 16 Production Build | 通过 |
| OpenAPI → TypeScript 类型生成 | 通过 |
| 浏览器桌面完整流程 | 通过 |
| 390×844 响应式检查 | 通过，`scrollWidth == clientWidth` |

浏览器真实链路覆盖：

1. 新建任务。
2. 任务列表更新。
3. 确认 Outline。
4. 确认 Unit。
5. 发现旧离线模板的 Quality Error。
6. 批准 Revision Plan。
7. 生成并确认满足门禁的新 Unit。
8. 生成 Document Version 2。
9. 使用精确 Document hash 最终确认。
10. 页面和侧边栏均进入 `COMPLETED`。

验证中发现并修复：

- 离线模型验收模板缺少显式条件词，导致 Quality Checker 必然阻断。
- Read Model 曾返回历史 Quality Issue，导致修订成功后界面仍显示已解决问题。
- Next.js Google Font 构建依赖外网，已改为本地系统字体栈。

未在本次实现范围内的 204 个计划场景仍作为后续扩展测试清单。

# PRD Agent 当前仓库实现审核

> 审核日期：2026-07-27  
> 审核对象：当前工作树，包含未提交和未跟踪文件  
> 对照基线：V1.1 PRD、V1.1 总体设计、Step 1～7 设计与测试方案  
> 审核结论：**演示主路径有条件通过；Step 7 完整 DoD 不通过；Step 8 可进入设计，不宜直接进入功能实现**

## 1. 审核范围与口径

本次不是针对某个 Commit 或 PR 的增量评审，而是对当前本地项目快照进行全量审核。原因是当前 `HEAD` 仍停留在 Step 2 文档提交，Step 3～7 的大部分代码、测试和文档尚未纳入 Git 跟踪；仅使用 `git diff` 会漏掉主要实现。

审核分为两个互相独立的维度：

1. **Standards**：实现是否遵守总体设计、分层边界、安全规则、测试与交付约定。
2. **Spec**：当前行为是否真正覆盖 PRD 和 Step 1～7 已承诺能力。

## 2. 可复现验证结果

| 门禁 | 本次结果 | 判定 |
| --- | --- | --- |
| Python 全量 + 真实 PostgreSQL | `119 passed`，1 个非阻断弃用警告 | 通过 |
| Python Compile | 通过 | 通过 |
| Vitest | `4 passed` | 通过，但覆盖不足 |
| TypeScript strict typecheck | 通过 | 通过 |
| Next.js Production Build | 通过 | 通过 |
| Git whitespace | 通过 | 通过 |
| Direct Prompt Eval | 30/30 Run 完成 | 可复现 |
| Minimal Workflow Eval | 30/30 Run 完成 | 可复现 |
| Single Retrieval Eval | 30/30 Run 完成 | 可复现 |
| Bounded Investigation Eval | 30/30 Run 完成 | 可复现 |

本次 Eval 的关键结果：

| 配置 | Requirement Coverage | Unknown Preservation | Unsupported Claim Rate |
| --- | ---: | ---: | --- |
| Direct Prompt | 0.05 | 0.10 | N/A |
| Minimal Workflow | 0.85 | 0.10 | N/A |
| Single Retrieval | 0.90 | 0.10 | N/A |
| Bounded Investigation | 0.90 | 0.10 | N/A |

Bounded Investigation 的 Coverage Completion Rate 为 `0.90`，Duplicate Action Rate 为 `0.0667`，No-progress Termination Rate 为 `0.10`。这些数据证明固定评测可以运行，但尚未证明 Loop 相比 Single Retrieval 在 PRD 质量上有增益，也没有完成 Loop + Grounding 消融。

## 3. Standards 审核

### S-01：Step 3～7 产物未进入版本控制

- 严重级别：P1
- 证据：当前 `HEAD` 为 `0cc5e81`，大量 `src/prd_agent/*`、`tests/*`、`web/*`、迁移和 Step 3～7 文档仍是 `??`。
- 影响：无法用 Commit SHA 复现当前实现；文档中的实现记录缺少可信代码固定点；后续审核无法安全区分新增回归与已有问题。
- 建议：Step 8 实现前先按领域、存储、API、Web、测试和文档拆分提交，禁止把当前全量工作树一次性标记为“Step 8”。

### S-SEC-01：三个解析工具绕过统一内容脱敏

- 严重级别：P0
- 证据：`read_file.py` 和 `search_text.py` 会调用 `RepositoryContentPolicy.redact()`；但 `src/prd_agent/tools/parse_database_schema.py:68-77`、`parse_openapi.py:91-119`、`find_related_tests.py:51-67` 将原始摘录、默认值、约束、枚举、Pattern 或匹配测试行直接写入 `ToolResult`。
- 违反：Step 3 设计要求所有仓库内容在进入 Tool Result、Evidence、日志和模型上下文前执行同一公开内容策略，不能由单个工具选择是否脱敏。
- 影响：Schema 默认密码、OpenAPI 示例 Token、测试夹具 Secret 等内容可通过解析工具进入 Evidence、SSE Read Model 或生成上下文。
- 建议：把脱敏收敛到 Tool Runner/Tool Result 构造边界；同时保留工具内的最小化摘录，并新增密码、Token、私钥和连接串的跨工具参数化回归。

### S-02：FastAPI 复用单个 PostgreSQL Connection

- 严重级别：P1
- 证据：`src/prd_agent/api/app.py:102-123` 在 App 级保存单个 Repository；`src/prd_agent/api/app.py:423-460` 的 SSE 循环与普通命令共享该 Connection。
- 违反：总体设计要求事务边界明确，Step 7 测试计划要求并发命令、多 SSE 连接和无跨任务串流。
- 影响：同步请求、异步 SSE 和事务可能互相串行或共享事务状态；一个连接故障会影响全部请求；无法可靠支撑 10 个并发 SSE 门禁。
- 建议：改为连接池 + 每请求/每次 SSE Poll 独立短事务；WorkflowService 不持有 App 全局可变 Connection。

### S-03：OpenAPI 生成类型未成为前端唯一事实源

- 严重级别：P1
- 证据：`web/lib/api/schema.d.ts` 已生成，但 `web/lib/api/types.ts:1-127` 仍手写重复类型，`web/lib/api/client.ts` 依赖后者。
- 违反：Step 7 设计 13 节明确禁止手写重复 Task/Unit/Quality 类型，并要求 Schema 漂移阻断 CI。
- 影响：后端字段或枚举变化可以在 TypeScript 编译通过的同时造成运行时漂移。
- 建议：Client 直接从生成 Schema 派生类型，加入离线 OpenAPI Snapshot 与 `git diff --exit-code` 门禁。

### S-04：公开事件只改名，未执行 Payload 白名单

- 严重级别：P1
- 证据：`src/prd_agent/api/app.py:441-448` 直接复制 `event.payload`；`src/prd_agent/api/mapping.py:30-44` 只映射事件名。
- 违反：Step 7 设计要求未知/私有字段不能透传，API 序列化前必须执行公开内容策略。
- 影响：未来内部事件一旦加入路径、模型错误或工具参数，浏览器边界会自动泄露。
- 建议：为每个公开事件定义独立 Pydantic Payload Schema，使用字段白名单构造，而不是复制内部字典。

### S-05：Readiness 未校验迁移版本

- 严重级别：P2
- 证据：`src/prd_agent/api/app.py:214-222` 只执行任务列表查询。
- 违反：Step 7 设计 15 节要求 Ready 同时验证数据库连接和目标迁移版本。
- 影响：部分表或索引缺失时可能 Ready=200，随后业务接口失败。
- 建议：引入迁移版本表；Ready 只做轻量版本检查，不用业务查询替代 Schema 检查。

### S-06：前端发布门禁脚本不完整

- 严重级别：P1
- 证据：`web/package.json:5-12` 无 `lint`、`test:e2e`、OpenAPI 漂移脚本；自动化前端测试仅 4 个。
- 违反：Step 7 测试计划 21～23 节要求 lint、P0 Playwright、无障碍、安全、并发和故障恢复门禁。
- 影响：当前“Build 通过”不能代表 Step 7 计划中的 P0 Web 行为通过。
- 建议：在 Step 8 Gate 0 补齐最小 Playwright 主路径、澄清、Grounding Block、断线、并发冲突和 Reopen 流程。

## 4. Spec 审核

### P0-01：默认 Web 主链路没有启用 Investigation 与 Grounding

- 证据：`src/prd_agent/api/app.py:108-113` 只注入 `DocumentQualityService`；`unit_context_provider` 和 `grounding_service` 均未配置。`src/prd_agent/workflow/service.py:447-456,563-614` 只有注入这些依赖时才调查和执行 Grounding。
- 对照：V1.1 总体设计 11.2～11.6、Step 6 Grounding 硬门禁、Step 7 “只展示已完成核心能力”约束。
- 实际行为：默认 Web 可以在没有任何 Evidence、Fact 或 Grounding Result 的情况下确认单元并完成任务。
- 结论：这是当前最严重的需求偏差。单独的模块测试通过，但实际 FastAPI 装配没有联通 Agent Core。

### P0-02：澄清问题存在于 Brief，但页面不展示

- 证据：离线模型会生成 `open_questions`（`src/prd_agent/workflow/stub_model.py:33-70`）；TaskDetail 返回完整 Brief；但 `web/components/task-workbench.tsx:456-475` 的 Brief 仅展示用户、场景、目标和范围。
- 对照：V1.1 总体设计 11.1 要求每轮显示 1～3 个澄清问题；Step 7 场景 A 要求页面展示问题和输入动作。
- 实际行为：输入“优化体验”等模糊需求时，用户看到可回复输入框，却看不到 Agent 想问什么。
- 结论：澄清路径不可用，应在进入 Step 8 前修复并增加浏览器回归。

### P1-01：TaskDetail 缺少 Investigation 与 Evidence

- 证据：`src/prd_agent/api/models.py:119-129` 和 `src/prd_agent/api/mapping.py:206-323` 只有 Brief、Outline、Grounding、Quality 和 Document。
- 对照：Step 7 设计 8.2、11.3、16 节要求 Investigation 目标、Coverage、Fact、Unknown、停止原因和脱敏 Evidence。
- 影响：Web 的“执行轨迹”实际上只有 Unit Rail 和静态说明，不能展示 Step 3～5 的核心求职项目价值。

### P1-02：Reopen 没有让用户选择 Unit 和填写原因

- 证据：`web/components/task-workbench.tsx:157-163` 自动提交服务端返回的全部 Unit，并使用固定原因；`339-345` 点击即执行。
- 对照：Step 7 设计 11.4 和场景 E 要求选择 Unit、填写非空原因并确认精确对象。
- 影响：可能重开不必要的依赖链，且审计记录不是用户真实原因。

### P1-03：SSE 降级后不会恢复 SSE，也不检查 Sequence 缺口

- 证据：`web/components/task-workbench.tsx:68-90` 连续失败后关闭 EventSource 并永久轮询；没有 `online`、`visibilitychange` 或 sequence gap 处理。
- 对照：Step 7 设计 10.1 第 6～10 条。
- 影响：网络恢复后仍停留在轮询；事件缺口只能依赖偶然刷新，不能按合同显式发现。

### P1-04：Step 5 Eval 与 Failure Analysis 未完成

- 证据：`eval/configs/` 没有 `loop-grounding-v1`；四个本次可运行配置的 `unsupported_claim_rate` 均为 N/A；`eval/reports/` 无固定报告；README 未展示真实对比数据。
- 对照：Step 5 设计 19 节、DoD 第 719～720 行，以及 V1.1 PRD 9.3～9.5。
- 影响：已有 Grounding 单测证明保守策略，但项目尚未提供 Grounding 带来的量化收益、失败类型与修复后回归。

### P1-05：前端幂等键不能覆盖“成功但响应丢失”

- 证据：`web/lib/api/client.ts:43-50` 每次调用 `command()` 都生成新 UUID。
- 对照：Step 7 测试 S7-FI-003 要求命令成功但响应断开后使用相同 Idempotency-Key 重试。
- 影响：用户手动重试会发出不同请求；StartTask 可能重复创建任务，其他命令可能得到版本冲突而不是首次结果。
- 建议：以待提交操作为单位生成并保留 Key，直到收到确定响应或用户明确放弃。

### P2-01：移动端与首页信息架构仅部分实现

- 证据：`web/app/page.tsx:1-5` 总是跳转新建页；`web/app/globals.css:1217-1225` 在移动端顺序堆叠，没有“对话 / PRD / 调查”页签。
- 对照：Step 7 设计 11.1～11.2。
- 影响：不阻塞桌面 Demo，但不满足原方案的信息架构。

### Scope Creep

未发现明显范围膨胀。当前偏差主要是设计承诺未完全落地，而不是实现了无关能力。

## 5. 综合判定

| 目标口径 | 当前判定 | 说明 |
| --- | --- | --- |
| 本地 30 秒桌面演示 | PASS | 创建、确认、修订、完成主路径可运行 |
| Step 1～7 自动化回归 | PASS WITH GAPS | 119 个 Python 和 4 个前端测试通过，但计划场景覆盖不足 |
| Step 7 完整 Definition of Done | FAIL | Grounding 未装配、追溯模型缺失、OpenAPI 类型重复、E2E/恢复门禁缺失 |
| Portfolio Core“有数据支持的可靠性结论” | PARTIAL | Eval 可运行，但 Loop + Grounding 与 Failure Analysis 未完成 |
| 进入 Step 8 设计 | PASS | 设计可继续 |
| 直接进入 Step 8 功能实现 | HOLD | 先完成 Gate 0 |

## 6. Step 8 前置 Gate 0

以下项目完成后，Step 8 才进入历史 PRD 功能切片：

1. 默认 FastAPI 装配真实 Investigation Context Provider 和 GroundingService。
2. 将 Repository 内容脱敏收敛到统一边界，修复三个解析工具的泄露路径。
3. 模糊需求页面显示 1～3 个 `open_questions`。
4. TaskDetail 增加脱敏 Investigation、Coverage、Fact、Unknown 和 Evidence。
5. SSE Payload 改为逐事件白名单，连接恢复合同补齐。
6. 前端使用 OpenAPI 生成类型，移除重复手写模型。
7. 增加 `loop-grounding-v1` Eval 和至少一个失败修复回归 Artifact。
8. 增加最小 Playwright P0 门禁。
9. 将 Step 3～7 当前实现纳入可审计 Git 提交。

Gate 0 不改变 Step 8 的业务范围；它只是确保历史 PRD 检索接入的是实际运行中的 Grounded Workflow，而不是测试专用 Hook。

# M0 Agent Core 第二部分实现方案：领域类型、Policy 与最小 PRD Workflow

> 日期：2026-07-21  
> 适用范围：M0 Agent Core / 阶段 1：最小 PRD 垂直切片  
> 前置 Step：`2026-07-21-m0-agent-core-step-1-eval-baseline.md`  
> 目标：在不接入 Repository Tools、Investigation Loop、Grounding、Web 和 Celery 的前提下，完成可恢复、可确认、可输出 Markdown 的最小 PRD Workflow。

## 1. 实现目标

第二步要把第一步的评测基线接入一个真实的领域 Workflow。它不追求覆盖完整 V1.1，而是验证主业务状态、用户确认和最小内容生成是否能够在持久化状态上稳定运行。

完成后，系统必须支持以下闭环：

```text
CLI 首条需求
  → 创建 Task 和 Agent Run
  → 提取 Requirement Brief
  → 信息不足时提出一次澄清
  → 生成 Outline
  → 等待用户确认 Outline
  → 生成一个 Confirmation Unit
  → 等待用户确认 Unit
  → 输出 Markdown PRD
```

本阶段的“一个确认单元”是有意限制的垂直切片。领域模型应支持多个单元，但不在本阶段实现动态 5～12 个单元、调查 Loop 或完整全文检查。

## 2. 与第一步的边界

### 2.1 复用第一步

- 复用 `EvalCase`、`BaselineConfig`、`ModelResponse` 和 `ModelAdapter` 的契约。
- 复用固定数据集、Stub Model、输入/输出哈希和报告机制。
- Workflow 生成的 Markdown 可以作为后续 Eval 配置的新增 Ablation：`Minimal Workflow`。
- 继续使用 PostgreSQL，禁止增加 SQLite 业务实现。

### 2.2 本阶段明确不实现

- `repo_tree`、`search_text`、`read_file`、OpenAPI、数据库 Schema 和测试查找工具。
- `InformationNeedPlanner`、`InvestigationGraph`、Evidence、Fact、Unknown 和 Source Grounding。
- 历史 PRD、pgvector、远程仓库、飞书和外部 Coding Agent。
- Next.js、FastAPI、SSE、Redis、Celery、Outbox、OIDC 和多租户。
- 多确认单元并发、章节动态增删和完整 PRD 全文质量检查。

模型即使在上下文中“建议查询代码”，也必须被当作普通文本处理，不能触发工具调用或改变业务状态。

## 3. 架构责任

### 3.1 三类状态分开管理

| 状态 | 唯一事实来源 | 本阶段用途 |
| --- | --- | --- |
| 用户可见业务状态 | PostgreSQL 业务表 | Task、Requirement Brief、Outline、Confirmation Unit、PRD 章节和用户确认 |
| 图执行状态 | LangGraph PostgreSQL Checkpointer | 当前节点、Graph State、interrupt cursor 和恢复位置 |
| 评测运行状态 | 第一阶段 Eval Store / PostgreSQL | Prompt、Model、Trial、指标和报告 |

LangGraph 只负责可恢复的图执行，不负责业务 exactly-once。业务 Policy 只读取业务状态和命令，不读取模型私有推理。PostgreSQL 业务表仍然是用户可见状态的唯一事实来源。

### 3.2 模块边界

```text
src/prd_agent/
  domain/
    enums.py              # Task、Run、Outline、Unit 状态
    entities.py           # 领域实体和值对象
    commands.py           # 用户命令和版本条件
    events.py             # 公开领域事件
  policies/
    task_policy.py        # Task 合法状态迁移
    run_policy.py         # 单活动 Run 与重试规则
    outline_policy.py     # 大纲锁定和确认单元顺序
    idempotency.py        # 命令幂等键和版本冲突
  workflow/
    state.py              # LangGraph Graph State
    graph.py              # 低层 Graph API 图定义
    nodes.py              # 结构化模型调用节点
    router.py             # 由业务状态决定下一节点
    service.py             # 启动、回复、确认、停止公共入口
  storage/
    postgres.py           # 业务表读写和事务
    migrations/           # Alembic 迁移
  rendering/
    markdown.py           # Requirement Brief、Outline、Unit 到 Markdown
  cli/
    workflow.py           # 最小命令行入口
```

模块必须通过公共接口通信。`workflow.nodes` 不得直接修改数据库；节点返回结构化结果，由 `WorkflowService` 在事务中应用 Policy 和持久化。

## 4. 领域模型

### 4.1 Task

```text
task_id: str
title: str | None
status: DRAFT | CLARIFYING | OUTLINE_REVIEW | GENERATING |
        FINAL_REVIEW | COMPLETED | FAILED | STOPPED | DELETING
version: int
current_outline_version: int | None
current_unit_sequence: int | None
created_at: datetime
updated_at: datetime
```

Task 的 `version` 是乐观锁版本。每个改变 Task 的命令必须携带 `expected_task_version`；版本不一致返回 `VersionConflict`，不能静默覆盖。

### 4.2 RequirementBrief

Requirement Brief 只保存用户目标与当前已确认上下文，不保存模型私有推理：

```json
{
  "problem": "待解决的问题",
  "target_users": ["目标角色"],
  "scenarios": ["核心使用场景"],
  "goals": ["预期结果"],
  "scope_in": ["本期范围"],
  "scope_out": ["非目标"],
  "product_rules": ["已知规则"],
  "success_metrics": ["可验证结果"],
  "current_state_fact_ids": [],
  "target_decisions": ["用户目标方案"],
  "authorized_assumptions": [],
  "agent_suggestions": [],
  "open_questions": ["待澄清问题"],
  "unknown_item_ids": [],
  "source_conflict_ids": []
}
```

第二步不允许写入 `current_state_fact_ids`、`unknown_item_ids` 和 `source_conflict_ids` 的真实来源，因为调查模块尚未实现；这些字段保留为空数组。未授权的模型假设只能进入 `agent_suggestions` 或 `open_questions`。

### 4.3 Outline 与 ConfirmationUnit

```text
OutlineVersion:
  outline_id
  task_id
  version
  status: DRAFT | PENDING_CONFIRMATION | CONFIRMED | SUPERSEDED
  nodes: OutlineNode[]
  confirmation_units: ConfirmationUnit[]
  created_at
  confirmed_at

OutlineNode:
  node_id
  sequence
  title
  purpose
  complexity: LOW | MEDIUM | HIGH
  required_information: []

ConfirmationUnit:
  unit_id
  outline_id
  sequence
  title
  status: PENDING | GENERATING | PENDING_CONFIRMATION | CONFIRMED | FAILED
  content: str | None
  version
```

第二步只允许生成一个 `ConfirmationUnit`，其 `sequence` 固定为 1。Outline 确认后，Outline 版本转为 `CONFIRMED`，不能原地修改；用户调整时创建新版本并将旧版本标记为 `SUPERSEDED`。

### 4.4 AgentRun 与命令

```text
AgentRun:
  run_id
  task_id
  thread_id
  graph_name: prd_workflow
  graph_version: m0.step2.v1
  status: QUEUED | RUNNING | WAITING_USER | SUCCEEDED | FAILED | STOPPED
  input_hash
  idempotency_key
  started_at
  ended_at
  error
```

公共命令：

```text
StartTask(message, idempotency_key)
ReplyToTask(task_id, message, expected_task_version, idempotency_key)
ConfirmOutline(task_id, outline_version, expected_task_version, idempotency_key)
ConfirmUnit(task_id, unit_id, expected_task_version, idempotency_key)
StopRun(task_id, run_id, expected_task_version, idempotency_key)
RetryRun(task_id, run_id, expected_task_version, idempotency_key)
```

本阶段 CLI 可以暂不暴露 Stop/Retry，但领域 Policy 和数据模型必须预留状态，避免后续改变实体语义。

## 5. Policy 设计

### 5.1 TaskTransitionPolicy

合法主状态只允许：

```text
DRAFT → CLARIFYING | DELETING
CLARIFYING → OUTLINE_REVIEW | FAILED | STOPPED | DELETING
OUTLINE_REVIEW → CLARIFYING | GENERATING | DELETING
GENERATING → OUTLINE_REVIEW | FINAL_REVIEW | FAILED | STOPPED | DELETING
FINAL_REVIEW → GENERATING | COMPLETED | DELETING
COMPLETED → GENERATING | DELETING
FAILED → CLARIFYING | OUTLINE_REVIEW | GENERATING | DELETING
STOPPED → CLARIFYING | OUTLINE_REVIEW | GENERATING | DELETING
```

Policy 输入旧状态、命令和操作者，输出新状态或明确拒绝原因。模型输出不在 Policy 的输入中承担状态决策职责。

### 5.2 RunPolicy

- 同一 `task_id` 最多存在一个 `QUEUED`、`RUNNING` 或 `WAITING_USER` 的活动 Run。
- `WAITING_USER` 不占用 Worker，但业务上仍表示当前任务等待用户输入。
- 用户确认或回复创建新的 `run_id`，沿用同一 `thread_id` 和兼容的 `graph_version`。
- 已完成 Run 使用相同 `idempotency_key` 重复提交时返回原结果，不重复调用模型。
- 失败 Run 只能通过显式 Retry 或用户新回复继续，不能由 Worker 无限自动重试。

### 5.3 OutlinePolicy

- Outline 最多三级；第二步只创建一个节点和一个确认单元。
- 没有 `CONFIRMED` 的 Outline 不得进入 Unit Generation。
- 已确认 Outline 不得被模型自动增删章节。
- 用户修改 Outline 必须创建新版本，重新等待确认。

### 5.4 UnitPolicy

- 只有当前序号 Unit 可以生成和确认。
- 前一 Unit 未确认，后续 Unit 不得进入 `GENERATING`。
- Unit 生成失败保留原版本和失败 Run，不创建伪造正文。
- 用户确认后写入不可变 `prd_section_version`，作为最终 Markdown 的来源。

## 6. LangGraph 最小图

### 6.1 Graph State

Graph State 只保存当前执行需要的游标和结构化输入，不作为业务状态真相：

```python
class PrdWorkflowState(TypedDict, total=False):
    task_id: str
    run_id: str
    thread_id: str
    graph_version: str
    task_version: int
    current_node: str
    user_message: str
    requirement_brief: dict
    outline_id: str
    outline_version: int
    unit_id: str
    generated_content: str
    interrupt_reason: str
    error_code: str
```

每次节点执行前从 PostgreSQL 读取最新业务快照，并校验 `task_version`；节点完成后由应用服务提交结果和状态迁移，再更新 checkpoint。

### 6.2 节点与条件边

```text
START
  → LOAD_TASK
  → EXTRACT_REQUIREMENT_BRIEF
  → ROUTE_CLARIFICATION
       ├─ NEED_USER_INPUT → WAIT_USER_CLARIFICATION
       └─ SUFFICIENT → GENERATE_OUTLINE
  → WAIT_OUTLINE_CONFIRMATION
  → PREPARE_FIRST_UNIT
  → GENERATE_FIRST_UNIT
  → WAIT_UNIT_CONFIRMATION
  → RENDER_MARKDOWN
  → FINAL_REVIEW
  → END
```

节点职责：

| 节点 | 输入 | 输出 | 业务副作用 |
| --- | --- | --- | --- |
| `LOAD_TASK` | `task_id` | Task 快照 | 无 |
| `EXTRACT_REQUIREMENT_BRIEF` | 首条需求或回复 | Requirement Brief 草稿 | 追加 Brief 版本 |
| `ROUTE_CLARIFICATION` | Brief 草稿 | `NEED_USER_INPUT` 或 `SUFFICIENT` | 更新 Task 状态 |
| `WAIT_USER_CLARIFICATION` | 澄清问题 | interrupt | Run 进入 `WAITING_USER` |
| `GENERATE_OUTLINE` | Brief | Outline 草稿 | 追加 Outline 版本 |
| `WAIT_OUTLINE_CONFIRMATION` | Outline | interrupt | Task 进入 `OUTLINE_REVIEW` |
| `PREPARE_FIRST_UNIT` | 已确认 Outline | Unit 草稿 | 创建 sequence=1 Unit |
| `GENERATE_FIRST_UNIT` | Brief + Outline + Unit | Markdown 章节 | 追加 Unit 版本 |
| `WAIT_UNIT_CONFIRMATION` | 章节 | interrupt | Unit 进入 `PENDING_CONFIRMATION` |
| `RENDER_MARKDOWN` | 已确认内容 | Markdown | 写入文档版本 |
| `FINAL_REVIEW` | Markdown | 检查结果 | Task 进入 `FINAL_REVIEW` |

### 6.3 中断和恢复

- `interrupt()` 只用于等待用户澄清、Outline 确认和 Unit 确认。
- 中断前必须先在业务事务中写入问题、草稿和 `WAITING_USER` 状态。
- 用户回复携带 `task_id`、`expected_task_version` 和幂等键，应用服务校验后才调用 `Command.resume`。
- 恢复时 Worker 根据 PostgreSQL Task 状态和 `thread_id` 找到 checkpoint；不得相信客户端传入的任意 checkpoint。
- checkpoint 恢复失败时标记 Run `FAILED`，保留已持久化的 Brief、Outline 和 Unit，不自动从头生成。

## 7. 数据库表与事务边界

本阶段新增业务表：

```text
prd_tasks
task_messages
requirement_brief_versions
outline_versions
outline_nodes
confirmation_units
prd_section_versions
agent_runs
idempotency_records
domain_events
```

关键约束：

- `prd_tasks(task_id, version)` 支持乐观锁。
- `agent_runs` 对活动状态建立部分唯一索引，保证单 Task 单活动 Run。
- `outline_versions(task_id, version)` 唯一。
- `confirmation_units(outline_id, sequence)` 唯一。
- `prd_section_versions(unit_id, version)` 唯一且只追加。
- `idempotency_records(actor_id, idempotency_key)` 唯一。
- `domain_events(task_id, sequence)` 唯一，按序追加。

事务边界：

1. `StartTask`：Task、首条 Message、Requirement Brief 草稿和 Agent Run 在一个事务中创建。
2. 节点结果：模型输出通过 Schema 校验后，业务状态、版本记录和 Domain Event 在一个事务中提交。
3. 用户确认：确认记录、Task 版本递增、下一 Run 创建在一个事务中提交。
4. Checkpoint：业务事务成功后写入或更新 LangGraph checkpoint；checkpoint 失败必须产生可重试错误，不回滚已确认业务内容。
5. 幂等重放：先读取幂等记录；同键同输入返回历史结果，同键不同输入拒绝。

## 8. 模型调用契约

第二步只需要两个结构化模型能力：

```text
RequirementBriefExtractor
  input: user_message + previous_brief
  output: RequirementBriefDraft

PrdOutlineGenerator
  input: RequirementBrief
  output: OutlineDraft

ConfirmationUnitGenerator
  input: RequirementBrief + ConfirmedOutline + UnitDefinition
  output: UnitContentDraft
```

每个 Adapter 必须返回：

```text
model_id
prompt_version
structured_output
raw_output_hash
token_usage
finish_reason
```

结构化输出失败时最多允许一次基于同一输入的格式修复；修复仍失败则 Run `FAILED`。本阶段不提供工具 Schema，模型不能触发外部函数。

## 9. Markdown 输出

第二步输出稳定模板，不追求完整 PRD：

```markdown
# {title}

## 需求摘要
{requirement_brief.problem}

## 范围边界
### 范围内
{scope_in}
### 范围外
{scope_out}

## 业务规则
{product_rules}

## 第一确认单元：{unit.title}
{unit.content}

## 待确认事项
{open_questions}
```

渲染器只读取已确认版本；草稿、模型原始输出和未确认 Unit 不得出现在最终 Markdown 中。所有字段缺失时使用明确的待确认标记，不写入编造内容。

## 10. CLI 公共接口

建议命令：

```bash
python -m prd_agent workflow start --message "支持订单金额两位小数"
python -m prd_agent workflow show --task-id <task_id>
python -m prd_agent workflow reply --task-id <task_id> --message "仅影响新订单" \
  --expected-task-version 2
python -m prd_agent workflow confirm-outline --task-id <task_id> \
  --outline-version 1 --expected-task-version 3
python -m prd_agent workflow confirm-unit --task-id <task_id> \
  --unit-id <unit_id> --expected-task-version 5
```

CLI 只负责解析参数、展示快照和调用 Application Service，不直接修改数据库，不自行判断状态迁移。

## 11. TDD 实施顺序

按以下垂直切片逐项推进，每项先写一条失败测试，再实现最小行为：

### Slice 1：Task 与状态 Policy

- 测试合法/非法状态迁移、乐观锁版本和删除保护。
- 实现 `Task`、`TaskStatus`、`TaskTransitionPolicy`。
- 验收：Policy 不依赖 LangGraph、数据库和模型。

### Slice 2：StartTask 与 Requirement Brief

- 测试首条非空消息原子创建 Task、Message 和首个 Run。
- 测试空消息拒绝、幂等重放和 Brief Schema 校验。
- 实现 PostgreSQL Repository 和 `StartTask`。

### Slice 3：Clarification Interrupt

- 测试信息不足时进入 `CLARIFYING` 和 `WAITING_USER`。
- 测试客户端不能跳过澄清直接确认 Outline。
- 实现 `EXTRACT_REQUIREMENT_BRIEF`、`ROUTE_CLARIFICATION` 和 interrupt。

### Slice 4：Outline 生成与确认

- 测试足够信息时生成一份 Outline 草稿。
- 测试 Outline 未确认不能生成 Unit。
- 测试确认后版本锁定，修改创建新版本。
- 实现 `GENERATE_OUTLINE` 和 `ConfirmOutline`。

### Slice 5：第一个 Confirmation Unit

- 测试只创建 sequence=1 Unit。
- 测试 Unit 生成、失败保留和用户确认。
- 实现 `PREPARE_FIRST_UNIT`、`GENERATE_FIRST_UNIT` 和 `ConfirmUnit`。

### Slice 6：Markdown 与最终状态

- 测试只渲染已确认 Brief、Outline 和 Unit。
- 测试完成后 Task 进入 `FINAL_REVIEW`，不把未确认草稿写入文档。
- 实现 `RENDER_MARKDOWN`、基础 `FINAL_REVIEW` 和 CLI `show`。

### Slice 7：恢复与 Step 1 Eval 接入

- 测试进程重启后从 PostgreSQL 业务状态和 LangGraph checkpoint 继续。
- 测试相同 Run 幂等恢复，不重复生成已确认 Unit。
- 将 Minimal Workflow 注册为新的 Eval 配置，保留 Direct Prompt 结果。

## 12. 验收用例

### Case A：信息足够的新增需求

输入“订单列表增加创建时间筛选”，预期一次生成 Brief 和 Outline，确认 Outline 后生成一个 Unit，确认 Unit 后输出 Markdown。

### Case B：信息不足

输入“优化订单体验”，预期只提出 1～3 个澄清问题，Task 进入 `CLARIFYING`，不生成 Outline。

### Case C：重复提交

同一 `Idempotency-Key` 重复 StartTask，返回同一 `task_id` 和 `run_id`，数据库不新增 Task。

### Case D：版本冲突

使用旧 `expected_task_version` 确认 Outline，预期返回 `409` 等价领域错误，不改变 Outline 或 Task。

### Case E：刷新或进程重启

在等待 Outline 确认时终止进程，再恢复执行，预期 Task 仍为 `OUTLINE_REVIEW`，不重复生成 Outline。

### Case F：模型结构化输出失败

模型返回非法 Brief 或 Outline，预期最多一次格式修复；仍失败则 Run `FAILED`，保留输入和失败原因。

## 13. 完成定义

- 不依赖外部工具即可完成最小 PRD 垂直切片。
- Task、Run、Brief、Outline、Unit 和 Section 全部有结构化 Schema。
- 所有状态迁移由 Policy 控制，模型不能跳过确认节点。
- Outline 和 Unit 使用追加版本，已确认内容不可原地覆盖。
- 同一 Task 最多一个活动 Run，重复命令按幂等键返回原结果。
- PostgreSQL 是业务状态唯一事实来源；LangGraph checkpoint 仅负责图恢复。
- CLI 可以展示 Task 快照、澄清问题、Outline、Unit 和最终 Markdown。
- 所有 Slice 单测通过，至少完成 6 个端到端 Workflow 测试。
- Minimal Workflow 可以接入第一步 Eval Runner，并与 Direct Prompt Baseline 生成对比报告。

## 14. 第二步之后的衔接

第二步完成后进入总领方案阶段 2：Repository Tools 与 Evidence。届时不修改 Task、Run、Outline 和 Confirmation Unit 的业务语义，只在 `UNIT_PREPARATION` 和 `GENERATE_FIRST_UNIT` 之间插入 Information Need、Investigation 和 Grounding 节点。

后续 Investigation 失败、Evidence 不足或 Source Conflict 只改变当前 Unit 的调查结果和待确认项，不允许绕过已有的确认 Policy。

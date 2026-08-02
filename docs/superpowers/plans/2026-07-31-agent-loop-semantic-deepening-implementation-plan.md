# Agent Loop 语义深化实施计划

> 状态：PROPOSED / READY FOR IMPLEMENTATION
> 日期：2026-07-31
> 对应设计：[`2026-07-31-agent-loop-semantic-deepening-design.md`](../specs/2026-07-31-agent-loop-semantic-deepening-design.md)
> 目标版本：`agent-runtime.v4` / `agent-loop-snapshot.v3`
> 实施原则：Tracer Bullet、先兼容后切换、每个提交可回滚、每个阶段独立 Gate

---

## 0. 计划摘要

按以下顺序实施，禁止跳过前置 Gate：

| 顺序 | Phase | 目标 | 依赖 |
| ---: | --- | --- | --- |
| 0 | Characterization | 固定当前行为和 Eval 基线 | 无 |
| 1 | Workflow Version + Resume | 让恢复和灰度可被信任 | Phase 0 |
| 2 | Run Execution Ledger | 统一预算、幂等和副作用顺序 | Phase 1 |
| 3 | Information Need | 正确决定是否调查、查什么 | Phase 2 |
| 4 | Evidence Knowledge + Grounding | 相关来源不再自动变成事实 | Phase 3 |
| 5 | Unified Investigation | 真正 Replan，Supplement 复用主 Loop | Phase 4 |
| 6 | Reviewable Unit + Quality | 按当前 Unit 生成和修订 | Phase 5 |
| 7 | Eval / Shadow / Enforce | 数据化灰度并删除旧 producer | Phase 6 |
| 8 | Orchestration Completion | 闭合 v4 Review Workflow、发布与 rollout readiness | Phase 6/7 本地基础 |
| 9 | Operational Closure | PostgreSQL 实证、生产语义链、Review 投影和 durable rollout control | Phase 8 |
| 10 | Environment Graduation | remote Eval、staging、rollback rehearsal 和 production canary/default | Phase 9 |
| 11 | Legacy Retirement | v1 drain、producer removal、reader observation/removal | Phase 10 |

Phase 0～2 是可靠性地基；Phase 3～5 是 Agent 质量核心；Phase 6 建立 Reviewable Unit
计算语义；Phase 7 建立本地 rollout 基础；Phase 8 闭合 authoritative PRD 工作流；Phase 9
补真实 PostgreSQL、生产 semantic Module、Review projection 与 rollout authority；Phase 10 执行
可回滚的环境晋级；Phase 11 才删除 legacy。任何 Phase 未过 Gate，不得通过扩大预算、Prompt
或跳过观察窗口绕过。

---

## 1. 通用实施规则

1. 每个功能先添加 characterization test，再修改生产路径。
2. Go/Python 合同遵循 expand → 双读/双写 → switch → contract。
3. 已有 `agent-runtime.v1` Run 必须可继续恢复；不得原地升级其 snapshot。
4. 新模型输出全部使用严格 Schema；最多一次 same-input 格式修复。
5. Policy 测试不 mock Graph；Graph 测试不依赖具体内部函数调用次数。
6. 远程模型、Capability 和 ACK 等待期间不得持有 PostgreSQL 事务。
7. Python 不连接 PostgreSQL；所有 durable 结果通过现有事件流提交。
8. 不编辑或删除用户当前未提交的生产 Ingress 变更。
9. 每个提交只覆盖一个可描述的不变量，避免“重写整个 runtime.py”。
10. 每个 Phase 更新设计文档的实现状态和验证记录。

---

## 2. Phase 0：Characterization 与 Eval 基线

详细文档：

- [Phase 0 优化实现方案](./2026-07-31-agent-loop-phase-0-characterization-eval-design.md)
- [Phase 0 单元测试方案](./2026-07-31-agent-loop-phase-0-characterization-eval-unit-test-plan.md)

### 2.1 目标

在改变语义前固定当前 v1 行为、错误行为和成本基线，使后续优化可证明而非凭感觉。

### 2.2 代码修改

新增：

```text
agent-python/tests/characterization/
  test_current_need_behavior.py
  test_current_coverage_behavior.py
  test_current_grounding_behavior.py
  test_current_resume_behavior.py
  test_current_shadow_behavior.py

eval/configs/langgraph_v1.json
eval/configs/langgraph_v4_shadow.json
eval/reports/.gitkeep
```

扩展 Eval 输出：

- Information Need requiredness；
- Coverage 前后状态；
- Evidence、Fact、Unknown、Conflict 数量；
- GroundingFinding；
- Model Attempt、Tool Call、Token、Latency；
- stop reason；
- 物理调用与 durable replay 次数。

Characterization 必须明确记录当前已知问题：

- 非空 Evidence 会完成 Coverage；
- ref 存在可使 Claim Supported；
- Replan 只增加计数；
- shadow 可能执行额外工作；
- snapshot 未覆盖全部不变量。

这些测试用于固定迁移起点；对应语义修复后应被新断言替换，不永久保护错误行为。

### 2.3 验证

```bash
venv/bin/python -m pytest -q agent-python/tests
PYTHONPATH=src venv/bin/python -m prd_agent.eval validate-dataset \
  --manifest eval/cases/manifest.json
```

运行并保存脱敏基线报告；如果暂时无法调用真实模型，报告必须标记
`deterministic_only=true`，不得宣称产品质量收益。

### 2.4 最小提交

1. `test: characterize current agent loop semantics`
2. `feat: report production-loop eval trace metrics`
3. `docs: record agent-runtime v1 baseline`

### 2.5 Gate

- 当前 50 个 Agent Python 测试继续通过；
- 固定 case 可以输出 v1 trace；
- 报告能区分 Evidence 命中和 Grounding 支持；
- 没有提交生产正文、Prompt 或 Evidence excerpt。

---

## 3. Phase 1：Workflow Version 与 Resume Validation

详细文档：

- [Phase 1 Workflow Version 与 Resume Validation 设计方案](./2026-07-31-agent-loop-phase-1-workflow-version-resume-design.md)

### 3.1 目标

在任何新语义进入生产前，先让 Go 能持久化和路由 workflow version，并让 Python
恢复时完整校验 snapshot、Repository Snapshot、Artifact 和 Draft receipt。

### 3.2 Go 修改

主要文件：

```text
backend-go/db/migrations/0012_agent_workflow_version.sql
backend-go/internal/runcontrol/model.go
backend-go/internal/runcontrol/store.go
backend-go/internal/runcontrol/memory_store.go
backend-go/internal/storage/agent_execution.go
backend-go/internal/dispatcher/dispatcher.go
contracts/proto/agent/v1/agent_execution.proto
```

实施：

1. 给 `go_agent_runs` 增加不可变 `workflow_version`，已有行回填 `agent-runtime.v1`；
2. Run Admission/创建命令选择版本，普通用户请求不能自由指定；
3. `GetRunContext` 返回持久化版本，不再硬编码 v1；
4. Dispatcher 根据版本选择兼容 Worker；未知版本不派发；
5. 扩展 `AgentRunInput` 的 snapshot identity 摘要，保持 protobuf 向后兼容。

### 3.3 Python 修改

新增：

```text
agent-python/agent/resume/models.py
agent-python/agent/resume/validator.py
agent-python/agent/resume/artifacts.py
```

调整：

```text
agent-python/agent/graph/snapshot.py
agent-python/agent/graph/state.py
agent-python/agent/graph/runtime.py
agent-python/agent/context.py
agent-python/agent/checkpoint.py
```

`LangGraphAgentLoop.__call__` 只接收 `ValidatedRunState`，不再自行散落处理 hydration。

校验：

- Run/Task/workflow identity；
- Repository binding/revision；
- checkpoint sequence；
- 单调计数和 append-only ID 集合；
- artifact key/hash/run identity；
- immutable/reopened Unit 集合；
- `READY_TO_SUBMIT + resume_draft` 的 key/hash/task version；
- 未知 status/schema fail closed。

### 3.4 测试

新增：

```text
agent-python/tests/test_resume_validator.py
backend-go/internal/runcontrol/workflow_version_test.go
backend-go/internal/storage/workflow_version_integration_test.go
backend-go/internal/dispatcher/workflow_routing_test.go
```

Crash matrix 至少覆盖每个已有 checkpoint status，以及 Draft ACK 后 Run terminal ACK 前重启。

### 3.5 最小提交

1. `feat: persist agent workflow version on runs`
2. `feat: route dispatches by workflow version`
3. `feat: add agent-loop snapshot v3 models`
4. `feat: validate and hydrate resumable agent state`
5. `test: cover snapshot drift and submitted-draft recovery`

### 3.6 Gate

- v1 Run 仍由兼容路径恢复；
- v4 Run 不能被 v1 Worker 接收；
- revision 漂移、计数回退、集合缩小、Artifact 缺失全部产生
  `CHECKPOINT_INCOMPATIBLE`；
- Draft 已 ACK 时零模型、零 Capability 调用完成 terminal event。

---

## 4. Phase 2：Run Execution Ledger

详细文档：

- [Phase 2 Run Execution Ledger 设计与实施 Plan](./2026-08-01-agent-loop-phase-2-run-execution-ledger-design.md)

### 4.1 目标

所有模型、Capability、Artifact 和 Checkpoint 副作用进入一个深 Module，消除节点内
硬编码预算和不一致的提交顺序。

### 4.2 Python 修改

新增：

```text
agent-python/agent/runtime/ledger.py
agent-python/agent/runtime/budget.py
agent-python/agent/runtime/idempotency.py
agent-python/agent/runtime/outcomes.py
```

Ledger 提供：

- `reserve_model(operation, request_hash, estimate)`；
- `finish_model(receipt, validated_artifact, usage)`；
- `reserve_capability(action_signature)`；
- `finish_capability(receipt, evidence, usage)`；
- `remaining()`；
- `stop_reason()`。

重构以下调用方：

```text
agent-python/agent/graph/runtime.py
agent-python/agent/graph/advanced.py
agent-python/agent/worker_server.py
```

删除：

- Supplement 的硬编码工具阈值；
- 各节点自行拼接 attempt key；
- 调用完成后才首次判断总预算；
- shadow 下额外模型或 Capability 调用。

### 4.3 Go/合同修改

Model Attempt receipt 增加足以识别 durable replay 的状态和 output artifact ref；
Go Store 对相同 `(run_id, attempt_key, request_hash)` 返回已有 receipt，对相同 key 不同
hash 返回冲突。物理模型结果仍不长期保存完整原始响应；保存经过 Schema 校验的恢复 Artifact。

### 4.4 测试

- 每类预算在物理调用前停止；
- Initial、Supplement、Grounding、Repair 共享计数；
- Artifact ACK 早于引用它的 SUCCEEDED/checkpoint；
- crash after model response before checkpoint 可复用 validated Artifact；
- 相同 attempt key 不同 request hash 被拒绝；
- shadow 的额外远程调用数为 0。

### 4.5 最小提交

1. `feat: add shared agent run budget ledger`
2. `feat: centralize model attempt execution and replay`
3. `feat: centralize capability reservation and accounting`
4. `fix: make advanced shadow mode side-effect free`
5. `test: cover ledger crash and budget boundaries`

### 4.6 Gate

- 任一 Run 不超过配置的后续调用上限；
- 已 ACK 物理调用在恢复后不重复；
- 所有 Loop 的用量可加总为同一 ConsumedBudget；
- 旧节点不再直接读取环境变量或硬编码预算。

---

## 5. Phase 3：Information Need Planning

> 详细设计与实施顺序：[`2026-08-01-agent-loop-phase-3-information-need-planning-design.md`](2026-08-01-agent-loop-phase-3-information-need-planning-design.md)

### 5.1 目标

在 Graph 选择 Action 或生成正文前，先产生并持久化一个可校验的 Information Need Plan。

### 5.2 Python 修改

新增：

```text
agent-python/agent/information_need/models.py
agent-python/agent/information_need/planner.py
agent-python/agent/information_need/policies.py
agent-python/agent/information_need/prompts.py
agent-python/agent/information_need/artifact.py
```

迁移可复用的纯规则来源：

- `src/prd_agent/investigation/planner.py`；
- `src/prd_agent/application/default_investigation.py`；
- PRD 的 `NONE / OPTIONAL / REQUIRED` 和触发规则。

禁止迁移旧 Python API、数据库 Store 或应用状态机依赖。

Graph 新增 `PLAN_INFORMATION_NEED` / `NEED_PLANNED`：

- `NONE` 直接进入 Unit/Draft 生成；
- `OPTIONAL` 由确定性 value/budget policy 决定执行或跳过并记录原因；
- `REQUIRED` 必须进入 Investigation，不能通过模型直接返回 Markdown 结束；
- Plan 保存为 `INFORMATION_NEED_PLAN` Artifact 后才 checkpoint。

### 5.3 测试

固定案例：

- 纯新增需求 → `NONE`，0 Tool Call；
- 修改 API 字段 → `REQUIRED/CODE`；
- 历史术语补充 → `OPTIONAL/HISTORICAL_PRD`；
- 权限不足的 REQUIRED → `HUMAN_INPUT_REQUIRED` 或明确 Unknown；
- Planner 试图放宽 Requiredness → Policy 拒绝；
- 恢复命中 Plan Artifact → 0 Planner Model Attempt。

### 5.4 最小提交

1. `feat: define versioned information need plans`
2. `feat: add deterministic requiredness policy`
3. `feat: plan and checkpoint information needs`
4. `feat: route none optional and required needs`
5. `eval: measure information need precision and recall`

### 5.5 Gate

- 所有 Run 都有明确 Need Plan 或兼容 legacy 标记；
- `NONE` Case 不调用 Capability；
- `REQUIRED` Case 不会在调查前生成确定性 CURRENT_STATE；
- Eval 报告包含 requiredness 混淆矩阵。

---

## 6. Phase 4：Evidence Knowledge 与 Claim Grounding

详细设计与按序实施方案：
[`2026-08-01-agent-loop-phase-4-evidence-knowledge-grounding-design.md`](2026-08-01-agent-loop-phase-4-evidence-knowledge-grounding-design.md)

### 6.1 目标

用 Verified Fact、Unknown 和 Source Conflict 替代“Evidence 非空即完成、ref 存在即支持”。

### 6.2 Python 修改

新增或深化：

```text
agent-python/agent/knowledge/models.py
agent-python/agent/knowledge/normalizer.py
agent-python/agent/knowledge/fact_extractor.py
agent-python/agent/knowledge/conflict_detector.py
agent-python/agent/knowledge/coverage.py
agent-python/agent/grounding/policies.py
agent-python/agent/grounding/classifier.py
```

优先移植以下纯逻辑，不携带旧持久化依赖：

```text
src/prd_agent/evidence/
src/prd_agent/grounding/service.py
src/prd_agent/investigation/policies.py
```

实现顺序：

1. Evidence identity/revision/locator/hash 校验；
2. 确定性 Parser Fact；
3. Unknown 和 Conflict；
4. Fact verification status；
5. Claim-to-Fact type/scope 校验；
6. 有界语义支持分类；
7. 基于 verdict 的 Coverage update；
8. `KNOWLEDGE_BUNDLE` 和 `GROUNDING_REPORT` Artifact。

删除 `AdvancedLoopRunner.supplement()` 中把全部新 Evidence refs 自动附到所有
unsupported Claim 的行为。

### 6.3 测试

- 相关但不支持 Claim 的 Evidence → `UNSUPPORTED`；
- commit/revision 错误 → `STALE_SOURCE`；
- `EMPTY` → Unknown，不生成“不存在” Fact；
- 两个来源矛盾 → Source Conflict / `CONFLICTING`；
- INFERRED 不可升级为 CODE_VERIFIED；
- CURRENT_STATE 必须由 CURRENT_STATE Fact 支持；
- 只有 Knowledge fingerprint 变化才重置 no-progress；
- Knowledge Artifact 重放 hash 稳定。

### 6.4 最小提交

1. `feat: define knowledge bundle and verified fact models`
2. `feat: validate evidence identity and source revisions`
3. `feat: extract facts unknowns and source conflicts`
4. `feat: ground claims through verified facts`
5. `fix: update coverage from knowledge verdicts`
6. `eval: measure fact accuracy and unsupported claims`

### 6.5 Gate

- 非空 Retrieval Hit 不再自动完成 Coverage；
- 所有 blocking CURRENT_STATE Claim 都有 GroundingFinding；
- Unsupported/Conflicting Claim 被降级或进入 Supplement，不静默通过；
- v4 的 Unsupported Claim Rate 不高于 v1，Critical Unknown Recall 不下降。

---

## 7. Phase 5：统一 Investigation、Replan 与 Supplement

详细设计与按序实施方案：
[`2026-08-01-agent-loop-phase-5-unified-investigation-design.md`](2026-08-01-agent-loop-phase-5-unified-investigation-design.md)

### 7.1 目标

删除初始调查和高级 Supplement 的重复 Implementation，让三种模式通过同一
Investigation Interface。

### 7.2 Python 修改

目标结构：

```text
agent-python/agent/investigation/
  models.py
  policies.py
  prompts.py
  nodes.py
  graph.py
  runner.py
```

`InvestigationRunner` 接收 `mode=INITIAL|REPLAN|SUPPLEMENT`、Need Plan、共享 Ledger
和已有 KnowledgeBundle，返回 `InvestigationResult`。

重构：

- 从 `graph/runtime.py` 移出调查节点闭包；
- `graph/advanced.py` 不再直接调用 `search_repository/search_prd_catalog`；
- Supplement 创建 scoped Need Plan，再调用 Investigation；
- Replan Prompt 接收 prior actions、公开结果、无进展原因和剩余预算；
- Replan 必须产生新 Action Signature 或停止；
- `PERMISSION_DENIED`、`HUMAN_INPUT_REQUIRED`、`USER_STOPPED` 进入统一 Stop Reason。

### 7.3 测试

- 初始两轮逐步完成不同 Coverage；
- 空结果 → 真正 Replan → 新 Action；
- 重复 Action 不产生第二次物理调用；
- Supplement 使用相同 Action Policy 和 checkpoint；
- Supplement 无新 Fact → NO_PROGRESS/Unknown；
- Initial + Supplement 共享 Tool/Token budget；
- Permission denied 不重规划到未授权来源；
- `OBSERVED` 恢复不重复 Capability。

### 7.4 最小提交

1. `refactor: extract controlled investigation module`
2. `feat: implement strategy-changing replans`
3. `refactor: run targeted supplements through investigation`
4. `feat: unify investigation stop reasons`
5. `test: cover initial replan and supplement recovery`

### 7.5 Gate

- 删除高级路径的直接 Capability 调用；
- `replan_count > 0` 时 trace 必须出现策略或 Action Signature 变化；
- Initial/Supplement 使用相同预算、去重、Knowledge 和恢复规则；
- Graph 顶层只路由，不解释调查内部状态。

---

## 8. Phase 6：Reviewable Unit、Quality 与全文检查

详细设计与实施 Plan：

- [Phase 6 Reviewable Unit、Quality 与全文检查](./2026-08-01-agent-loop-phase-6-reviewable-unit-quality-design.md)

### 8.1 目标

将“先生成全文再拆 Unit”迁移为按 Run Purpose 和当前 Confirmation Unit 执行。

### 8.2 Go/合同修改

扩展：

```text
contracts/proto/agent/v1/agent_execution.proto
backend-go/internal/runcontrol/model.go
backend-go/internal/storage/confirmation.go
backend-go/internal/storage/agent_execution.go
backend-go/internal/httpapi/router.go
```

新增 `RunPurpose` 和 `UnitScope`：

- `PLAN_OUTLINE`；
- `GENERATE_UNIT`；
- `REVISE_UNIT`；
- `FULL_REVIEW`。

Go 负责锁定 Outline/Unit 顺序、决定当前 dependency-ready Unit、汇总已确认内容并
创建下一 Agent Run。Python 不在一个 Worker 中等待用户。

### 8.3 Python 修改

新增或深化：

```text
agent-python/agent/unit/models.py
agent-python/agent/unit/generator.py
agent-python/agent/unit/repair.py
agent-python/agent/unit/invariants.py
agent-python/agent/quality/policies.py
agent-python/agent/quality/classifier.py
agent-python/agent/quality/full_review.py
```

规则：

- 每个生成 Run 只输出当前 Unit；
- 标题/节点必须来自锁定 Outline；
- 已确认摘要只读；
- Repair 只返回 Unit Patch；
- 未 reopen 的 Unit hash 不变；
- Repair 后重新 Grounding；
- Full Review 只输出问题和 affected unit keys，不直接修改已确认内容。

Quality code 至少覆盖：

- missing required section；
- broken traceability；
- inconsistent terminology；
- rule/state/permission conflict；
- ambiguous acceptance criterion；
- unresolved source conflict；
- unmarked unknown；
- unsupported current state；
- sensitive content / draft too large。

### 8.4 迁移策略

1. v4 先支持 `PLAN_OUTLINE` 和单 Unit candidate，但保留 v1 whole-draft reader；
2. Go UI/API 可以同时展示 legacy multi-unit Draft 和 v4 current Unit；
3. 新 Task 默认按 Unit Run 后，停止产生 legacy whole-draft；
4. 全部 legacy active Run drain 后再删除 producer；
5. Published PRD 仍由 Go 汇总当前已确认 Unit 并发布到 Feishu。

### 8.5 测试

- Outline 锁定后模型不能增删章节；
- 只有 dependency-ready Unit 可生成；
- 当前 Unit 未确认前不创建下一生成 Run；
- reopen 单 Unit 不修改 immutable Unit；
- Repair 删除 Unknown/改变 Evidence/越界 Unit 被拒绝；
- 验收标准缺少条件与预期结果时 Quality 不通过；
- Full Review 不直接写入已确认 Unit；
- 所有当前 Unit Version 确认后才可 Publish Preview。

### 8.6 最小提交

1. `feat: add versioned agent run purpose and unit scope`
2. `feat: plan and lock reviewable units`
3. `feat: generate one confirmation unit per run`
4. `feat: enforce unit patch invariants`
5. `feat: deepen unit and full-document quality checks`
6. `feat: sequence dependency-ready unit runs in go`
7. `test: cover outline unit revision and publish gates`

### 8.7 Gate

- 一个 v4 生成 Run 只提交一个 active Unit candidate；
- 未确认 Unit 会阻止下一 Unit 和发布；
- confirmed immutable Unit 的 content hash 在 reopen Run 后保持不变；
- Quality Gate 覆盖 PRD 7.13 定义的检查类别。

---

## 9. Phase 7：Eval、Shadow、Enforce 与清理

详细设计与实施 Plan：

- [Phase 7 Eval、Shadow、Enforce 与 Legacy Removal](./2026-08-01-agent-loop-phase-7-rollout-legacy-removal-design.md)

### 9.1 目标

用固定数据和故障演练决定是否默认启用 v4，而不是仅凭测试通过切换。

### 9.2 Eval Matrix

| Variant | Need | Knowledge | Grounding | Unit | 用途 |
| --- | --- | --- | --- | --- | --- |
| v1 current | 固定 | Evidence only | ref presence | whole draft | Baseline |
| v4-a | 开 | Evidence only | legacy | whole draft | Need ablation |
| v4-b | 开 | 开 | 开 | whole draft | Grounding ablation |
| v4-c | 开 | 开 | 开 | current Unit | Candidate |

每个固定 case 至少运行 3 次；报告均值、方差、P50/P95 和失败轨迹。

### 9.3 Enforce Gate

必须同时满足：

- Information Need Precision/Recall 达到项目设定阈值；
- Verified Fact Accuracy 和 Evidence Precision 不低于 v1；
- Unsupported CURRENT_STATE Claim 显著下降或保持为 0；
- Critical Unknown Recall 不下降；
- Source Conflict Detection 提升；
- Tool/Token 不超过硬预算；
- P95 在可接受范围；
- crash matrix 中重复物理模型/Capability 调用为 0；
- 24 小时单 Worker 运行无无界内存增长；
- v4 drain 和回滚演练完成。

### 9.4 切换顺序

1. 本地 deterministic v4；
2. staging v4 shadow（零额外远程调用）；
3. staging 指定用户/Task enforce；
4. production canary；
5. production 默认 v4；
6. drain v1 active Run；
7. 删除 legacy producer；
8. 保留 legacy reader 一个发布周期；
9. 删除 reader 和旧配置。

### 9.5 最小提交

1. `eval: add agent-runtime v4 ablation matrix`
2. `feat: expose low-cardinality loop metrics`
3. `ops: add v4 canary and drain controls`
4. `docs: record v4 staging and recovery evidence`
5. `refactor: remove legacy agent loop producer`
6. `refactor: remove expired snapshot compatibility`

---

## 10. Phase 8：Review Workflow 编排闭环与 Operational Graduation

详细设计与实施 Plan：

- [Phase 8 Review Workflow 编排闭环与 Operational Graduation](./2026-08-01-agent-loop-phase-8-orchestration-completion-design.md)

### 10.1 目标

把 Phase 6、7 已有但分散的合同、校验与 rollout 原语闭合为 authoritative v4 主链：

```text
Outline output -> locked Outline -> one dependency-ready Unit Run
  -> confirmed Unit set -> Full Review -> immutable publish document
```

本阶段以 Go `ReviewWorkflowModule` 为领域事务边界，深化 Python
`ReviewableUnitExecutionModule`，通过 `PublishDocumentCompatibilityModule` 隔离 v1/v4
发布读取，再补齐 durable GateDecision、Drain 与 legacy inventory。不得在本阶段执行真实
production canary 或删除 legacy producer/reader。

### 10.2 实施顺序

1. 固定 v1 compatibility fixture、v4 transition table 和 crash matrix；
2. 添加 0016/0017 migration，不修改 0014/0015；
3. 实现 Go Review Workflow 的 Memory/PostgreSQL Adapter；
4. 深化 Python Unit execution、bounded repair、re-Grounding 与 re-Quality；
5. 实现 v1 Working Draft / v4 locked Outline 发布 Adapter；
6. 持久化 GateDecision、Drain 和 legacy inventory；
7. 接通零远程副作用 Shadow，并生成 hash-bound readiness record；
8. 运行本地、合同、并发、crash-replay 和 PostgreSQL Gate。

### 10.3 Gate

- v4 Task 可从 Outline RunOutput 闭环到 Publish Preview；
- 并发确认和 crash replay 不产生重复 Unit Version、Run 或 Outbox；
- Repair 后必须重新 Grounding 和 Quality；
- Full Review hash set 漂移时发布 fail closed；
- v1 publish fixture 保持不变；
- rollout 缺少 GateDecision 或 inventory 非零时 fail closed；
- 只允许将状态更新为 `LOCALLY VERIFIED`；Phase 9 完成 operational closure 后，Phase 10 才执行
  staging/canary，Phase 11 才执行 drain/retirement。

---

## 11. Phase 9：Operational Closure

详细设计与实施 Plan：

- [Agent Loop 剩余实现设计：Operational Closure、Rollout 与 Legacy Retirement](./2026-08-01-agent-loop-remaining-implementation-design.md)

### 11.1 目标

把 Phase 8 的本地原语升级为可进入 staging 的完整实现：

- 真实 PostgreSQL migration、v4 E2E、并发、replay 和 rollback 证据；
- Memory/PostgreSQL Adapter 共用 Review Workflow contract；
- Python production Unit path 真实执行 Need、Investigation、Knowledge、Grounding、Quality 和
  Repair；
- Go Review Workflow Interface 收拢 materialization、decision、Full Review 和 publish；
- v1/v4 统一 Review projection 与 Web 工作台；
- durable rollout policy 真正驱动新 Agent Run assignment；
- authoritative trace 产生零远程副作用 Shadow Artifact；
- terminal publish payload bounded retention。

### 11.2 Gate

- PostgreSQL Gate 零 skip；
- v4 Task 从 creation 到 Publish Preview 全链通过；
- production v4 Repair 后 re-Ground/re-Quality；
- Web 可完成 Outline/Unit/Full Review/Publish workflow；
- rollout stage、policy 和 assignment 一致；
- Shadow extra model/capability/write 为 0；
- 只允许更新为 `STAGING READY`。

---

## 12. Phase 10：Environment Graduation

### 12.1 目标

执行 remote Eval、staging Shadow、staging selected Task Enforce、rollback/drain rehearsal、
production canary 和 production default v4。所有动作由 Phase 9 的 durable command 与
GateDecision 驱动。

### 12.2 Gate

- remote Eval matrix 每 case 至少 3 次且 hard Gate 通过；
- staging Shadow 观察窗口额外远程副作用为 0；
- staging v4 完成真实 Outline -> Publish/reconcile；
- rollback 只影响新 Run，已有 Run 保持 immutable assignment；
- production canary 每档满足最小时长、最小样本和全部 Gate；
- default v4 稳定观察完成，但不在本阶段删除 legacy。

---

## 13. Phase 11：Legacy Retirement

### 13.1 目标

在 production default v4 稳定后，依次完成 v1 execution drain、producer removal、一个发布周期
reader observation，以及 reader/config/expired snapshot compatibility removal。

### 13.2 Gate

- v1 active Run/dispatch/outbox/ledger/snapshot 全零；
- producer removal 绑定 zero inventory 和 rollback window；
- reader 按当前 release/window 统计且一个发布周期 hit 为 0；
- `LEGACY_RETIRED` 绑定 completed DrainRecord 和 removal inventory hash；
- protobuf 旧 field/enum reserved，不复用编号；
- Feishu 继续是 Published PRD canonical owner。

---

## 14. 全量验证命令

不需要数据库的快速 Gate：

```bash
venv/bin/python -m pytest -q agent-python/tests
venv/bin/python -m pytest -q tests
cd backend-go && go test ./...
npm --prefix web run test
npm --prefix web run typecheck
```

合同生成与一致性：

```bash
buf lint contracts
buf generate contracts
git diff --exit-code -- contracts/gen agent-python/agent/v1 web/lib/api/schema.d.ts
```

PostgreSQL Gate：

```bash
PRD_AGENT_TEST_DATABASE_DSN="$PRD_AGENT_DATABASE_DSN" \
  venv/bin/python -m pytest -q
cd backend-go && PRD_AGENT_TEST_DATABASE_DSN="$PRD_AGENT_DATABASE_DSN" go test ./...
```

Eval：

```bash
PYTHONPATH=src venv/bin/python -m prd_agent.eval run-baseline \
  --manifest eval/cases/manifest.json \
  --config eval/configs/langgraph_v1.json \
  --output-dir eval/reports

PYTHONPATH=src venv/bin/python -m prd_agent.eval run-baseline \
  --manifest eval/cases/manifest.json \
  --config eval/configs/langgraph_v4_shadow.json \
  --output-dir eval/reports
```

真实 staging 命令、Secret 和报告正文不得写入仓库；只提交脱敏指标、hash 和失败分类。

---

## 15. 风险与回滚

| 风险 | 控制 | 回滚 |
| --- | --- | --- |
| 新 snapshot 旧 Worker 不识别 | workflow version 路由 | drain v4 后切回 v1 |
| Knowledge 分类增加成本 | 确定性 Parser 优先、共享 Ledger | 关闭语义分类但保留来源校验 |
| Need Planner 误判 NONE | RequirednessPolicy 可收紧 | 指定 case 回退 REQUIRED |
| Unit 化扩大 Go 状态机 | RunPurpose 分阶段启用 | 保留 whole-draft reader |
| Artifact 增长 | 大小上限、TTL、类型白名单 | 关闭新 Run，清理孤立 Artifact |
| Shadow 隐藏成本 | shadow 禁止额外远程调用 | 直接切 off |
| Eval 改善但时延恶化 | 同时报质量、Token、P95 | 保持 canary，不默认切换 |

---

## 16. 完成清单

- [x] Phase 0：v1 characterization 与 Eval trace 可重复运行
- [x] Phase 1：workflow version 持久化，Snapshot v3 本地恢复 Gate 通过
- [x] Phase 2：远程副作用经过共享 Ledger，预算/恢复 Gate 与 shadow 零额外远程调用已通过本地验证
- [x] Phase 3：Need Plan 支持 NONE/OPTIONAL/REQUIRED，已完成本地实现与回归验证
- [x] Phase 4：Knowledge 和 Grounding 不再把 ref presence 当作支持（本地验证完成）
- [x] Phase 5：Initial/Replan/Supplement 复用同一 Investigation（本地验证完成）
- [ ] Phase 6：一个 Run 只生成或修订当前 Confirmation Unit
- [ ] Phase 7：v4 Eval、canary、drain、回滚证据完整
- [ ] Phase 8：Review Workflow 本地闭环完成，真实 PostgreSQL/production semantic Gate 待 Phase 9
- [ ] Phase 9：PostgreSQL、production semantic、Review projection 与 rollout authority 闭环
- [ ] Phase 10：remote Eval、staging、rollback rehearsal、production canary/default 完成
- [ ] Phase 11：v1 drain、producer/reader 退役完成
- [ ] legacy producer 删除且兼容 reader 按计划退役
- [ ] 设计文档的完成定义全部满足

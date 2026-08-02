# Agent Loop Phase 6：Reviewable Unit、Quality 与全文检查设计及实施 Plan

> 状态：IMPLEMENTATION IN PROGRESS / CORE LOCALLY VERIFIED
> 日期：2026-08-01
> 前置阶段：Phase 5 Unified Investigation Gate 已本地通过
> 后续阶段：Phase 7 Eval / Shadow / Enforce / Legacy Removal
> 对应 Phase 5：[`2026-08-01-agent-loop-phase-5-unified-investigation-design.md`](2026-08-01-agent-loop-phase-5-unified-investigation-design.md)
> 对应总设计：[`2026-07-31-agent-loop-semantic-deepening-design.md`](../specs/2026-07-31-agent-loop-semantic-deepening-design.md)
> 对应总计划：[`2026-07-31-agent-loop-semantic-deepening-implementation-plan.md`](2026-07-31-agent-loop-semantic-deepening-implementation-plan.md)
> 目标版本：仅 `agent-runtime.v4` 生产新语义；`agent-runtime.v1` 保持兼容读取

---

## 0. 执行结论

Phase 6 将“Python 先生成整份 Working Draft，再拆成多个 Confirmation Unit”迁移为：

```text
PLAN_OUTLINE
  -> 用户确认并锁定 Outline
  -> GENERATE_UNIT(current dependency-ready Unit)
  -> 用户确认当前 Unit
  -> ...
  -> FULL_REVIEW
  -> 用户最终确认 / 选择受影响 Unit 重新修订
  -> Go 汇总已确认 Unit 并创建 Publish Preview
```

实现采用两个较深的 Module：

1. Go `UnitRunOrchestrationModule`：拥有 Outline 锁定、Unit 顺序、依赖就绪判定、
   Run Purpose、Unit Scope、用户确认和下一 Agent Run 创建；
2. Python `ReviewableUnitModule`：通过一个 `run(request)` Interface 生成 Outline、当前
   Unit、单 Unit Patch 或 Full Review Report，并在内部复用 Phase 3～5 的 Need、
   Investigation、Knowledge、Grounding 与共享 Ledger。

该结构通过删除测试：删除 Go Module 会让锁定、依赖、版本和下一 Run 规则重新散落到
HTTP、storage、dispatcher；删除 Python Module 会让生成、Repair、Grounding 和 Quality
顺序重新散落到 Graph 节点。两者均提供实际 Depth、Leverage 与 Locality，不是新的
pass-through。

本阶段必须完成以下语义切换：

- 一个 v4 Agent Run 只有一个持久化 `RunPurpose`；
- `GENERATE_UNIT/REVISE_UNIT` 只允许提交一个 current Unit；
- Outline 和 `UnitScope` 由 Go 冻结，Python 只读；
- Repair 只产生当前 Unit 的有界 replacement patch，随后重新提取 Claim、Grounding 和
  Quality；
- Full Review 只输出问题和 `affected_unit_keys`，不能直接改已确认内容；
- Python 返回候选结果后结束，不能在 Worker 内等待用户；
- Published PRD 仍由 Go 按锁定 Outline 聚合已确认 Unit 后写入 Feishu。

---

## 1. 当前实现与必须解决的问题

### 1.1 当前 whole-draft producer

当前 `agent-python/agent/graph/advanced.py` 的顺序是：

```text
DraftBundle(all units)
  -> Grounding(all claims)
  -> basic Quality
  -> build_confirmation_units(all units)
  -> SubmitDraft(working-draft.v2)
```

`DraftBundle.build()` 可以从一份 Markdown 自动拆出多个 Unit；
`build_confirmation_units()` 再将所有 Unit 一次性放入 `working-draft.v2`。这违反 PRD：

- Agent 每次只生成一个 Confirmation Unit；
- 当前 Unit 未确认前，不得生成下一个；
- 已确认内容是稳定只读上下文；
- Outline 锁定后不得自行增删、合并或重排章节。

### 1.2 Go 只在 Draft 提交后发现 Unit

当前 Go `ParseConfirmationCandidates()` 从 `working-draft.v2` 才创建 Confirmation Unit；
因此 Go 事前不知道：

- Outline 版本和锁定 hash；
- 当前 Unit 是否 dependency-ready；
- 本 Run 应生成、修订还是全文检查；
- 模型是否越界输出了其他 Unit；
- 用户确认后应创建哪个下一 Run。

现有 reopen 路径已有 `RevisionScope` 和 immutable Unit hash 保护，可作为迁移基础，但它
仍要求下一份 whole draft 携带所有 immutable Unit。

### 1.3 Quality Module 过浅

当前高级 Quality 主要检查：

- Grounding 是否 PARTIAL；
- Unit 是否为空；
- Unit key 是否重复。

它没有覆盖 PRD 7.13 的章节完整性、术语、规则、角色权限、状态/异常闭环、Unknown、
Source Conflict 和验收标准可执行性。继续向 `check_advanced_quality()` 增加零散条件会扩大
其 Interface，并让 Graph 知道更多分类和修复细节；Phase 6 应将其深化为
`DocumentQualityModule.evaluate()`。

### 1.4 不采用的方向

- 不继续扩展 `working-draft.v2` 让每个 Run 重传所有 Unit；
- 不把 Outline 锁定和 dependency-ready 判定交给模型；
- 不让 Python 创建下一 Agent Run 或修改 Task/Confirmation 状态；
- 不让 `FULL_REVIEW` 自动改写已确认 Unit；
- 不把 confirmed Unit 正文复制为第二份长期 canonical PRD；
- 不用一个全局 `advanced_loop_mode` 替代每个 Run 的持久化 Purpose/Scope；
- 不为了目录整齐先拆 `runtime.py`，而不先建立可验证的 Interface。

---

## 2. 目标、非目标与完成定义

### 2.1 目标

- Go 原子持久化 `RunPurpose` 与冻结的 `UnitScope`；
- Outline 最多三级，包含稳定 node key、Unit key、顺序、依赖和 required sections；
- 用户确认 Outline 后其 version/hash 不可被运行时静默改变；
- Go 只为一个 dependency-ready Unit 创建 `GENERATE_UNIT` Run；
- Python 对 Purpose 做严格输入/输出配对并只返回一种版本化结果；
- `REVISE_UNIT` 只修改已 reopen 的 current Unit；
- immutable Unit 的 content hash 在 revision 前后保持不变；
- Quality 先执行确定性 Policy，再对需要语义判断的类别调用受 Ledger 管理的远程模型
  Adapter；
- Repair 后重新执行 Claim extraction、Knowledge Grounding 和 Quality；
- 全部 Unit 确认后自动创建一个 `FULL_REVIEW` Run；
- Full Review 完成且没有 blocking issue 后才允许 Publish Preview；
- v1 whole-draft reader、已有 v1 Run 和历史 Working Draft 继续可读。

### 2.2 非目标

- 不切换 production 默认 workflow；由 Phase 7 完成；
- 不删除 v1 whole-draft producer/reader；仅停止 v4 使用它；
- 不改变 Phase 3 Requiredness、Phase 4 Fact 或 Phase 5 Investigation 语义；
- 不在 Worker 内处理用户确认、飞书发布或 Task 生命周期；
- 不引入并行 Unit 生成、多 Agent 或并行模型/Capability 调用；
- 不把 Full Review Report 变成 Published PRD 正文的一部分；
- 不在本阶段声明真实模型质量、PostgreSQL 或 staging Gate 已通过。

### 2.3 完成定义

- `GENERATE_UNIT/REVISE_UNIT` 的 accepted output 恰好包含 `current_unit_key`；
- Outline hash、Unit scope hash、expected Task version 任一不匹配均 fail closed；
- 当前 Unit 未确认时不会创建下一 Unit Run；
- 非 dependency-ready Unit 不可被选为 current Unit；
- Repair 越界、删除 Unknown、替换 Evidence、修改 immutable hash 均被拒绝；
- Quality 至少覆盖第 8 节的稳定 code；
- `FULL_REVIEW` 只持久化 report，不写 Confirmation Unit Version；
- Publish Preview 只聚合当前 locked Outline 下全部 confirmed latest Unit Version；
- crash/resume 任一 durable 状态不重复已 ACK 的模型或 Capability 物理调用。

---

## 3. 所有权与核心不变量

### 3.1 Go Control Plane 所有权

Go 唯一拥有：

- Outline identity、version、状态和 locked hash；
- Confirmation Unit identity、顺序、依赖和 confirmation status；
- Run Purpose、Unit Scope、Task version 和 scope hash；
- dependency-ready Unit 选择；
- 用户确认/reopen 与下一 Agent Run 创建；
- Full Review readiness 与 Publish Preview gate；
- 已确认 Unit 的最终聚合顺序。

### 3.2 Python Worker 所有权

Python 只负责计算：

- `PLAN_OUTLINE` 的候选 Outline；
- current Unit 的 Information Need、Investigation、Knowledge、Draft、Grounding、Quality；
- current Unit 的有界 Repair；
- 全文只读检查产生的 Review Issue。

Python 不拥有 durable Task/Unit 状态，不连接 PostgreSQL，不决定最终确认或发布。

### 3.3 不可破坏的不变量

1. 一个 Agent Run 对应一个 `RunPurpose`，创建后不可变。
2. `GENERATE_UNIT/REVISE_UNIT` 的 `UnitScope.current_unit_key` 非空且唯一。
3. `PLAN_OUTLINE/FULL_REVIEW` 不携带可写 current Unit。
4. Outline 只有用户确认后进入 `LOCKED`；锁定版本内容不可修改。
5. current Unit 的全部依赖必须是同一 locked Outline 下的 confirmed latest version。
6. confirmed context 只读；未 reopen Unit 的 hash 不得变化。
7. Revision Patch 只能替换 current Unit 正文，不是任意 JSON Patch。
8. Repair 后必须重新提取 Claim 并重新 Grounding，不能复用旧 pass 结果。
9. `FULL_REVIEW` 只能产生 Report；任何内容修改都要由用户选择 Unit、reopen、重新生成和
   重新确认。
10. 所有远程调用继续经过 Phase 2 Ledger，共享 Run Budget。
11. 所有来源判断继续消费 Phase 4 `KnowledgeBundle`，不能退回 ref-presence Grounding。
12. 等待用户时没有 Python Worker 占用，符合 ADR-0003。

---

## 4. 目标 Module 与 Interface

### 4.1 Go `UnitRunOrchestrationModule`

建议目标文件：

```text
backend-go/internal/runcontrol/unit_orchestration.go
backend-go/internal/runcontrol/outline.go
backend-go/internal/storage/outline.go
backend-go/internal/storage/unit_orchestration.go
backend-go/internal/httpapi/router.go
```

外部 Interface 保持小而完整：

```go
type UnitRunOrchestrator interface {
    AcceptRunOutput(ctx context.Context, lease LeaseContext, output RunOutput) (RunOutputReceipt, error)
    ConfirmOutline(ctx context.Context, command ConfirmOutlineCommand) (TransitionResult, error)
    DecideUnit(ctx context.Context, command UnitDecisionCommand) (TransitionResult, error)
    BuildPublishDocument(ctx context.Context, command BuildPublishCommand) (PublishDocument, error)
}
```

Interface 隐藏：版本锁、依赖图、scope hash、Task optimistic lock、幂等、下一 Run、Outbox、
legacy reader 选择。HTTP 与 dispatcher 只提交 command 并消费 transition result。

### 4.2 Python `ReviewableUnitModule`

建议目标文件：

```text
agent-python/agent/unit/models.py
agent-python/agent/unit/runner.py
agent-python/agent/unit/generator.py
agent-python/agent/unit/repair.py
agent-python/agent/unit/invariants.py
agent-python/agent/quality/models.py
agent-python/agent/quality/policies.py
agent-python/agent/quality/classifier.py
agent-python/agent/quality/full_review.py
```

唯一外部 Interface：

```python
class ReviewableUnitModule:
    def run(self, request: UnitRunRequest) -> UnitRunResult: ...
```

`run()` 根据 Purpose 在 Implementation 内路由；Graph 只负责恢复入口和结果提交，不理解
Outline、Patch、Quality code 或 immutable 规则。模型、Quality classifier、Artifact sink
和 Phase 5 Investigation Runner 是 Module 内部 Seam；只有 remote/test 两个 Adapter 已真实
存在的 Seam 才保留。

### 4.3 Interface 数据模型

```python
class RunPurpose(StrEnum):
    PLAN_OUTLINE = "PLAN_OUTLINE"
    GENERATE_UNIT = "GENERATE_UNIT"
    REVISE_UNIT = "REVISE_UNIT"
    FULL_REVIEW = "FULL_REVIEW"

@dataclass(frozen=True)
class UnitRunRequest:
    purpose: RunPurpose
    scope: UnitScope
    requirement_brief: RequirementBriefRef
    confirmed_context: tuple[ConfirmedUnitContext, ...]
    resume: UnitResumeCursor | None

@dataclass(frozen=True)
class UnitRunResult:
    purpose: RunPurpose
    output_kind: RunOutputKind
    scope_hash: str
    artifact_key: str
    content_hash: str
    payload: Mapping[str, object]
```

Purpose 与 Output 必须一一对应：

| Run Purpose | 唯一允许 Output | 是否创建 Unit Version |
| --- | --- | --- |
| `PLAN_OUTLINE` | `OUTLINE_CANDIDATE` | 否 |
| `GENERATE_UNIT` | `UNIT_CANDIDATE` | 是，PENDING |
| `REVISE_UNIT` | `UNIT_PATCH` | 是，PENDING |
| `FULL_REVIEW` | `FULL_REVIEW_REPORT` | 否 |

---

## 5. 合同设计与版本策略

### 5.1 expand-only Proto

在 `AgentRunInput` 的现有字段 23 后增加：

```proto
RunPurpose run_purpose = 24;
UnitScope unit_scope = 25;
```

新增：

```proto
enum RunPurpose {
  RUN_PURPOSE_UNSPECIFIED = 0;
  RUN_PURPOSE_PLAN_OUTLINE = 1;
  RUN_PURPOSE_GENERATE_UNIT = 2;
  RUN_PURPOSE_REVISE_UNIT = 3;
  RUN_PURPOSE_FULL_REVIEW = 4;
}

message UnitScope {
  string schema_version = 1;       // unit-scope.v1
  string outline_id = 2;
  int64 outline_version = 3;
  string outline_hash = 4;
  string current_unit_key = 5;
  string current_unit_title = 6;
  int32 current_unit_ordinal = 7;
  repeated string section_node_keys = 8;
  repeated string dependency_unit_keys = 9;
  repeated ConfirmedUnitContext confirmed_context = 10;
  repeated string reopened_unit_keys = 11;
  repeated string immutable_unit_keys = 12;
  string requirement_brief_ref = 13;
  string requirement_brief_hash = 14;
  string user_feedback = 15;
  string scope_hash = 16;
}

message ConfirmedUnitContext {
  string unit_key = 1;
  int64 unit_version = 2;
  string content_hash = 3;
  string summary = 4;
  string working_draft_ref = 5;
  string markdown = 6;
}
```

`summary`、`markdown` 和 `working_draft_ref` 有单项及总量上限。普通 Unit 生成默认只携带
依赖摘要，需要全文一致性判断时才携带当前 Task 的 bounded confirmed Markdown；
`FULL_REVIEW` 携带全部 confirmed Unit Markdown。它们只用于活动恢复和当前 Task 的生成
上下文，不形成第二个 Published PRD 权威，符合 ADR-0001。

### 5.2 不复用 `SubmitDraft` 表达所有结果

`SubmitDraft` 的 Interface 名称、错误和存储语义都假设“提交 Working Draft”。把 Outline、
Unit Patch 和 Review Report 塞入该调用会让调用者了解内部 payload 分支，形成浅 Module。

新增 `SubmitRunOutput`：

```proto
message RunOutput {
  string schema_version = 1;       // run-output.v1
  string output_key = 2;
  RunOutputKind output_kind = 3;
  RunPurpose run_purpose = 4;
  string scope_hash = 5;
  int64 expected_task_version = 6;
  string content_hash = 7;
  bytes payload = 8;
}
```

direct RPC 和 Worker stream 均扩展 `run_output` 事件。旧字段 `draft_key/draft_patch` 保留给
v1 reader/producer；v4 不再调用 `SubmitDraft`。

### 5.3 合同校验顺序

Go 接受输出前固定执行：

1. lease/fencing 有效；
2. Run workflow 为 v4，Purpose 与持久化值相同；
3. output kind 与 Purpose 一一对应；
4. expected Task version 相同；
5. `scope_hash` 与 `go_run_unit_scopes` 相同；
6. Outline version 仍 locked 且 hash 相同；
7. current Unit 仍处于期望状态并 dependency-ready；
8. payload schema、大小和 content hash 有效；
9. immutable context hash 与数据库一致；
10. 以 `run_id + output_key` 幂等持久化，再 ACK。

任一步失败都不创建 Unit Version，不更新 Task，不创建下一 Run。

---

## 6. Outline、Unit Scope 与持久化

### 6.1 `outline-candidate.v1`

```json
{
  "schema_version": "outline-candidate.v1",
  "title": "...",
  "requirement_size": "MEDIUM",
  "nodes": [
    {
      "node_key": "acceptance.criteria",
      "parent_key": "acceptance",
      "ordinal": 30,
      "title": "验收标准",
      "questions": ["如何验证系统预期结果？"],
      "required_content": ["precondition", "trigger", "expected_result"],
      "unit_key": "acceptance"
    }
  ],
  "units": [
    {
      "unit_key": "acceptance",
      "title": "验收标准",
      "ordinal": 30,
      "node_keys": ["acceptance", "acceptance.criteria"],
      "depends_on": ["solution"]
    }
  ]
}
```

稳定 key 来自 normalized semantic path + hash suffix，不使用数组 index；ordinal 只控制显示
顺序。校验必须拒绝重复 key、悬空 parent、依赖缺失、依赖环、超过三级、Unit 为空、Unit
超过 15 个和 node 未归属 Unit。

### 6.2 PostgreSQL 迁移

新增迁移建议 `0014_reviewable_unit_runs.sql`：

```text
go_prd_outlines
  outline_id, task_id, created_at

go_prd_outline_versions
  outline_version_id, outline_id, version, status,
  payload, content_hash, source_run_id, created_at, locked_at

go_run_unit_scopes
  run_id, run_purpose, outline_version_id, current_unit_key,
  scope_payload, scope_hash, expected_task_version, created_at

go_full_review_reports
  report_id, task_id, outline_version_id, source_run_id,
  payload, content_hash, disposition, created_at
```

扩展 `go_confirmation_unit_versions`：

- `outline_version_id`；
- `source_run_id`；
- `unit_version_no`；
- v4 路径允许 `draft_id` 为空，legacy 路径继续非空；
- 唯一约束覆盖 `(unit_id, outline_version_id, unit_version_no)`。

旧 reader 继续按 latest `draft_id` 读取 v1；v4 reader 按 locked Outline 的每个 Unit identity
选择 latest Unit Version。不要把两套查询揉成一个隐含 fallback，显式使用
`workflow_version`/outline presence 选择 Adapter。

### 6.3 Task 状态投影

建议将已有字符串状态收敛为以下合法迁移：

```text
DRAFT
  -> OUTLINE_REVIEW
  -> UNIT_GENERATING
  -> UNIT_REVIEW
  -> FULL_REVIEW_RUNNING
  -> FULL_REVIEW
  -> REVIEWABLE
  -> PUBLISHING / COMPLETED
```

失败和停止沿用现有状态。Agent Run 产生候选结果后终止为 `SUCCEEDED`；等待用户属于 Task
状态，不让已结束 Run 长期占用 Queue Slot。

---

## 7. Purpose 执行流程

### 7.1 `PLAN_OUTLINE`

```text
validate Requirement Brief
  -> Information Need (outline scope)
  -> optional/required Investigation
  -> generate Outline Candidate
  -> deterministic outline invariants
  -> save OUTLINE_CANDIDATE Artifact
  -> checkpoint READY_TO_SUBMIT
  -> SubmitRunOutput
  -> Run succeeds / Task OUTLINE_REVIEW
```

用户调整大纲时创建新的 `PLAN_OUTLINE` Run 和新 Outline Version；旧版本保留审计但不能
继续生成。用户确认时 Go 将版本设为 `LOCKED`，创建稳定 Confirmation Unit identities，
并原子创建第一个 dependency-ready `GENERATE_UNIT` Run。

### 7.2 `GENERATE_UNIT`

```text
validate frozen UnitScope
  -> Information Need(current Unit)
  -> Investigation if required
  -> generate exactly current Unit
  -> extract Claims / materialize Unknowns
  -> Grounding
  -> at most one scoped Supplement through Phase 5
  -> DocumentQualityModule.evaluate(UNIT)
  -> at most one bounded Repair
  -> re-extract + re-Ground + re-Quality
  -> save UNIT_CANDIDATE Artifact
  -> SubmitRunOutput
```

已确认上下文只允许引用，输出不得包含其他 Unit body。若生成结果需要修改 Outline 或已
确认 Unit，返回 `NEEDS_HUMAN_DECISION` issue，不擅自扩展 scope。

### 7.3 `REVISE_UNIT`

输入包含 base Unit version/hash、用户 feedback 和 immutable context。输出
`unit-patch.v1`：

```json
{
  "schema_version": "unit-patch.v1",
  "unit_key": "acceptance",
  "base_content_hash": "...",
  "replacement_markdown": "...",
  "resolved_issue_ids": ["quality-..."],
  "preserved_unknown_ids": ["unknown-..."],
  "used_fact_ids": ["fact-..."]
}
```

采用 replacement 而不是通用 JSON Patch，使 Interface 只表达“替换一个 current Unit”，
同时保留 base hash 进行 optimistic lock。Repair/Revision Invariant 必须验证：

- `unit_key == current_unit_key`；
- base hash 与 latest reopened version 相同；
- replacement 非空、大小受限、标题来自 locked Outline；
- 未经用户解决的 Unknown 不能消失；
- Evidence/Fact 只能来自当前 Knowledge 和 confirmed context；
- immutable Unit key/hash 集合完全不变。

### 7.4 用户确认后的下一 Run

Go 在同一事务中：

1. 以 idempotency key 和 expected Task version 写 Confirmation Decision；
2. 标记 current Unit latest version `CONFIRMED`；
3. 重新计算 dependency-ready pending Unit；
4. 若存在，创建一个 `GENERATE_UNIT` Run、冻结 scope、取得 Queue Slot 或进入等待容量；
5. 若全部 confirmed，创建一个 `FULL_REVIEW` Run；
6. 写 Outbox 与 Task Event；
7. 提交事务。

排序规则为 dependency-ready 集合中最小 ordinal，再以 unit key 稳定排序。生产仍只执行
一个 Agent Run，不并行生成多个 ready Unit。

### 7.5 `FULL_REVIEW`

Full Review 读取 locked Outline、全部 confirmed latest Unit 的 bounded content/refs、
Knowledge/Grounding 摘要，输出：

```json
{
  "schema_version": "full-review-report.v1",
  "outline_hash": "...",
  "unit_hashes": {"scope": "...", "acceptance": "..."},
  "outcome": "PASSED|NEEDS_REVISION|NEEDS_HUMAN",
  "issues": [
    {
      "issue_id": "quality-...",
      "code": "INCONSISTENT_TERMINOLOGY",
      "severity": "BLOCKING|IMPORTANT|INFORMATIONAL",
      "affected_unit_keys": ["scope", "acceptance"],
      "message": "...",
      "suggested_action": "..."
    }
  ]
}
```

Go 验证 report 中 unit hash 与当前 confirmed version 完全相同。报告持久化后不创建新 Unit
Version。发现问题时由用户选择 affected Unit 并 reopen；修改后所有受影响内容重新确认，
再创建新的 Full Review Run。

---

## 8. Document Quality Module

### 8.1 Interface

```python
class DocumentQualityModule:
    def evaluate(self, request: QualityRequest) -> QualityReport: ...
```

`QualityRequest` 明确 `UNIT` 或 `FULL_DOCUMENT` scope。Implementation 顺序：

1. 确定性 shape/security/size Policy；
2. traceability/Grounding 投影检查；
3. 可确定性解析的验收标准、状态和权限检查；
4. 必要时通过 Ledger 调用语义 Quality classifier；
5. 稳定 issue identity、disposition 与 affected keys；
6. 决定 `PASSED / REPAIR_REQUIRED / NEEDS_HUMAN`。

### 8.2 稳定 Quality code

| Code | 默认处置 | 说明 |
| --- | --- | --- |
| `MISSING_REQUIRED_SECTION` | REPAIRABLE | locked Outline required content 未覆盖 |
| `BROKEN_TRACEABILITY` | REQUIRES_GROUNDING | Claim/Fact/Evidence 链断裂 |
| `INCONSISTENT_TERMINOLOGY` | REPAIRABLE 或 HUMAN | 同一概念使用冲突名称 |
| `RULE_CONFLICT` | NEEDS_HUMAN | 前后规则互斥 |
| `ROLE_PERMISSION_CONFLICT` | NEEDS_HUMAN | 角色与权限不一致 |
| `STATE_FLOW_NOT_CLOSED` | REPAIRABLE | 状态/异常缺少闭环 |
| `AMBIGUOUS_ACCEPTANCE_CRITERION` | REPAIRABLE | 缺前置条件、触发或预期结果 |
| `UNRESOLVED_SOURCE_CONFLICT` | NEEDS_HUMAN | Source Conflict 尚未由用户处理 |
| `UNMARKED_UNKNOWN` | REPAIRABLE | 未知被写成确定事实 |
| `UNSUPPORTED_CURRENT_STATE` | REQUIRES_GROUNDING | CURRENT_STATE 没有 Supported Fact |
| `SENSITIVE_CONTENT` | FATAL | 命中 credential/secret 规则 |
| `CONTENT_TOO_LARGE` | FATAL | Unit/Report 超合同上限 |
| `OUTLINE_SCOPE_VIOLATION` | FATAL | 增删、重排或越界 Unit |
| `IMMUTABLE_UNIT_CHANGED` | FATAL | 未 reopen Unit hash 变化 |

### 8.3 Repair 路由

- `REPAIRABLE` 且预算允许：最多一次 Unit Repair；
- `REQUIRES_GROUNDING`：最多一次 Phase 5 scoped Supplement，再 Grounding；
- `NEEDS_HUMAN`：保存报告并结束，不自动选择业务结论；
- `FATAL`：fail closed，不提交候选 Unit；
- 一个 issue 多次出现时使用稳定 identity，不重复消耗 repair budget；
- Full Review 的任何 issue 都不自动 Repair confirmed content。

---

## 9. Artifact、Checkpoint 与恢复

### 9.1 Artifact 类型

| 类型 | schema | 适用 Purpose |
| --- | --- | --- |
| `OUTLINE_CANDIDATE` | `outline-candidate.v1` | PLAN_OUTLINE |
| `UNIT_CANDIDATE` | `unit-candidate.v1` | GENERATE_UNIT |
| `UNIT_PATCH` | `unit-patch.v1` | REVISE_UNIT/repair |
| `QUALITY_REPORT` | `quality-report.v2` | Unit Quality |
| `FULL_REVIEW_REPORT` | `full-review-report.v1` | FULL_REVIEW |

Phase 3～5 Artifact 类型保持不变。Artifact 正文只用于活动恢复和有界保留；Go 的 Outline、
Confirmation 与 Review projection 是控制状态，不是 Published PRD 权威。

### 9.2 Snapshot v3 expand-only 状态

目标总设计已将 `UNIT_DRAFTED` 列入 `agent-loop-snapshot.v3`；Phase 6 以 expand-only 方式
增加：

```text
OUTLINE_DRAFTED
UNIT_DRAFTED
UNIT_PATCHED
FULL_REVIEW_DRAFTED
QUALITY_REPAIR_REQUIRED
QUALITY_PASSED
QUALITY_NEEDS_HUMAN
READY_TO_SUBMIT
```

同时保存 `run_purpose`、`scope_hash`、current unit key、outline hash、base Unit hash 和相关
Artifact identity。v1 Run 不产生这些状态；旧 Worker 不接收 v4 Run。

### 9.3 恢复顺序

- 模型结果先保存并 ACK Artifact，再完成 Ledger，再 checkpoint；
- `UNIT_DRAFTED` 恢复直接 hydration Artifact，不重复生成；
- Repair Artifact ACK 后 crash，恢复先校验 base hash，再重新 Grounding；
- `READY_TO_SUBMIT` 恢复先检查 Go 是否已有相同 output receipt；
- output 已 ACK 但 CompleteRun 未 ACK 时，只重放 CompleteRun；
- scope、outline、Task version 或 confirmed hash 漂移时 fail closed，不在 Python 合并。

---

## 10. 实施顺序与最小提交

### Step 6.0：Characterization

- 固定 v1 whole-draft、多 Unit、confirm/reopen、publish 现状；
- 增加失败测试证明当前会一次产出多个 Unit；
- 记录现有 Task/Run 状态和 reader payload。

提交：`test: characterize whole draft confirmation workflow`

### Step 6.1：合同与存储 expand

- 增加 RunPurpose、UnitScope、RunOutput；
- 生成 Go/Python 合同；
- 新增 Outline/Scope/Review 表和内存存储对等实现；
- v1 行为不变。

提交：`feat: add versioned run purpose unit scope and output`

### Step 6.2：Go Outline 锁定

- 实现 Outline schema/invariants；
- 增加 outline read/confirm/adjust HTTP Interface；
- 锁定后创建稳定 Unit identities；
- 创建第一个 dependency-ready Run。

提交：`feat: plan and lock reviewable units`

### Step 6.3：Python 单 Unit 生成

- 新增 `ReviewableUnitModule`；
- `PLAN_OUTLINE` 和 `GENERATE_UNIT` 接入 v4 Graph；
- current-only/outline/confirmed-context invariants；
- v4 使用 SubmitRunOutput。

提交：`feat: generate one confirmation unit per run`

### Step 6.4：Revision 与 immutable

- `REVISE_UNIT` 接入 replacement patch；
- Go 校验 base Unit version/hash；
- immutable context hash 检查；
- 删除 v4“重传全部 immutable Unit”路径。

提交：`feat: enforce scoped unit revision invariants`

### Step 6.5：Quality 深化

- 稳定 Quality code 与 disposition；
- deterministic Policy + Ledger model classifier；
- Repair 后 re-extract/re-Ground/re-Quality；
- size/security gate。

提交：`feat: deepen unit document quality checks`

### Step 6.6：Full Review 与发布 Gate

- all-confirmed 后创建 FULL_REVIEW Run；
- report 只读校验与 affected Unit reopen；
- Go 按 Outline 聚合 confirmed Units；
- Publish Preview 要求最新 report PASSED 且 hash set 匹配。

提交：`feat: gate publish on full document review`

### Step 6.7：双读与观测

- v1 whole-draft reader 保留；
- v4 current-unit reader/UI projection；
- 指标区分 workflow/purpose/output/disposition；
- 更新运维与迁移文档。

提交：`test: cover outline unit revision review and publish gates`

---

## 11. 单元与集成测试方案

### 11.1 Python 模型与不变量

新增：

```text
agent-python/tests/unit/test_models.py
agent-python/tests/unit/test_invariants.py
agent-python/tests/unit/test_runner.py
agent-python/tests/unit/test_repair.py
agent-python/tests/quality/test_policies.py
agent-python/tests/quality/test_classifier.py
agent-python/tests/quality/test_full_review.py
```

关键 case：

- Outline key/hash 对相同输入稳定；
- 重复 key、悬空 parent、依赖环、超过三级/15 Unit 被拒绝；
- GENERATE 只输出 current Unit；
- 标题/node 不属于 locked Outline 被拒绝；
- confirmed summary 只能作为上下文，不能被输出为其他 Unit patch；
- REVISE 的 base hash 漂移、越界 unit key、immutable change 被拒绝；
- Repair 删除 unresolved Unknown 或引用不存在 Fact 被拒绝；
- Repair 后 Grounding/Quality 的 Artifact generation 增加；
- 验收标准缺少 precondition/trigger/expected result 产生稳定 code；
- FULL_REVIEW 不调用 Unit writer，不返回 patch；
- sensitive marker 和 payload size 在模型提交前失败。

### 11.2 Graph/恢复测试

- 每个 Purpose 从初始状态到 READY_TO_SUBMIT；
- `UNIT_DRAFTED` crash 恢复不重复 model call；
- Repair ACK 前后 crash matrix；
- Grounding Supplement 使用 Phase 5 Runner；
- output ACK 后 crash 只重放 CompleteRun；
- snapshot purpose/scope/current Unit 漂移 fail closed；
- Run Budget 对 generation、classifier、supplement、repair 统一扣减。

### 11.3 Go 内存存储测试

- Run Purpose/Scope 创建后不可变；
- Outline confirm idempotent、expected Task version 冲突；
- 只选择 dependency-ready 最小 ordinal Unit；
- 当前 Unit 未确认时不创建下一 Run；
- 确认最后 Unit 创建 FULL_REVIEW；
- reopen 只允许 confirmed leaf，或显式要求同时 reopen dependents；
- SubmitRunOutput purpose/kind/scope/hash mismatch 全部拒绝；
- immutable Unit content hash 保持；
- Full Review 不创建 Confirmation Unit Version；
- report unit hashes 过期时 Publish Preview 被拒绝；
- v1 SubmitDraft/reader 行为不变。

### 11.4 PostgreSQL 与 HTTP 测试

- migrations 可从当前 schema 升级并保留 v1 数据；
- Outline lock、Unit confirm、下一 Run、Outbox 在同一事务；
- 并发 confirm 只有一个成功创建下一 Run；
- duplicate output key 同 hash replay，异 hash 拒绝；
- tenant/owner 访问控制；
- list endpoint 分别正确投影 v1/v4；
- rollback migration 不丢已有 v1 Working Draft；
- publish aggregation 顺序、标题与 hash 稳定。

### 11.5 性质测试矩阵

| 性质 | 生成策略 | 断言 |
| --- | --- | --- |
| dependency DAG | 随机无环/有环 Outline | 只选 ready；环被拒绝 |
| immutable | 随机 confirmed hash 集 | 非 reopen hash 永不变化 |
| current-only | 随机额外 Unit payload | 任何越界输出被拒绝 |
| optimistic lock | 随机 version/hash 漂移 | 不产生部分写 |
| resume | 每个 ACK 后注入 crash | 物理调用最多一次 |
| idempotency | 相同/不同 output hash | 相同 replay；不同拒绝 |

---

## 12. 验证命令与 Gate

本地快速验证：

```bash
venv/bin/python -m pytest -q agent-python/tests
venv/bin/python -m pytest -q tests
cd backend-go && go test ./...
cd backend-go && go vet ./...
buf lint contracts
buf generate contracts
npm --prefix web run test
npm --prefix web run typecheck
```

PostgreSQL Gate：

```bash
PRD_AGENT_TEST_DATABASE_DSN="$PRD_AGENT_DATABASE_DSN" \
  venv/bin/python -m pytest -q
cd backend-go && PRD_AGENT_TEST_DATABASE_DSN="$PRD_AGENT_DATABASE_DSN" go test ./...
```

Phase 6 Gate 全部满足才可进入 Phase 7：

- 一个 v4 generation/revision Run 只提交一个 current Unit；
- Outline 锁定、dependency-ready、current Unit 与 immutable hash Gate 全通过；
- 当前 Unit 未确认不会创建下一 Unit 或 Publish Preview；
- all-confirmed 自动进入 Full Review，不直接发布；
- Full Review 不修改 confirmed content；
- Quality code 覆盖 PRD 7.13 类别；
- v1 reader 和已有 active Run 回归通过；
- PostgreSQL 并发/幂等测试通过；
- 未执行真实 staging 时文档必须明确标记，不能宣布 rollout ready。

---

## 13. 风险与回滚

| 风险 | 控制 | 回滚 |
| --- | --- | --- |
| Go 状态迁移扩大 | transition table + optimistic lock + 单事务 | 停止创建 v4 Unit Run，保留 v1 reader |
| Outline 锁定后模型越界 | scope hash + deterministic invariants | output 拒绝，Task 保持可重试 |
| confirmed context 过大 | summary/ref 上限，按依赖选择 | 降低上下文，不改 confirmed content |
| Repair 掩盖 Grounding 缺口 | re-extract + re-Ground 强制顺序 | NEEDS_HUMAN，不提交候选 |
| Full Review 报告过期 | unit hash set 绑定 | 丢弃过期 report，重跑 Review |
| 双 reader 查询混淆 | workflow/outline 显式选择 Adapter | v4 switch off，继续 v1 reader |
| Artifact/Working Draft 增长 | 类型/大小白名单 + bounded retention | 停止新 Run，清理孤立临时 Artifact |
| 迁移中断 | expand-only migration 与可重放 backfill | 不 contract schema，旧路径继续运行 |

回滚不得把 v4 snapshot 交给 v1 Worker。先停止创建新 v4 Run，drain 或明确终止 active v4
Run，再切默认；该流程由 Phase 7 固化和演练。

---

## 14. 完成清单

- [x] RunPurpose/UnitScope/RunOutput expand-only 合同完成
- [ ] Go Outline/Scope/Review persistence 和内存对等实现完成
- [ ] Outline confirm 后只创建一个 dependency-ready Run
- [x] Python ReviewableUnitModule 接入四种 Purpose
- [x] GENERATE/REVISE current-only 与 immutable Gate 完成
- [ ] Quality code、Repair re-Grounding 和 Full Review 完成
- [ ] Go confirmed Unit aggregation 与 Publish Preview Gate 完成
- [x] v1 whole-draft reader/active Run 回归通过
- [x] Agent Python、root Python、Go、web、contract 本地 Gate 通过
- [ ] PostgreSQL 并发与 migration Gate 通过
- [ ] 设计状态更新为 IMPLEMENTED / LOCALLY VERIFIED

# Agent Loop Phase 8：Review Workflow 编排闭环与 Operational Graduation 设计及实施 Plan

> 状态：PARTIALLY IMPLEMENTED / LOCAL AND POSTGRESQL SCHEMA GATES PASSED / V4 PRODUCTION SEAMS OPEN
> 日期：2026-08-01
> 前置阶段：[Phase 6 Reviewable Unit + Quality](./2026-08-01-agent-loop-phase-6-reviewable-unit-quality-design.md)、[Phase 7 Rollout 基础](./2026-08-01-agent-loop-phase-7-rollout-legacy-removal-design.md)
> 对应总设计：[Agent Loop 语义深化设计](../specs/2026-07-31-agent-loop-semantic-deepening-design.md)
> 目标版本：`agent-runtime.v4`，不修改已有 v1 Run/snapshot 的语义
> 后续阶段：[Phase 9～11 剩余实现](./2026-08-01-agent-loop-remaining-implementation-design.md)
> 当前验证：Go test/vet、Agent Python、root Python、Web test/typecheck 已通过；2026-08-02 已在
> 两个隔离 PostgreSQL 数据库验证 clean migration 0001→0017、schema-only 0015→0017 和现有
> 3 个 integration tests。当前 tests 不含 v4 Review Workflow，因此 v4 PostgreSQL E2E、并发与
> replay 仍未验证。生产 `ReviewableUnitRuntime` 尚未注入完整 Grounding Adapter，Shadow 尚未
> 接入 authoritative trace。

---

## 0. 执行结论

Phase 8 不再增加新的 Agent 算法，而是闭合已经存在但尚未贯通的 authoritative 工作流：

```text
PLAN_OUTLINE output
  -> Go materialize Outline Candidate
  -> user confirms locked Outline
  -> Go creates exactly one dependency-ready GENERATE_UNIT Run
  -> Python Need / Investigation / Knowledge / Grounding / Quality
  -> Go materializes exactly one current Confirmation Unit Version
  -> user confirms or reopens
  -> Go creates next Unit Run or FULL_REVIEW Run
  -> Full Review binds exact confirmed Unit hash set
  -> Go assembles immutable publish document
  -> Publish Preview / Feishu
```

本阶段深化三个主 Module，并为第四个 Module 建立可执行地基：

1. Go `ReviewWorkflowModule`：拥有 Outline lock、RunOutput materialization、Confirmation
   decision、dependency-ready 选择、下一 Agent Run、Task projection 和 Outbox 原子性；
2. Python `ReviewableUnitExecutionModule`：在一个 `run(request)` Interface 后真正执行
   Phase 3～5 语义、Claim extraction、Grounding、Quality、Repair 和 re-Grounding；
3. Go `PublishDocumentCompatibilityModule`：显式选择 v1 Working Draft Adapter 或 v4
   Locked Outline Adapter，生成同一 immutable publish document；
4. Go `WorkflowRolloutControlModule`：本阶段只完成 GateDecision、Drain 和 legacy inventory
   的持久化/演练能力，不执行 production canary 或 legacy 删除。

推荐顺序固定为 `Review Workflow -> Python Execution -> Publish Compatibility -> Rollout
Control -> Zero-Remote-Effects Shadow`。Phase 8 Gate 未通过前不得启用 production canary。

---

## 1. 当前实现证据与剩余缺口

### 1.1 已经具备的能力

- protobuf 已扩展 `RunPurpose`、`UnitScope`、`RunOutput` 和 `SubmitRunOutput`；
- Go 已校验 Purpose/Output kind、scope hash、Task version、current Unit、Outline DAG、
  immutable/base hash 和 Full Review 只读属性；
- Go 内存与 PostgreSQL Adapter 已持久化 Run Scope/Output；
- 新 v4 Agent Run 会获得 `PLAN_OUTLINE` scope，retry/reopen 会继承 rollout assignment；
- Python LangGraph 已按四种 Purpose 返回独立 `RunOutput`，不再把 v4 结果塞进 legacy Draft；
- Python 已有稳定 Quality code、Full Review Policy、Ledger、Knowledge、Grounding 和 Unified
  Investigation Module；
- Go 已有稳定 cohort assignment；Python/Go 已有 fail-closed Gate evaluator；
- Python 已有 `NoRemoteEffectsAdapter` 和 deterministic Shadow evaluator；
- 本地 Agent Python、root Python、Go、Go Vet 和 Web Gate 已通过。

### 1.2 authoritative 主链仍未闭合

`SubmitRunOutput` 当前只保存 opaque payload。它没有在同一事务中：

- 物化 Outline Candidate / locked Outline Version；
- 物化 current Confirmation Unit Version；
- 更新 PRD Task 的 review 状态；
- 根据依赖创建唯一下一 Agent Run；
- 最后一个 Unit 确认后创建 `FULL_REVIEW` Run；
- 保存 Full Review Report 并绑定 confirmed Unit hash set。

因此合同和校验已经存在，但用户仍无法仅通过 v4 RunOutput 完成整条 PRD Task。

### 1.3 Python Module 仍有浅 Interface 泄漏

当前 `ReviewableUnitRuntime` 在 Graph 和 `ReviewableUnitModule` 之间重新暴露 operation、JSON
schema 和 payload 组装；`required_content` 与 locked Outline payload 通过
`requirement_brief_ref` 的字符串前缀传递。`REVISE_UNIT` 只标记
`requires_regrounding=true`，没有强制执行：

```text
replacement patch
  -> claim extraction
  -> Knowledge Grounding
  -> Quality
  -> bounded repair
  -> re-extract / re-Ground / re-Quality
```

这使 Module 的 Interface 接近 Implementation，Depth 不足。

### 1.4 Publish 仍由 legacy Working Draft 驱动

内存与 PostgreSQL 的 `CreatePublishPreview` 都读取 latest Working Draft。v4 locked Outline、
confirmed latest Confirmation Unit 和 Full Review disposition 尚未参与 Publish Gate。若直接在
两套 Store Adapter 与 HTTP handler 中增加 `if workflow == v4`，版本选择会跨 Seam 泄漏。

### 1.5 Rollout 只有局部实现

迁移已经创建 assignment、gate decision 和 drain 表，但当前代码只实现 assignment 读写。
缺少：

- hash-bound GateDecision persistence；
- rollout stage transition；
- active Run/dispatch/outbox/ledger/snapshot inventory；
- Drain refresh/complete；
- legacy producer/reader removal eligibility；
- production Shadow 与 Ledger deny 的实际连接。

---

## 2. 目标、非目标与完成定义

### 2.1 目标

- 每个 v4 RunOutput 在一个 Go 事务中被验证、持久化并物化为唯一领域结果；
- Outline Candidate 可调整，确认后变成不可变 locked Outline Version；
- locked Outline 只创建一个 dependency-ready current Unit Run；
- current Unit 未确认时不创建下一 Unit Run；
- `REVISE_UNIT` 只产生 reopened current Unit 的新版本，其他 confirmed hash 不变；
- Python Repair 后真实执行 re-Grounding 和 re-Quality；
- all-confirmed 自动创建一个 `FULL_REVIEW` Run；
- Full Review Report 绑定 exact outline hash + unit hash set；
- v4 Publish Preview 由 locked Outline 顺序聚合 confirmed latest Unit；
- v1 reader 和历史 Working Draft 行为保持不变；
- Memory/PostgreSQL Adapter 通过同一 Interface contract suite；
- GateDecision、Drain 和 legacy inventory 具备 durable、可重放的本地实现；
- 为 Phase 9 输出明确、机器可读、hash-bound 的 readiness record。

### 2.2 非目标

- 不在本阶段执行真实 production canary、默认切 v4 或删除 legacy；
- 不把 PostgreSQL 变成 Published PRD 的 canonical owner；
- 不允许 Python 直接创建 Agent Run、更新 Task 或连接 PostgreSQL；
- 不并行生成多个 Confirmation Unit；
- 不在 Shadow 中调用远程模型或 Capability；
- 不修改已有 v1 snapshot 或把 v4 snapshot 路由给 v1 Worker；
- 不通过扩大预算绕过 Quality、Grounding 或 dependency Gate；
- 不把 rollout 决策交给 Python Eval 进程。

### 2.3 完成定义

Phase 8 完成必须同时满足：

1. 从新建 v4 PRD Task 到 Publish Preview 的完整内存测试通过；
2. 同一流程的 PostgreSQL 集成测试通过；
3. 并发 Outline confirm / Unit confirm 只创建一个下一 Run；
4. RunOutput ACK 后任意 crash 重放不产生重复 Unit Version/Run/Outbox；
5. Repair 后 Grounding Artifact generation 与 Quality generation 单调增加；
6. Full Review hash set 漂移会阻止 Publish Preview；
7. v1 whole-draft confirm/reopen/publish fixture 不变；
8. rollout stage 无 GateDecision 或 inventory 非零时 fail closed；
9. 所有本地 Gate 与配置的 PostgreSQL Gate 通过；
10. 文档状态只更新为 `LOCALLY VERIFIED`，不宣称 staging/production ready。

---

## 3. 所有权与不变量

### 3.1 Go Control Plane 唯一拥有

- PRD Task review state；
- Outline identity/version/status/locked hash；
- Confirmation Unit identity、ordinal、dependency 和 latest version；
- Run Purpose、Unit Scope、Task optimistic version；
- RunOutput materialization 与幂等；
- 用户确认/reopen；
- 下一 dependency-ready Agent Run 与 Outbox；
- Full Review readiness/disposition；
- v1/v4 publish document Adapter 选择；
- rollout stage、GateDecision、Drain 和 legacy inventory。

### 3.2 Python Worker 只拥有计算

- Outline Candidate；
- current Unit Candidate / Patch；
- Information Need、Investigation、Knowledge、Claim、Grounding 和 Quality 计算；
- Full Review Report；
- Ledger 管理下的远程模型/Capability 调用；
- 版本化 Artifact 与 RunOutput 编码。

### 3.3 核心不变量

1. 一个 Agent Run 只有一个不可变 Purpose 和 scope hash。
2. 一个 accepted RunOutput 只物化一个 Purpose 对应的结果。
3. Outline lock 后 payload/hash/version 不可改变。
4. 同一 Task 同时最多存在一个非终态 authoritative v4 review Run。
5. dependency-ready 由 Go 从 durable confirmed status 计算，模型无权选择。
6. current Unit 未确认时不得创建下一 Unit Run。
7. confirmed 且未 reopen 的 Unit hash 永不变化。
8. Full Review 只能写 Report，不能写 Confirmation Unit Version。
9. Publish Preview 必须绑定最新 locked Outline、latest confirmed Unit hash set 和 PASSED
   Full Review Report。
10. Outbox、Task version、领域结果和下一 Run 在同一 PostgreSQL 事务提交。
11. 远程调用期间不持有 PostgreSQL 事务。
12. Published PRD canonical owner 仍是 Feishu，符合 ADR-0001。
13. production 只使用 remote LLM Adapter，符合 ADR-0002。
14. 等待用户不占 Worker，Queue Slot/Run Admission 仍由 PostgreSQL 控制，符合 ADR-0003。

---

## 4. 目标 Module、Interface 与 Seam

### 4.1 Go `ReviewWorkflowModule`

建议文件：

```text
backend-go/internal/runcontrol/review_workflow.go
backend-go/internal/runcontrol/review_models.go
backend-go/internal/runcontrol/review_invariants.go
backend-go/internal/storage/review_workflow.go
backend-go/internal/runcontrol/review_memory.go
```

外部 Interface：

```go
type ReviewWorkflow interface {
    AcceptRunOutput(ctx context.Context, command AcceptRunOutputCommand) (ReviewTransition, error)
    ConfirmOutline(ctx context.Context, command ConfirmOutlineCommand) (ReviewTransition, error)
    DecideUnit(ctx context.Context, command DecideUnitCommand) (ReviewTransition, error)
    ResolveFullReview(ctx context.Context, command ResolveFullReviewCommand) (ReviewTransition, error)
    BuildPublishDocument(ctx context.Context, command BuildPublishDocumentCommand) (PublishDocument, error)
}
```

`ReviewTransition` 只暴露调用者真正需要的结果：

```go
type ReviewTransition struct {
    TaskVersion int
    TaskStatus ReviewTaskStatus
    MaterializedKind string
    MaterializedID string
    CreatedRun *AgentRun
    EventSequence int64
}
```

Implementation 隐藏：

- lease/fencing 和 expected Task version；
- command idempotency；
- output payload/schema/hash 校验；
- Outline DAG、lock 与 Unit identity 创建；
- dependency-ready 选择；
- Run Admission、Queue Slot、Budget、rollout assignment 继承；
- Task projection 与 Outbox；
- Full Review hash set 和 Publish Gate；
- v1/v4 Adapter 选择。

Memory 与 PostgreSQL 是两个真实 Adapter，证明该 Seam 不是 hypothetical。删除此 Module 会让
上述复杂度重新散落到 dispatcher、HTTP、confirmation、publish 和两套 Store Adapter，符合
删除测试。

### 4.2 Python `ReviewableUnitExecutionModule`

建议深化：

```text
agent-python/agent/unit/execution.py
agent-python/agent/unit/models.py
agent-python/agent/unit/invariants.py
agent-python/agent/unit/transport.py
agent-python/agent/quality/classifier.py
agent-python/agent/quality/repair.py
```

唯一外部 Interface 保持：

```python
class ReviewableUnitExecutionModule:
    def run(self, request: UnitExecutionRequest) -> UnitRunResult: ...
```

将以下信息改为显式类型，不再编码进 ref 字符串：

```python
@dataclass(frozen=True)
class UnitExecutionRequest:
    purpose: RunPurpose
    scope: UnitScope
    requirement_brief: RequirementBrief
    locked_outline: LockedOutline | None
    confirmed_context: tuple[ConfirmedUnitContext, ...]
    resume: UnitResumeCursor | None
```

Implementation 内部顺序：

```text
Purpose validation
  -> Information Need
  -> Unified Investigation
  -> Knowledge Bundle
  -> candidate/patch generation
  -> Claim extraction
  -> Grounding
  -> deterministic Quality
  -> optional Ledger-managed classifier
  -> bounded repair
  -> Claim re-extraction
  -> re-Grounding
  -> re-Quality
  -> RunOutput
```

只保留已经真实变化的内部 Seam：remote/test Model Adapter、Capability Adapter、Ledger
Adapter。吸收现有浅 `ReviewableUnitRuntime`，Graph 只负责 hydrate、调用和提交，不理解
Purpose schema、Quality code 或 repair 顺序。

### 4.3 Go `PublishDocumentCompatibilityModule`

Interface：

```go
type PublishDocumentSource interface {
    Build(ctx context.Context, task TaskIdentity) (PublishDocument, error)
}
```

两个 Adapter：

- `V1WorkingDraftAdapter`：保持现有 latest Working Draft + legacy confirmation 行为；
- `V4LockedOutlineAdapter`：按 locked Outline ordinal 读取 latest confirmed Unit，验证 PASSED
  Full Review hash set，再确定性组装 Markdown/content hash。

`PublishDocument` 是短期发布 payload，不是新的 canonical Published PRD：

```go
type PublishDocument struct {
    WorkflowVersion WorkflowVersion
    SourceVersionID string
    Content []byte
    ContentHash string
    TaskVersion int
}
```

Publish Preview、confirmation token 和 Feishu publish worker 只消费统一的 immutable
`PublishDocument`。版本选择只存在于该 Module，避免泄漏到 Memory/PostgreSQL/HTTP/worker。

### 4.4 Go `WorkflowRolloutControlModule`

Phase 8 只实现 readiness，不执行真实切流：

```go
type WorkflowRolloutControl interface {
    AssignNewRun(ctx context.Context, request AssignmentRequest) (RolloutAssignment, error)
    RecordGateDecision(ctx context.Context, command RecordGateDecisionCommand) (GateDecisionRecord, error)
    BeginDrain(ctx context.Context, command BeginDrainCommand) (DrainRecord, error)
    RefreshDrain(ctx context.Context, drainID string) (DrainRecord, error)
    InspectLegacy(ctx context.Context) (LegacyInventory, error)
    AuthorizeTransition(ctx context.Context, command RolloutTransitionCommand) (RolloutStage, error)
}
```

Python Eval 只产生 machine report；Go 验证 report/manifest identity 并拥有 authoritative
decision。`LegacyInventory` 至少包含：

- active/waiting/stopping v1 Agent Run；
- active dispatch；
- unpublished Run outbox；
- non-terminal ledger entry；
- v1 checkpoint/snapshot；
- legacy producer symbol inventory version；
- v1 reader hit count 与观察窗口。

### 4.5 `ZeroRemoteEffectsShadowModule`

Phase 8 只连接本地执行链并证明零副作用：

- 输入仅为 authoritative trace/artifact hash；
- 注入 `NoRemoteEffectsAdapter`；
- Ledger 在 `SHADOW` mode 拒绝 MODEL/CAPABILITY reservation；
- 只允许写 `shadow-evaluation.v1` Run Artifact；
- 不创建第二 Agent Run、Queue Slot、Draft、Unit、Review 或 Publish；
- Go 只持久化 hash 和低基数 counters，不复制 Python evaluator Implementation。

---

## 5. 数据模型与迁移策略

### 5.1 不修改已编号 migration

`0014`、`0015` 可能已在本地或测试数据库应用。Phase 8 不回写旧 migration，新增：

```text
0016_review_workflow_orchestration.sql
0017_rollout_readiness.sql
```

### 5.2 `0016_review_workflow_orchestration.sql`

新增：

```text
go_prd_outlines
  outline_id PK, task_id FK, created_at

go_prd_outline_versions
  outline_version_id PK, outline_id FK, version, status,
  payload, content_hash, source_run_id, created_at, locked_at,
  UNIQUE(outline_id, version)

go_full_review_reports
  report_id PK, task_id FK, outline_version_id FK, source_run_id FK,
  payload, content_hash, disposition, unit_hash_set_hash, created_at

go_review_transition_idempotency
  tenant_id, owner_id, operation, idempotency_key,
  request_hash, transition_payload, created_at
```

扩展 `go_confirmation_unit_versions`：

- `draft_id` 允许 NULL，legacy 行保持原值；
- `outline_version_id` nullable FK；
- `source_run_id` nullable FK；
- `unit_version_no` nullable positive integer；
- v4 partial unique constraint：
  `(unit_id, outline_version_id, unit_version_no) WHERE outline_version_id IS NOT NULL`。

扩展 `go_run_outputs`：

- `materialized_kind`；
- `materialized_id`；
- `materialized_at`；
- 同 hash replay 返回原 materialization，不再次更新 Task/Outbox。

### 5.3 `0017_rollout_readiness.sql`

扩展/新增：

```text
go_rollout_stage_state
  singleton_key, stage, policy_version, last_gate_decision_id,
  evidence_hash, version, updated_at

go_legacy_inventory_snapshots
  inventory_id, workflow_version, counts_json, inventory_hash,
  observed_from, observed_until, created_at

go_legacy_reader_observations
  observation_window, reader_kind, hit_count, evidence_hash, created_at
```

GateDecision 必须绑定 candidate/baseline、dataset/config/report/manifest hash。Drain 必须绑定
inventory snapshot，不能只记录手工填写的 count。

### 5.4 Retention

- active/recovery 所需 Outline/Unit/Review payload 按现有 Working Draft retention 管理；
- Published PRD 成功后可保留 hash、identity、audit 和 locator，正文遵循 bounded retention；
- 不建立第二份无限期 Published PRD body，保持 ADR-0001。

---

## 6. 状态机与事务流程

### 6.1 Task review 状态

```text
DRAFT
  -> OUTLINE_REVIEW
  -> UNIT_GENERATING
  -> UNIT_REVIEW
  -> FULL_REVIEW_RUNNING
  -> FULL_REVIEW
  -> REVIEWABLE
  -> PUBLISHING
  -> PUBLISHED
```

旧 Task 状态继续可读。v4 只允许上述迁移；任何非法或跨步迁移返回稳定错误，不隐式修复。

### 6.2 接受 PLAN_OUTLINE output

单事务：

1. lease/fencing、Purpose、scope、Task version、output hash 校验；
2. 同 output key/hash replay 返回原 Outline Version；
3. 创建或追加 `DRAFT` Outline Version；
4. 标记 RunOutput materialization；
5. Task -> `OUTLINE_REVIEW`，Task version +1；
6. append task event；
7. 不创建下一 Agent Run，等待用户。

### 6.3 确认 Outline

单事务：

1. tenant/owner/idempotency/expected Task version；
2. 锁 Outline Version，并验证尚无其他 locked version；
3. 按 stable Unit key 创建 Confirmation Unit identities，不创建内容版本；
4. 选择 ordinal 最小的 dependency-ready Unit；
5. 创建一个 `GENERATE_UNIT` Agent Run 和冻结 Unit Scope；
6. 继承 Task rollout assignment、ledger/budget；
7. Run Admission、Queue Slot、Outbox、Task -> `UNIT_GENERATING` 原子提交。

并发确认最多一个事务成功；另一个得到 idempotent replay 或 Task version conflict。

### 6.4 接受 GENERATE_UNIT / REVISE_UNIT output

单事务：

1. 验证 current Unit、locked Outline、scope hash、base hash 和 immutable context；
2. 创建 current Confirmation Unit Version；
3. `REVISE_UNIT` 要求 prior version 为 REOPENED 且 version number 单调；
4. 保存 Quality/Grounding Artifact identity，不把 report 当 confirmed content；
5. Task -> `UNIT_REVIEW`，不创建下一 Run；
6. output/materialization/event 原子提交。

### 6.5 确认 Unit

单事务：

1. 确认 current latest Unit Version；
2. 若仍有 dependency-ready unconfirmed Unit，创建恰好一个 `GENERATE_UNIT` Run；
3. 若所有 Unit 已确认，创建恰好一个 `FULL_REVIEW` Run；
4. 若没有 ready Unit 但仍有 pending Unit，返回 dependency invariant failure；
5. Task projection、Run/Scope/Budget/Queue Slot/Outbox 原子提交。

### 6.6 Reopen Unit

- 默认只允许 confirmed leaf Unit；
- 非 leaf 必须由用户显式选择 dependent closure，不能静默 reopen；
- 创建 `REVISE_UNIT` Run，scope 包含 current base hash、confirmed dependency context、
  immutable hash 和用户反馈；
- existing v4 Run 不修改 workflow assignment；新 revision Run 继承 Task assignment；
- downstream Full Review Report 立即 stale，Publish Preview 被阻止。

### 6.7 Full Review 与发布

`FULL_REVIEW` scope 包含 locked Outline payload/hash和所有 confirmed latest Unit hash。接受
Report 时：

- 重新读取 durable hash set；
- 任一漂移则拒绝/标记 stale；
- `PASSED` -> Task `REVIEWABLE`；
- `NEEDS_REVISION` -> Task `FULL_REVIEW`，保存稳定 issue 和 affected Unit keys；
- 不自动修改或 reopen Unit。

Publish Preview 调用 `PublishDocumentCompatibilityModule`：

```text
v1 -> V1WorkingDraftAdapter
v4 -> V4LockedOutlineAdapter
        -> all latest versions CONFIRMED
        -> latest Full Review PASSED
        -> exact outline/unit hash set match
        -> deterministic Markdown + content hash
```

### 6.8 Crash/replay

- remote ACK 前后 crash 继续由 Ledger outcome Artifact 处理；
- RunOutput 已保存但未 materialize 的状态由同一事务设计消除；
- materialized output ACK 丢失时相同 key/hash 返回原 result；
- next Run/Outbox 使用 stable transition identity；
- CompleteRun ACK 丢失只重放 terminal completion，不重新 materialize；
- confirmed context、Outline 或 Task version 漂移 fail closed。

---

## 7. 合同与兼容策略

### 7.1 protobuf

优先不新增顶层 RPC。复用 `SubmitRunOutput`，由 Go `ReviewWorkflowModule` 接受并物化。
只有在 HTTP/Web 需要新投影时增加 expand-only message；不得复用或重编号已有字段。

### 7.2 JSON schemas

完善：

- `outline-candidate.v1`：稳定 key、parent、ordinal、required content、Unit dependency；
- `unit-candidate.v1`：current identity、claim/unknown/fact refs、Quality report identity；
- `unit-patch.v1`：base hash、replacement、preserved unknown、resolved issue；
- `full-review-report.v1`：outline hash、unit hashes、disposition、issues、
  `affected_unit_keys`；
- `publish-document.v1`：source workflow/version、Task version、content hash。

Go/Python 使用共享 fixtures 验证 canonical hash 和拒绝规则，避免重复 Implementation 漂移。

### 7.3 HTTP/Web

新增或扩展：

```text
GET  /tasks/:task_id/outline
POST /tasks/:task_id/outline/confirm
GET  /tasks/:task_id/confirmation-units
POST /tasks/:task_id/confirmation-units/:version_id/confirm
POST /tasks/:task_id/confirmation-units/:version_id/reopen
GET  /tasks/:task_id/full-review
```

所有 mutation 需要 idempotency key 和 expected Task version。响应使用 Review projection，不暴露
内部 prompt、hidden reasoning、secret 或未授权 Evidence excerpt。

---

## 8. 测试设计

### 8.1 Go Interface contract suite

同一套 contract tests 分别运行 Memory/PostgreSQL Adapter：

- PLAN_OUTLINE output 只创建 DRAFT Outline；
- Outline confirm idempotent，锁定后不可调整；
- 并发 confirm 只产生一个下一 Run/Outbox；
- dependency DAG 稳定选择最小 ordinal ready Unit；
- current Unit 未确认不创建下一 Run；
- GENERATE 只能物化 current Unit；
- REVISE stale base/immutable drift 被拒绝；
- 最后 Unit 确认创建唯一 FULL_REVIEW；
- Full Review 不写 Unit Version；
- report hash set 漂移阻止 Publish；
- v4 publish aggregation 顺序/hash 稳定；
- v1 reader fixture 不变；
- duplicate key 同 hash replay、异 hash拒绝。

### 8.2 Python execution tests

- 四种 Purpose 通过同一 `run(request)` Interface；
- explicit Requirement Brief/Locked Outline，不再使用 ref 前缀；
- GENERATE 执行 Need -> Investigation -> Knowledge -> Grounding -> Quality；
- REVISE repair 后 claim generation 增加；
- re-Grounding/re-Quality Artifact generation 增加；
- unresolved Unknown 不可被 repair 删除；
- model classifier 只通过 Ledger Adapter；
- Full Review 不调用 writer/repair；
- Ledger replay 不重复远程物理调用；
- snapshot purpose/scope/hash 漂移 fail closed。

### 8.3 Publish compatibility tests

- v1/v4 Adapter 输出统一 `PublishDocument`；
- v4 不读取 latest Working Draft；
- locked Outline title/ordinal 决定稳定顺序；
- missing/unconfirmed/reopened Unit 拒绝；
- stale/missing/blocking Full Review 拒绝；
- preview content hash 与 publish worker payload 相同；
- publish success 后数据库不成为 canonical Published PRD owner。

### 8.4 Rollout readiness tests

- GateDecision 绑定 manifest/report/config/dataset hash；
- missing metric、NaN、blocking case、低重复次数 fail closed；
- 未通过 Gate 不允许 stage transition；
- inventory 任一 v1 count 非零时 Drain 不完成；
- reader hit 非零时不允许 reader removal；
- policy change 只影响新 Run；
- rollback 不修改已有 Agent Run workflow；
- Shadow remote reservation 立即失败；
- Shadow 不产生 Run/Queue Slot/Outbox/Unit/Publish。

### 8.5 性质与 crash matrix

| 性质 | 生成方式 | 断言 |
| --- | --- | --- |
| Outline DAG | 随机 DAG/环/悬空 parent | 只接受合法图 |
| next Run | 随机 confirmed status | 最多一个且 dependency-ready |
| immutable | 随机 confirmed hash set | 非 reopen hash 不变 |
| concurrency | 双 confirm/双 output | 一个 commit，一个 replay/conflict |
| crash | 每个 durable write 后注入 | 无重复 Run/Outbox/remote call |
| publish | 随机 Unit 顺序/hash | canonical output 稳定 |
| drain | 随机 inventory counts | 全零才完成 |

---

## 9. 实施顺序与最小提交

### Step 8.0：Residual characterization

- 固定当前 RunOutput 只持久化、不 materialize 的行为；
- 固定 v4 Publish Preview 仍读取 Working Draft 的失败测试；
- 固定 Repair 未 re-Ground 的失败测试；
- 固定 Gate/Drain table 无 authoritative transition 的现状。

提交：`test: characterize incomplete v4 review orchestration`

### Step 8.1：Schema expand 与 projection types

- 新增 migration 0016/0017；
- 增加 Outline Version、Full Review、materialization identity；
- 增加 ReviewTaskStatus/ReviewTransition/PublishDocument；
- 保留 v1 schema/readers。

提交：`feat: expand durable review workflow state`

### Step 8.2：Memory Review Workflow Adapter

- 实现 `ReviewWorkflowModule`；
- 内存 Adapter 完成 Outline -> Unit chain -> Full Review；
- Run Admission、assignment、budget、outbox 统一 helper 收入 Implementation；
- 增加完整状态机/性质测试。

提交：`feat: orchestrate reviewable units in memory control plane`

### Step 8.3：PostgreSQL transactional Adapter

- 实现相同 Interface；
- `SELECT ... FOR UPDATE` + expected Task version；
- materialization/Task/next Run/Outbox 同事务；
- 并发和 crash integration tests。

提交：`feat: persist transactional review workflow`

### Step 8.4：Deepen Python execution

- 增加 explicit Requirement Brief/Locked Outline 类型；
- 吸收浅 `ReviewableUnitRuntime`；
- 接入 Phase 3～5 主链；
- Repair 后 re-extract/re-Ground/re-Quality；
- Full Review 增加 affected Unit keys。

提交：`feat: execute grounded reviewable unit pipeline`

### Step 8.5：Publish compatibility

- 建立 v1/v4 两个 Adapter；
- v4 组装 confirmed Unit；
- Full Review exact hash Gate；
- HTTP/Web projection 支持 Outline/Unit/Review。

提交：`feat: publish locked confirmed unit documents`

### Step 8.6：Rollout readiness control

- GateDecision persistence；
- rollout stage optimistic transition；
- legacy inventory 与 Drain refresh；
- maintenance dry-run/rehearsal command；
- 不启用 production canary。

提交：`feat: persist rollout readiness and drain evidence`

### Step 8.7：Zero-remote Shadow integration

- Ledger evaluation mode deny；
- authoritative trace -> deterministic shadow artifact；
- extra remote/write counters hard Gate；
- crash/replay 与低基数 metrics。

提交：`feat: connect zero effect rollout shadow`

### Step 8.8：Verification record

- 全量本地、PostgreSQL、Web、contract Gate；
- migration from existing schema；
- 更新 Phase 6/7/8 状态；
- 生成 Phase 9 staging/canary runbook 和 evidence template。

提交：`docs: record phase 8 orchestration verification`

---

## 10. 验证命令与 Gate

本地：

```bash
PYTHONPATH=agent-python venv/bin/python -m pytest -q agent-python/tests
PYTHONPATH=agent-python venv/bin/python -m pytest -q tests
cd backend-go && go test ./...
cd backend-go && go vet ./...
cd contracts && buf lint && buf generate
npm --prefix web test -- --run
npm --prefix web run typecheck
git diff --check
```

PostgreSQL：

```bash
cd backend-go && \
  PRD_AGENT_TEST_DATABASE_DSN="$PRD_AGENT_DATABASE_DSN" go test ./internal/storage ./internal/runcontrol ./internal/httpapi -count=1
```

Phase 8 Gate：

- Memory/PostgreSQL Adapter contract suite 同结果；
- v4 end-to-end 从 Task creation 到 Publish Preview；
- 并发/幂等/crash matrix 通过；
- Repair re-Grounding 有 Artifact/Ledger 证据；
- v1 reader/publish 回归通过；
- Full Review stale hash 不能发布；
- rollout transition 无 Gate/Inventory 证据时 fail closed；
- Shadow extra model/capability/write 为 0；
- 无真实 staging 证据时文档不得标为 rollout ready。

---

## 11. 风险与回滚

| 风险 | 控制 | 回滚 |
| --- | --- | --- |
| 状态机一次扩展过大 | Interface contract + tracer bullets | 停止创建新 v4，保留 v1 reader |
| migration 破坏 legacy draft FK | 新 migration + nullable expand | 不执行 contract migration |
| 并发确认重复 Run | Task row lock + stable transition key | replay/冲突，不补偿双 Run |
| Unit 链死锁 | DAG validation + explicit invariant failure | 保留 Task 状态供人工修复 |
| Repair 重复远程调用 | Ledger outcome Artifact | OUTCOME_UNKNOWN/人工处理 |
| v4 publish 内容成为第二 canonical | bounded retention + hash/audit only | Feishu 仍为唯一 Published PRD |
| v1/v4 reader 条件散落 | compatibility Module + 两 Adapter | 关闭 v4 assignment |
| Gate evaluator 漂移 | shared fixtures，Go authoritative decision | 拒绝 stage transition |
| Drain 漏计数 | PostgreSQL inventory，不依赖 Redis | 保持当前 rollout stage |
| Shadow 产生额外副作用 | Adapter deny + Ledger deny + counters | 立即关闭 Shadow |

回滚只影响后续新 Agent Run。已有 v4 Run 必须由 v4 Worker drain/stop；不能改写其
workflow version，也不能交给 v1 Worker 恢复。

---

## 12. Phase 9 边界

Phase 8 本地原语完成后，Phase 9 先补 operational closure：

```text
real PostgreSQL proof
  -> production Unit semantic execution closure
  -> Review projection + Web
  -> durable rollout policy drives assignment
  -> authoritative trace Shadow integration
  -> STAGING READY record
```

Phase 10 才执行 remote Eval、staging、rollback rehearsal、production canary/default；Phase 11
才执行 drain 和 legacy retirement。详细顺序见
[剩余实现设计](./2026-08-01-agent-loop-remaining-implementation-design.md)。任何环境动作必须消费
hash-bound GateDecision、inventory、Drain 和 publish end-to-end 证据，不得用手工说明替代
durable record。

---

## 13. 完成清单

- [x] Residual characterization tests 完成
- [x] migration 0016/0017 expand-only 文件与 schema-only upgrade 验证完成（legacy-row fixture 待补）
- [x] Go ReviewWorkflowModule + Memory Adapter 完成
- [ ] PostgreSQL transactional Adapter 与并发测试完成（Implementation 已有，v4 行为证明缺失）
- [ ] Python ReviewableUnitExecutionModule 深化完成（typed request 已有，production Grounding Seam 未闭合）
- [ ] Repair re-extract/re-Ground/re-Quality 完成
- [x] Full Review affected Unit/hash set Gate 完成本地实现
- [x] v1/v4 PublishDocument Adapter 完成本地实现
- [ ] HTTP/Web Outline/Unit/Review projection 完成
- [x] GateDecision/rollout stage/legacy inventory/Drain persistence Implementation 完成
- [ ] Zero-remote-effects Shadow 接入真实执行链
- [x] v1 compatibility regression 完成本地验证
- [ ] 本地、PostgreSQL、Web、contract Gate 全通过
- [ ] Phase 6/7 状态回填，Phase 9 runbook/evidence template 完成
- [ ] 设计状态更新为 `IMPLEMENTED / LOCALLY VERIFIED`

剩余实现已转入：
[Agent Loop 剩余实现设计](./2026-08-01-agent-loop-remaining-implementation-design.md)。

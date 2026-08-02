# Agent Loop Phase 7：Eval、Shadow、Enforce 与 Legacy Removal 设计及实施 Plan

> 状态：IMPLEMENTATION IN PROGRESS / LOCAL CONTROL-PLANE FOUNDATION
> 日期：2026-08-01
> 前置阶段：Phase 6 Reviewable Unit + Quality 全部本地/PostgreSQL Gate 通过
> 对应 Phase 6：[`2026-08-01-agent-loop-phase-6-reviewable-unit-quality-design.md`](2026-08-01-agent-loop-phase-6-reviewable-unit-quality-design.md)
> 对应总设计：[`2026-07-31-agent-loop-semantic-deepening-design.md`](../specs/2026-07-31-agent-loop-semantic-deepening-design.md)
> 对应总计划：[`2026-07-31-agent-loop-semantic-deepening-implementation-plan.md`](2026-07-31-agent-loop-semantic-deepening-implementation-plan.md)
> 目标：用固定 Eval、零副作用 Shadow、staging/canary 和 drain 证据决定默认切换；完成后删除 legacy producer，再延迟删除 reader

---

## 0. 执行结论

Phase 7 不是继续增加 Agent 能力，而是将 Phase 0～6 的能力变成可测量、可灰度、可回滚
的默认执行路径。发布顺序固定为：

```text
fixed offline eval
  -> deterministic shadow (zero extra remote calls)
  -> staging selected Task enforce
  -> production canary
  -> production default v4
  -> drain v1 active Run
  -> delete legacy producer
  -> retain reader for one release window
  -> delete expired reader/config
```

核心设计决策：

1. workflow version 表示 authoritative producer；Shadow 不改变 authoritative output；
2. rollout assignment 由 Go 在 Agent Run 创建时解析并持久化，Run 中途不可改变；
3. production Shadow 只对已有 trace/artifact 执行确定性离线比较，额外远程模型和
   Capability 物理调用必须为 0；
4. 需要真实远程模型的 v4 质量对比属于显式 Eval/Enforce，不冒充 Shadow；
5. 回滚只停止创建新 v4 Run，不把 v4 snapshot 路由给 v1 Worker；
6. legacy producer 与 reader 分两次删除，删除测试必须证明复杂度不会重新散落。

---

## 1. 当前基础与缺口

### 1.1 已有基础

- Go 已持久化 `workflow_version`，支持 v1/v4 worker routing；
- Worker health 暴露支持的 workflow、snapshot 和 ledger version；
- Agent Pool 已具备 drain；
- Phase 2 已建立共享 Ledger、物理调用与 durable replay 计数；
- Eval trace 已能记录 Need、Knowledge、Grounding、预算和 shadow delta；
- `PRD_AGENT_ADVANCED_LOOP_MODE=off|shadow|enforce` 已存在；
- HTTP metrics 已有低基数 route/method/status 指标。

### 1.2 必须补齐

- 当前 mode 是 Worker 进程级环境变量，不是每个 Agent Run 的 durable assignment；
- default workflow version 只有全局配置，缺少稳定 cohort 和 policy version；
- Shadow 的“候选语义”与 authoritative workflow 没有独立、可审计身份；
- Eval 没有完整 v1/v4 ablation matrix、重复运行统计和签名 Gate；
- 没有自动 canary gate evaluator、暂停/回滚状态和发布证据清单；
- legacy producer/reader 没有库存、引用计数、drain query 和删除顺序；
- 本地测试通过不能证明远程模型质量、24 小时内存稳定或生产回滚可行。

### 1.3 不采用的方向

- 不在 production Shadow 中再跑一次远程 v4 Agent Loop；
- 不用随机数在每次重试时重新决定 cohort；
- 不通过部署 v1 Worker 读取 v4 snapshot 实现“快速回滚”；
- 不在有 active v1 Run、legacy checkpoint 或 rollback window 时删除 reader；
- 不用高基数 Task/User/Run label 暴露 Prometheus 指标；
- 不以平均分掩盖 blocking case、方差、P95 或失败轨迹；
- 不让 Python Worker 自行决定 rollout mode。

---

## 2. 目标、非目标与完成定义

### 2.1 目标

- 固定 v1、v4-a、v4-b、v4-c 的 Eval Matrix，每个 case 至少重复 3 次；
- 报告均值、方差、P50/P95、hard failure 和失败 trace；
- Go 持久化 `RolloutAssignment`，同一个 Run 恢复时身份不变；
- Shadow 额外 model/capability physical call 为 0；
- staging 和 production canary 有显式 cohort、暂停、扩大和 rollback 控制；
- quality/cost/latency/recovery/memory Gate 由机器可读 manifest 判断；
- 默认 v4 后 drain 所有 v1 active Run 和 snapshot；
- 删除 v1 whole-draft producer，但保留 reader 一个发布周期；
- reader 删除前有零读取证据、备份/retention 证据和 rollback sign-off；
- 最终删除进程级 `PRD_AGENT_ADVANCED_LOOP_MODE` 及过期配置。

### 2.2 非目标

- 不在本阶段改变 Need、Knowledge、Grounding、Investigation、Unit 或 Quality 算法；
- 不以扩大 Token/Tool budget 换取 Eval 分数；
- 不改变 ADR-0003 的单 Worker 串行执行；
- 不引入第三套 workflow producer；
- 不自动删除历史 Eval 报告、审计事件或 Feishu Published PRD；
- 不承诺没有真实 staging/production 证据的“生产已完成”。

### 2.3 完成定义

- 所有 Eval case 的数据集、config、模型/prompt/version、运行次数和 report hash 可追溯；
- v4-c 满足机器 Gate，且无 blocking case regression；
- production Shadow 连续观察窗口内额外远程副作用为 0；
- staging selected Task enforce、production canary、drain 和 rollback 演练都有脱敏证据；
- 默认创建的 eligible Agent Run 使用 v4，assignment 在恢复/重试中不漂移；
- v1 active Run/snapshot/outbox/dispatch 计数归零后才删除 producer；
- reader 观测一个发布周期为零后才删除；
- 删除后所有 v4 tests、publish/reopen/history reader 和 rollback runbook 通过。

---

## 3. Rollout Module 与持久化 Assignment

### 3.1 Go `WorkflowRolloutModule`

建议目标文件：

```text
backend-go/internal/runcontrol/rollout.go
backend-go/internal/storage/rollout.go
backend-go/internal/config/config.go
backend-go/internal/httpapi/router.go
backend-go/cmd/maintenance/main.go
```

外部 Interface：

```go
type WorkflowRollout interface {
    Assign(ctx context.Context, request AssignmentRequest) (RolloutAssignment, error)
    EvaluateGate(ctx context.Context, request GateEvaluationRequest) (GateDecision, error)
    BeginDrain(ctx context.Context, command DrainCommand) (DrainStatus, error)
    InspectLegacy(ctx context.Context) (LegacyInventory, error)
}
```

Interface 隐藏 cohort hashing、allow/deny list、policy version、freeze、metrics query、drain
条件和 rollback switch，形成一个实际 Seam。HTTP、Task creation 和 maintenance command 不
重复实现 rollout 条件。

### 3.2 `RolloutAssignment`

```text
authoritative_workflow_version  agent-runtime.v1 | agent-runtime.v4
evaluation_mode                 OFF | SHADOW | ENFORCE
shadow_workflow_version         empty | agent-runtime.v4
cohort                          CONTROL | INTERNAL | CANARY | DEFAULT
policy_version                  rollout-policy.v1:<hash>
assignment_reason               explicit | allowlist | percentage | default | rollback
assigned_at
```

语义：

- `OFF`：只执行 authoritative workflow；
- `SHADOW`：authoritative workflow 仍为 v1，只对其已有输入/trace/artifact 运行 v4
  deterministic evaluator；
- `ENFORCE`：authoritative workflow 可为 v4，输出参与用户工作流；
- `shadow_workflow_version` 不创建第二个 Agent Run，不获得第二份 Run Budget；
- assignment 在创建 Task/Run 的事务中持久化，重试、reopen 和 resume 读取同一结果；
- 后续 Unit Run 默认继承 Task 的 authoritative assignment，除非显式 rollback policy 要求
  对“新 Run”切换；已有 Run 永不原地改版本。

### 3.3 PostgreSQL expand

新增迁移建议 `0015_agent_rollout_assignment.sql`：

```text
go_agent_rollout_assignments
  run_id PK/FK, authoritative_workflow_version, evaluation_mode,
  shadow_workflow_version, cohort, policy_version, assignment_reason, created_at

go_rollout_gate_decisions
  decision_id, candidate_version, baseline_version, report_hash,
  gate_manifest_hash, decision, failed_gate_codes, created_at

go_rollout_drains
  drain_id, workflow_version, status, started_at, completed_at,
  active_run_count, active_dispatch_count, snapshot_count, evidence_json
```

高维 report/trace 不复制进数据库；只保存 hash、状态、低维摘要和受控 artifact locator。

### 3.4 稳定 cohort

percentage assignment 使用：

```text
bucket = sha256(tenant_id + owner_id + task_id + policy_version) mod 10000
selected = bucket < canary_basis_points
```

优先级固定：emergency deny > explicit task override > tenant/user allowlist > percentage >
default。命中原因持久化；policy 变化只影响后续新 Run。

---

## 4. Shadow：零额外远程副作用

### 4.1 可执行内容

Shadow 只允许：

- 从 authoritative v1 trace 推导 Need/coverage 分类的确定性比较；
- 对已有 Evidence/Artifact 执行 schema、identity、hash、invariant 检查；
- 对已有 Draft 执行无远程调用的 deterministic Quality Policy；
- 计算 v1/v4 trace schema 差异和预计 route；
- 输出 `shadow-evaluation.v1` Artifact 与低基数计数。

Shadow 禁止：

- 额外模型调用；
- 额外 GitHub/Feishu/Repository Capability；
- 额外 Supplement、Repair 或 Unit generation；
- 写 Working Draft、Confirmation、Review 或 Publish 状态；
- 创建第二个 Queue Slot 或 Agent Run。

### 4.2 强制实现

不要仅依靠调用约定。为 shadow evaluator 注入 `NoRemoteEffectsAdapter`：任何 model 或
Capability Interface 调用立即抛出 `SHADOW_REMOTE_EFFECT_FORBIDDEN`。Ledger 在 evaluation
mode=SHADOW 时也拒绝 `MODEL/CAPABILITY` reservation，只允许 local transition。

`shadow-evaluation.v1` 至少记录：

```text
authoritative_trace_hash
candidate_policy_version
deterministic_findings
projected_need_requiredness
projected_quality_codes
extra_model_physical_calls = 0
extra_capability_physical_calls = 0
evaluator_elapsed_ms
```

### 4.3 Shadow 与 Eval 的区别

显式 Eval 可以在隔离环境中对固定 case 运行远程 v4，并计入独立预算；它必须标记
`measurement_mode=REMOTE_EVAL`。production Shadow 不运行完整 candidate producer，不能
用 Shadow 指标宣称模型生成质量提升。

---

## 5. Eval Matrix 与机器 Gate

### 5.1 固定 Variant

| Variant | Need | Knowledge/Grounding | Investigation | Unit | 作用 |
| --- | --- | --- | --- | --- | --- |
| `v1-current` | legacy | Evidence/ref | legacy | whole draft | baseline |
| `v4-a-need` | v4 | Evidence/ref | unified off | eval-only whole-draft Adapter | Need ablation |
| `v4-b-grounding` | v4 | Fact/Grounding | unified | eval-only whole-draft Adapter | knowledge ablation |
| `v4-c-unit` | v4 | Fact/Grounding | unified | current Unit + Quality | candidate |

所有 Variant 使用同一 case manifest、来源 fixture/revision、模型参数和硬预算。每个 case
至少运行 3 次；若 provider 不支持固定 seed，报告必须展示方差而不是伪装确定性。

### 5.2 数据集层次

- deterministic unit fixtures：schema、policy、crash、replay；
- golden product cases：纯新增、存量字段、状态、权限、历史 PRD、冲突、EMPTY；
- adversarial cases：错误引用、过期 revision、敏感内容、模糊验收标准；
- recovery matrix：每个 durable ACK 后 crash；
- staging sampled cases：只保存脱敏 input hash、标签和人工 rubric，不提交用户正文。

### 5.3 指标

质量：

- Information Need Precision / Recall；
- Coverage Completion；
- Verified Fact Accuracy；
- Evidence Precision；
- Unsupported Claim Rate，单列 `CURRENT_STATE`；
- Critical Unknown Recall；
- Source Conflict Detection；
- Acceptance Criteria Executability；
- Outline/Unit Scope Violation；
- Full Review blocking issue precision。

成本与可靠性：

- model/tool physical calls 与 durable replay；
- input/output Token；
- P50/P95 latency；
- Stop Reason 分布；
- retry/crash 后重复物理调用；
- RSS peak、24 小时 slope、queue wait、single-worker utilization；
- per Purpose success/failure/needs-human。

### 5.4 Gate manifest

新增机器可读：

```text
eval/gates/agent-runtime-v4.schema.json
eval/gates/agent-runtime-v4.json
```

manifest 必含：dataset hash、baseline/candidate config hash、最小重复次数、每项 absolute/
relative threshold、blocking case policy、P95 hard ceiling、budget ceiling、memory ceiling、
有效期和 approver。初始阈值必须由 Phase 0 baseline 与产品 owner 确认后提交，不能在代码
中临时放宽。

不可协商 hard gate：

- blocking `CURRENT_STATE` unsupported claim 为 0；
- shadow extra model/capability physical calls 均为 0；
- recovery duplicate physical calls 为 0；
- 所有 Run 不超过硬预算；
- Outline/immutable/current-only violation 为 0；
- 没有数据集或配置 hash 漂移；
- 24 小时测试没有无界内存增长；
- drain/rollback 演练成功。

### 5.5 统计与签名

- 报告均值、标准差、P50/P95 和样本数；
- blocking case 逐例判断，不因平均分通过而忽略；
- report JSON canonicalize 后计算 SHA-256；
- Gate decision 绑定 report hash + manifest hash；
- 只提交脱敏 summary、hash、失败分类；真实正文、secret、Evidence excerpt 不入仓库。

---

## 6. 指标与可观测性

### 6.1 低基数指标

建议：

```text
prd_agent_runs_total{workflow,purpose,mode,outcome}
prd_agent_run_duration_seconds{workflow,purpose}
prd_agent_budget_consumed_total{workflow,purpose,budget_kind}
prd_agent_physical_calls_total{workflow,call_kind,outcome}
prd_agent_durable_replays_total{workflow,call_kind}
prd_agent_quality_issues_total{workflow,code,severity}
prd_agent_shadow_extra_calls_total{call_kind}
prd_agent_rollout_assignment_total{cohort,workflow,reason}
prd_agent_legacy_active_runs{workflow}
prd_agent_legacy_reader_total{reader_kind}
```

禁止将 tenant、owner、task、run、unit、issue ID 放入 metrics label。详细诊断进入受访问
控制的 event/trace，以 ID 或 hash 关联。

### 6.2 告警

- `shadow_extra_calls_total > 0`：立即停止 Shadow；
- v4 unsupported current-state > hard gate：冻结 canary；
- P95/queue wait/RSS 超阈值：停止扩大；
- v4 retry/unknown outcome 上升：停止新 v4 assignment；
- worker supported version 不满足 queued Run：部署阻断；
- legacy reader 在 retention 窗口仍增长：禁止删除 reader。

---

## 7. 灰度、回滚与 Drain

### 7.1 阶段顺序

| Stage | authoritative | mode | cohort | 退出条件 |
| --- | --- | --- | --- | --- |
| 7.0 readiness | v1 | OFF | control | Phase 6 + fixed Eval ready |
| 7.1 shadow | v1 | SHADOW | staging/internal | 额外调用 0，deterministic diff 可解释 |
| 7.2 staging enforce | v4 | ENFORCE | explicit task/user | staging gate + rollback rehearsal |
| 7.3 production canary | v4 | ENFORCE | small basis points | canary window 全 Gate |
| 7.4 expand | v4 | ENFORCE | staged percentages | 每档观察窗通过 |
| 7.5 default | v4 | ENFORCE | eligible default | v1 new producer 关闭 |
| 7.6 drain | mixed active | inherited | existing only | v1 active state 归零 |
| 7.7 remove producer | v4 | ENFORCE | default | reader compatibility regression |
| 7.8 remove reader | v4 | ENFORCE | default | 一个发布周期零读取 |

每次扩大必须产生新的 Gate Decision；不能自动跨过多个 percentage 档位。

### 7.2 回滚 Runbook

发现 Gate 失败时：

1. 将 rollout policy 设为 `new assignments -> agent-runtime.v1`；
2. 保留 v4 Worker 支持，继续处理已创建 v4 Run；
3. 暂停新的 v4 Unit chain；若必须停止，使用正常 STOPPING/STOPPED 状态和审计事件；
4. 不把 v4 snapshot/Artifact 交给 v1 Worker；
5. 对 outcome unknown 的外部调用执行 reconcile；
6. 确认 confirmed Unit/Published PRD 未回退或被覆盖；
7. 记录 failed gate、时间窗、版本、active counts 和恢复结果；
8. 修复后从较小 cohort 重新开始，不直接恢复 default。

如果默认 v4 后 legacy producer 已删除，回滚只可重新部署支持窗口内已签名、已验证且固定
digest 的旧发布镜像/代码版本，不能依赖运行时拼回已删除代码。producer 删除前必须完成
rollback window sign-off。

### 7.3 Drain 条件

`agent-runtime.v1` drain 完成必须同时满足：

- 非 terminal v1 Agent Run = 0；
- queued/waiting/running/stopping v1 dispatch = 0；
- 未 ACK v1 Outbox = 0；
- v1 outcome unknown Ledger = 0；
- v1 active Working Draft/Revision recovery = 0；
- worker pool 中不再需要仅 v1 的 slot；
- retention window 内没有恢复 v1 snapshot 的请求。

查询结果及时间戳写入 `LegacyInventory` 和 drain evidence；人工目测不是删除依据。

---

## 8. Legacy 库存与删除测试

### 8.1 legacy producer

Phase 6 完成后应核对并删除或收缩：

```text
agent-python/agent/graph/advanced.py
  whole DraftBundle -> all Confirmation Units producer

agent-python/agent/draft/models.py
  Markdown auto-split used by v1 producer

agent-python/agent/confirmation/builder.py
  all-unit working-draft.v2 builder

agent-python/agent/graph/runtime.py
  v1 direct supplement/ref-presence/whole-draft routing

backend-go/internal/runcontrol/confirmation.go
  working-draft.v2 ParseConfirmationCandidates producer path

backend-go/internal/runcontrol/* / storage/*
  v1 default run creation and producer-only branches
```

先用 `rg` 和 runtime counters 生成精确引用清单；不能仅按文件名删除，因为部分文件还可能
承担 reader 或历史 reopen 兼容。

### 8.2 legacy reader

至少保留一个发布周期：

- v1 Working Draft/Confirmation list reader；
- v1 snapshot/Artifact diagnostic reader；
- 历史 Task/Run projection；
- 必要的数据导出和审计读取。

reader 禁止创建新 v1 Run、Draft 或 Unit Version。reader 命中应增加低基数 counter。

### 8.3 删除测试

对每个候选 Module 应用删除测试：

- 删除 pass-through 后复杂度应真正消失，而不是复制到多个 caller；
- 删除 legacy Adapter 后，v4 caller 不应新增 `if workflow == v1`；
- 若删除导致 schema/version/reader 条件散落，则先深化 compatibility Module 再删除；
- 测试只能跨公开 Interface，不依赖已删除 Implementation 的内部函数；
- `rg` 验证旧 producer symbol、config、Artifact type 和 output schema 不再被写入；
- historical fixture 仍可由 reader 读取，直到 reader removal Gate。

### 8.4 删除顺序

1. 停止新 v1 assignment；
2. drain v1；
3. 删除 Python v1 producer；
4. 删除 Go v1 producer branch 和 worker advertisement；
5. 保留 v1 reader + fixtures + metrics；
6. 观察一个发布周期；
7. 备份/retention/sign-off；
8. 删除 reader、旧 proto 字段消费、环境变量和 fixtures；
9. 新 ADR 或 release note 记录 compatibility contract 已结束。

Proto 字段删除仍遵守 protobuf 保留规则：删除字段后使用 `reserved` 保留字段号和名称。

---

## 9. 实施顺序与最小提交

### Step 7.0：Readiness inventory

- 固定 Phase 6 verification record；
- 输出 legacy producer/reader 引用清单；
- 建立 rollout assignment/gate/drain schema；
- 增加 low-cardinality metrics。

提交：`feat: persist agent workflow rollout assignments`

### Step 7.1：Eval matrix

- 增加四个 Variant config 和重复运行；
- 完成统计、report hash、gate schema/validator；
- 增加 blocking-case 判定。

提交：`eval: add agent runtime v4 ablation and gate matrix`

### Step 7.2：Deterministic Shadow

- 增加 `NoRemoteEffectsAdapter`；
- v1 authoritative trace -> deterministic shadow artifact；
- extra call hard gate 和告警；
- 删除 Shadow 中可能执行 candidate remote path 的代码。

提交：`feat: enforce zero side effect workflow shadow`

### Step 7.3：Staging/Canary controls

- allowlist/basis-points assignment；
- policy version 与 freeze；
- pause/expand/rollback/drain maintenance command；
- runbook 和脱敏 evidence template。

提交：`ops: add v4 canary rollback and drain controls`

### Step 7.4：Default v4

- Gate decision 通过后切 eligible default；
- 保留 explicit emergency rollback；
- 观察至少一个配置的发布窗口；
- 确认新 Task/Unit chain assignment 正确。

提交：`ops: make agent runtime v4 the gated default`

### Step 7.5：删除 producer

- drain evidence 归零；
- 删除 whole-draft/ref-presence/direct-supplement producer；
- 保留 v1 reader Adapter 和 fixtures；
- 移除 producer-only tests，替换为 v4 Interface tests。

提交：`refactor: remove legacy agent loop producer`

### Step 7.6：删除 reader

- 一个发布周期 reader counter 为 0；
- 完成 retention/export/sign-off；
- 删除 reader/config/过期 snapshot support；
- proto 字段号/name reserved；
- 更新 ADR/CONTEXT/运维文档。

提交：`refactor: remove expired agent loop compatibility readers`

---

## 10. 测试方案

### 10.1 Assignment/配置

- 同一 Task/Run 在 retry/resume/reopen 时 assignment 稳定；
- policy version 改变只影响新 Run；
- explicit deny/allow/percentage/default 优先级固定；
- bucket 计算跨 Go 进程稳定；
- 不受 map iteration、时钟或随机 seed 影响；
- unsupported worker version 队列不被错误派发。

### 10.2 Shadow

- 注入会计数的 model/capability Adapter，Shadow 调用数均为 0；
- Ledger 拒绝 Shadow remote reservation；
- Shadow 不写 Draft/Unit/Review/Publish；
- Shadow 不创建 Queue Slot、Run 或 Outbox；
- evaluator crash/replay 不改变 authoritative output；
- trace/artifact hash 漂移 fail closed；
- `shadow_extra_*` 非零时 Gate 必定失败。

### 10.3 Eval/Gate

- 每个 case 少于 3 次拒绝出 Gate Decision；
- dataset/config/report hash 漂移拒绝；
- 均值通过但 blocking case 失败时整体失败；
- P95、预算、memory 任一 hard ceiling 超限时失败；
- missing metric、NaN、空样本 fail closed；
- report canonicalization/hash 稳定；
- secret/excerpt redaction 测试通过。

### 10.4 Canary/回滚

- cohort basis points 分布与固定样本稳定；
- pause 后不再创建新 v4 assignment；
- 已有 v4 Run 继续路由 v4 Worker；
- v4 snapshot 永不路由 v1 Worker；
- rollback 不修改已确认 Unit 或 Published PRD；
- outcome unknown 进入 reconcile；
- 每档扩大需要新 Gate Decision。

### 10.5 Drain/删除

- 任一 v1 active Run/dispatch/outbox/ledger/snapshot 非零时拒绝 producer removal；
- producer removal 后 historical v1 reader fixture 仍可读；
- reader hit counter 增长时拒绝 reader removal；
- reader removal 后旧 proto field reserved；
- `rg`/静态检查无 v1 producer symbol 和 `ADVANCED_LOOP_MODE`；
- clean install/migration 与从现有数据库升级都通过；
- publish/reopen/history projection 回归通过。

### 10.6 运行稳定性

- staging 单 Worker 24 小时串行 case；
- 记录 RSS peak/slope、queue wait、P95、provider failure、replay；
- Redis restart 不丢 admitted Run；
- Worker restart 不重复物理调用；
- PostgreSQL transient failure 不产生部分 output/next Run；
- deployment drain timeout 有明确失败状态，不能假装成功。

---

## 11. 验证命令与证据

本地 Gate：

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

Eval 示例：

```bash
PYTHONPATH=src venv/bin/python -m prd_agent.eval run-matrix \
  --manifest eval/cases/manifest.json \
  --matrix eval/configs/agent-runtime-v4-matrix.json \
  --repetitions 3 \
  --output-dir eval/reports

PYTHONPATH=src venv/bin/python -m prd_agent.eval evaluate-gate \
  --report eval/reports/<report>.json \
  --gate eval/gates/agent-runtime-v4.json
```

staging/production 证据至少包含：

- deployment/config hash；
- rollout policy version；
- worker supported version inventory；
- cohort、观察窗口和样本数；
- Gate report/manifest hash；
- shadow extra call counters；
- canary quality/cost/P95/memory summary；
- rollback/drain timestamps 与 counts；
- approver 和结论。

Secret、原始用户正文、Prompt、Evidence excerpt 和可识别用户 ID 不进入仓库。

---

## 12. Enforce Gate

production 默认 v4 前必须同时满足：

- Phase 6 全部本地、PostgreSQL、web、contract Gate；
- remote Eval v4-c 通过已批准的机器 manifest；
- Information Need Precision/Recall 达标；
- Verified Fact Accuracy/Evidence Precision 不低于 baseline gate；
- blocking unsupported CURRENT_STATE 为 0；
- Critical Unknown Recall 不下降；
- Source Conflict Detection 与 Acceptance Criteria Executability 达标；
- hard budget 和 P95 ceiling 未超限；
- crash matrix 重复物理调用为 0；
- production Shadow 额外远程调用为 0；
- 24 小时单 Worker没有无界内存增长；
- staging enforce、rollback、drain rehearsal 完成；
- production canary 所有观察窗口通过；
- 文档中存在脱敏、hash-bound、可审计证据。

legacy producer 删除前额外要求 v1 drain 归零；legacy reader 删除前额外要求一个发布周期
reader hit 为 0。

---

## 13. 风险与回滚

| 风险 | 控制 | 回滚 |
| --- | --- | --- |
| cohort assignment 漂移 | 持久化 policy/hash/reason | 停止新 assignment，已有 Run 不变 |
| Shadow 隐藏成本 | NoRemoteEffects + Ledger deny + hard metric | 立即关闭 Shadow |
| Eval 平均分掩盖坏例 | blocking case + variance + P95 | Gate 失败，不扩大 |
| v4 质量好但资源恶化 | budget/P95/RSS/queue 同时 Gate | 保持较小 cohort 或切回新 Run v1 |
| rollback 误读 snapshot | worker version routing fail closed | drain/stop v4，不交给 v1 |
| producer 过早删除 | durable inventory + rollback window | 不批准删除提交 |
| reader 长尾仍被使用 | hit counter + release window | 延长 reader 保留期 |
| 指标泄露用户信息 | 低基数 labels + redaction tests | 禁用该 exporter/字段 |
| provider 波动污染结论 | 重复运行、方差、时间窗、config hash | 重新采样，不调低 Gate |

---

## 14. 完成清单

- [ ] RolloutAssignment/GateDecision/Drain persistence 完成
- [ ] v1/v4 ablation matrix、重复统计和机器 Gate 完成
- [ ] production Shadow 零额外 model/capability 物理调用
- [ ] staging selected Task enforce 证据通过
- [ ] production canary 分档通过
- [ ] rollback 与 drain rehearsal 完成
- [ ] eligible 新 Run 默认 v4
- [ ] v1 active Run/dispatch/outbox/ledger/snapshot 全部 drain
- [ ] legacy producer 删除，reader 兼容回归通过
- [ ] 一个发布周期 reader hit 为 0
- [ ] legacy reader/config/snapshot compatibility 删除并保留 proto reserved
- [ ] 最终设计/ADR/运维文档和脱敏 evidence 完成

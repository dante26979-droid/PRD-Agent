# Agent Loop 剩余闭环设计：Readiness、环境晋级与 Legacy Retirement

> 状态：LOCAL IMPLEMENTATION COMPLETE；环境晋级待受控证据
> 日期：2026-08-02
> 前置设计：[Phase 9–11 统一设计](./2026-08-01-agent-loop-remaining-implementation-design.md)
> 当前版本：`agent-runtime.v4` / `agent-loop-snapshot.v3` / migration `0021`
> 目标：补齐本地证据与 operator lifecycle，完成 Phase 10 环境晋级，并在 Gate 通过后执行
> Phase 11 legacy 退场。

---

## 0. 设计结论

剩余工作分为三类，必须按顺序执行：

```text
R1 Local Contract Closure
  -> R2 Production Semantic / Shadow Proof
  -> R3 Durable Readiness Record
  -> Phase 10 Environment Graduation
  -> Phase 11 Legacy Retirement
```

当前代码已具备 v4 Review Workflow、Scoped Unit Semantic、ReviewProjection、durable rollout
authority、pause/rollback、Shadow Artifact、retention 和本地 Compose 部署。不能重新实现这些能力，
也不能因为本地测试通过直接删除 v1。

剩余代码工作只有四项：

1. Memory/PostgreSQL 共用 Review Workflow contract suite；
2. 带真实 legacy row 的 migration upgrade fixture；
3. production repair 与 Shadow persisted-output binding 的 E2E proof；
4. `record-gate -> transition -> readiness record` 的统一、幂等 operator lifecycle。

Phase 10 是环境动作和观察证据；Phase 11 是不可逆代码删除。二者不能用 mock、本地 deterministic
Adapter 或人工勾选代替。

> 2026-08-02 实现校验：R1–R3 的代码、迁移、CLI、共用 contract、PostgreSQL upgrade fixture、
> Worker repair E2E、persisted Shadow trace 校验与本地 Compose 部署均已完成。验证证据见
> [最终实现与本地部署报告](./2026-08-02-agent-loop-final-implementation-and-local-deployment-report.md)。
> Phase 10/11 仍按本文的环境 Gate 执行，不因本地通过而自动晋级或删除 v1。

---

## 1. 当前基线与准确缺口

### 1.1 已完成

- PostgreSQL `0001 -> 0020` clean install；
- PostgreSQL v4 Outline -> Unit -> Full Review -> Publish/retention E2E；
- concurrent confirmation、idempotency replay、rollback authority；
- production `ScopedUnitSemanticModule` 与 bounded Supplement；
- `REVISE_UNIT` 已执行 patch -> claim extraction -> Grounding -> Quality，并推进 generation；
- v1/v4 unified ReviewProjection 与 Web workflow；
- durable active policy 驱动新 Agent Run assignment；
- `rolloutctl inspect/pause/resume/rollback/drain/readiness`；
- assignment -> Worker evaluation context -> `SHADOW_EVALUATION` Artifact；
- Memory/PostgreSQL Ledger 的 SHADOW MODEL/CAPABILITY deny；
- Publish payload bounded retention。

### 1.2 尚缺代码证据

| 缺口 | 当前状态 | 完成标准 |
| --- | --- | --- |
| Review contract | Memory/PG 各有测试，fixture 未共用 | 同一 case table 对两个 Adapter 运行 |
| legacy migration | schema-only upgrade 通过 | 0015 legacy rows 升到 0020 后语义不漂移 |
| repair production proof | Module 单测通过 | Worker + Artifact + generation + crash replay 通过 |
| Shadow trace authority | Go 校验 embedded trace/assignment hash | 从 persisted output 重建 trace 后再接受 Artifact |
| Gate command lifecycle | Gate/transition Interface 与 operator command 分离 | 同一 command identity/idempotency/expected-version 模型 |
| Readiness persistence | renderer 只输出 JSON | identity/hash/decision durable，能 verify/rebuild |

### 1.3 只能由环境证明

- remote LLM quality、latency、budget 与 provider failure；
- staging Shadow/Enforce 的观察窗口；
- Feishu staging publish/reconciliation；
- production canary 每档样本与时间窗口；
- v1 producer/reader inventory 为零；
- 一个完整发布周期 reader hit 为零。

---

## 2. 不变量

1. **Published PRD owner**：Feishu 继续拥有 Published PRD 正文；PostgreSQL 只保存 bounded recovery
   payload、hash、binding 和 audit metadata。
2. **Repository owner**：GitHub immutable revision 是 Repository Snapshot authority。
3. **Assignment immutable**：policy transition、pause 或 rollback 只影响命令之后的新 Agent Run。
4. **PostgreSQL authority**：Redis、进程环境和 Worker memory 不决定 rollout stage、assignment、drain
   或 readiness。
5. **Confirmation Unit**：Web、HTTP、Worker 和 persistence 使用同一 Confirmation Unit 语义。
6. **Remote-only production**：production 不允许 local model fallback。
7. **No silent upgrade**：未知 v1 snapshot/field 必须显式 unsupported，不能路由到 v4。
8. **No weak Gate**：样本不足只延长当前阶段，不能降低阈值或跳档。

---

## 3. R1：Local Contract Closure

### 3.1 Shared Review Workflow contract

新增 test-only contract Module：

```text
backend-go/internal/runcontrol/review_contract_test.go
backend-go/internal/storage/review_contract_postgres_test.go
```

定义最小 driver：

```go
type ReviewContractDriver interface {
    CreateV4Task(ctx context.Context, fixture ReviewFixture) ReviewHandle
    MaterializeOutline(ctx context.Context, handle ReviewHandle) ReviewOutlineVersion
    ConfirmOutline(ctx context.Context, command ConfirmOutlineCommand) ReviewTransition
    MaterializeUnit(ctx context.Context, handle ReviewHandle, unitKey string) ReviewUnit
    DecideUnit(ctx context.Context, command UnitDecisionCommand) ReviewTransition
    MaterializeFullReview(ctx context.Context, handle ReviewHandle) FullReviewReport
    ReadProjection(ctx context.Context, handle ReviewHandle) ReviewView
}
```

它是测试 Interface，不进入 production Store。Memory 与 PostgreSQL 分别提供 fixture Adapter；每个
case 只写一次。

必须覆盖：

- Outline materialization replay；
- same-key concurrent confirm 返回同一 transition/next Run；
- stale version conflict；
- dependency-ready Unit 顺序；
- reopen dependent closure；
- Full Review input hash drift；
- Publish Gate blocked/passed；
- terminal publish payload purge 后 projection 不丢 audit identity。

Gate R1-A：Memory/PostgreSQL case 数量、case ID 和期望结果完全一致，PostgreSQL DSN 缺失时 CI
job 失败而不是 skip。

### 3.2 Legacy migration data fixture

新增隔离数据库测试：

```text
apply 0001..0015
  -> insert v1 Task/Run/Draft/Publish/Snapshot/Gate rows
  -> apply 0016..0020
  -> verify old reader behavior
  -> create new v4 Task
  -> verify assignment/readiness/retention
```

fixture 必须包含：

- terminal/non-terminal v1 Run；
- published 与 result-unknown Publish Intent；
- v1/v2 snapshot；
- legacy Working Draft；
- non-empty reader observation；
- rollout assignment 在 `assignment_hash=''` 的历史状态。

升级策略：历史空 `assignment_hash` 不伪造为新 hash；旧 Run 保持 legacy identity，新 Run 必须写入
完整 hash。reader 通过 compatibility path 读取，任何新 mutation 必须使用 current schema。

Gate R1-B：升级前后 v1 projection/audit identity 一致；新 v4 写入不产生空 policy/assignment hash。

---

## 4. R2：Production Semantic 与 Shadow Proof

### 4.1 Repair production contract

当前单测已证明 `REVISE_UNIT` 会 re-Ground/re-Quality。剩余工作不是新增算法，而是增加真实 Worker
contract：

```text
reopen Confirmation Unit
  -> REVISE_UNIT Run
  -> model patch Artifact ACK
  -> claim Artifact ACK
  -> grounding Artifact ACK
  -> quality report in RunOutput
  -> crash before completion
  -> retry/replay
  -> identical content hash and generations
```

断言：

- `claim_generation = previous + 1`；
- `grounding_generation = previous + 1`；
- `quality_generation = previous + 1`；
- `requires_regrounding=false`；
- repair 只能修改 current Confirmation Unit；
- unknown/conflict 未解决时 Quality 不能 PASSED；
- crash replay 不增加 physical model/capability call。

Gate R2-A：Worker contract、PostgreSQL Artifact replay 和 budget ledger 三者同时通过。

### 4.2 Persisted authoritative trace projection

新增深 Module：

```go
type AuthoritativeTraceProjection interface {
    BuildTrace(ctx context.Context, runID string) (PublicRunTrace, error)
    ValidateShadowArtifact(ctx context.Context, runID string, artifact RunArtifact) error
}
```

Memory/PostgreSQL Adapter 从已 ACK 的 Working Draft 或 RunOutput、RunPurpose、checkpoint sequence、
assignment identity 重建 canonical public trace。禁止读取 prompt、hidden reasoning、secret 或未授权
Evidence excerpt。

接收顺序固定为：

```text
authoritative output ACK
  -> BuildTrace from persisted state
  -> Worker SHADOW_EVALUATION ACK
  -> Go recompute trace hash
  -> assignment/policy/effect validation
  -> Artifact accepted
```

任一 drift 返回 `SHADOW_TRACE_IDENTITY_MISMATCH`，不修改 authoritative output，并触发 rollout
pause evidence。

Gate R2-B：Memory/PostgreSQL contract 一致；output、assignment、policy、request hash 任一变更均
被拒绝；Artifact replay 不重复写入。

---

## 5. R3：Durable Gate、Transition 与 Readiness

### 5.1 Migration 0021

新增：

```text
go_rollout_readiness_records
  readiness_id PK
  schema_version
  candidate_version
  baseline_version
  commit_hash
  migration_set_hash
  contract_report_hash
  postgres_report_hash
  eval_manifest_hash
  shadow_policy_hash
  gate_decision_id FK
  rollout_state_version
  blocking_gate_codes JSONB
  record_hash
  status CHECK (BLOCKED, STAGING_READY)
  created_at

go_rollout_commands
  expand command_kind:
    RECORD_GATE
    TRANSITION_STAGE
    RECORD_READINESS
```

不修改旧 migration；0021 删除并重建 `command_kind` CHECK constraint。Readiness 只保存 hash 和
低敏 metadata，不保存报告正文。

### 5.2 One operator command model

把现有 `RecordGateDecision`、`AuthorizeRolloutTransition` 和 operator command 收敛到同一 validation
Implementation：

```text
inspect
record-gate --manifest --report --expected-version --idempotency-key --actor-ref
transition --to --policy-version --gate-decision-id --expected-version
pause / resume / rollback
drain begin / refresh / abort
readiness render / record / verify
```

所有 mutation：

- 默认 dry-run；
- `--apply` 才持久化；
- 必须携带 expected state version；
- command ID 重放返回相同 result；
- command ID + 不同 request hash 返回 idempotency conflict；
- stdout 只输出 machine-readable summary；
- manifest/report 正文不进入数据库和日志。

### 5.3 Readiness state machine

状态：

```text
BLOCKED
  -> LOCAL_CONTRACTS_PASSED
  -> POSTGRES_PROOF_PASSED
  -> SEMANTIC_SHADOW_PROOF_PASSED
  -> STAGING_READY
```

`STAGING_READY` 必须同时满足：

- R1-A/R1-B/R2-A/R2-B 全通过；
- rollout stage 为 `LOCALLY_VERIFIED`；
- active policy/version 与 stage state 一致；
- passed GateDecision hash 与 readiness input 一致；
- rollout 未 paused；
- migration set 是 `0001..0021`；
- readiness record rebuild 后 `record_hash` 相同。

Gate R3：在缺少任一输入时生成 `BLOCKED` record；不能通过 CLI 参数清空 blocker。

---

## 6. Phase 10：Environment Graduation

Phase 10 不再开发新的业务语义，只执行已审核的 command、观察和 rollback。若必须修改代码，立即
退出 Phase 10，回到 R1～R3 重新生成 readiness record。

### 6.1 10A Remote Eval

1. 固定 candidate/baseline、dataset、model、prompt、Artifact schema 与 config hash；
2. 每 case 至少 3 次，blocking case 单独判定；
3. 输出质量、grounding、tool/token、P50/P95 latency、RSS、queue wait、duplicate call；
4. provider outage 记为环境失败，不降低质量阈值；
5. `record-gate` 后 transition 到 `STAGING_SHADOW`。

退出 Gate：remote GateDecision PASSED；report/manifest/config/dataset hash 全部绑定。

### 6.2 10B Staging Shadow

- v1 authoritative，v4 deterministic Shadow；
- zero extra model/capability/draft/unit/publish effects；
- Artifact reject、hash drift、effect violation 任一出现立即 pause；
- 最小时长和 completed sample 写入 manifest；低流量时延长窗口，不降低阈值。

建议初始窗口：至少 24 小时且至少 50 个 completed Agent Run；若业务量无法满足，以更长时间换取
样本，不允许人工豁免 hard Gate。

### 6.3 10C Staging Enforce

- 仅 allowlist tenant/owner/Task 使用 v4；
- 使用真实 remote LLM、authorized Capability 和 Feishu staging binding；
- 覆盖 Outline -> Confirmation Unit -> Full Review -> Publish/reconcile；
- 强制演练：pause -> rollback new assignments -> existing v4 drain/stop -> resume；
- 验证 rollback 后新 Run 为 safe policy，已有 v4 assignment 不变。

退出 Gate：E2E、crash recovery、budget、quality、publish result-unknown reconciliation 和 rollback
rehearsal 全通过。

### 6.4 10D Production Canary

cohort 固定：

```text
100 bps -> 500 bps -> 2000 bps -> 5000 bps -> 10000 bps
```

每档必须拥有独立 policy version、GateDecision、expected state version 和 evidence hash。每档至少满足
manifest 的最小时长和最小样本；不得在同一 command 中跨多档。

Hard Gate：

- duplicate physical call = 0；
- cross-tenant access = 0；
- unauthorized source use = 0；
- publish wrong target = 0；
- queue/budget invariant violation = 0；
- blocking quality case failure = 0。

### 6.5 10E Production Default

- default workflow 切到 v4；
- 停止创建新 v1 Run，但保留 v1 Worker/producer/reader；
- 完成稳定观察窗口和一次 production rollback rehearsal；
- 创建 `PRODUCTION_DEFAULT` GateDecision；
- 只有该 Gate 通过才能开始 Phase 11。

---

## 7. Phase 11：Legacy Retirement

### 7.1 11A Execution drain

`drain begin --workflow agent-runtime.v1` 后持续 refresh：

- active/waiting/stopping Run = 0；
- active dispatch = 0；
- unpublished outbox = 0；
- non-terminal ledger = 0；
- recovery-required snapshot = 0。

reader hit 不计入 execution drain，但进入后续 reader observation Gate。

### 7.2 11B Producer removal

先生成并 hash-bind symbol inventory，删除：

- v1 whole-draft producer；
- v1 producer-only graph branch/factory；
- 创建新 v1 RunOutput/snapshot 的配置；
- producer-only tests 和 rollout flags。

保留：

- v1 Working Draft/Publish reader；
- retention 内 snapshot resume reader；
- historical audit/hash；
- protobuf 旧 field/enum。

Producer removal 单独提交，必须可通过回滚 commit 恢复；此时不删除 reader。

### 7.3 11C Reader observation

- producer removal 部署后观察至少一个完整发布周期；
- observation 按 release/workflow/reader kind/window 记录；
- current window hit 必须为 0；
- retention 内不存在需要恢复的 v1 snapshot；
- 任何 hit 都重置观察窗口。

### 7.4 11D Reader and compatibility removal

删除：

- Web/HTTP legacy Working Draft reader；
- legacy publish projection；
- expired v1/v2 snapshot compatibility；
- legacy config 与 dead rollout branches。

协议处理：

- protobuf 旧 field number/name 使用 `reserved`；
- enum numeric value 不复用；
- migration history 永久保留；
- unsupported legacy input 返回明确错误码。

最后 transition 到 `LEGACY_RETIRED`，必须绑定 completed DrainRecord、zero reader window、removal
inventory hash 和最终 evidence hash。

---

## 8. 测试与证据矩阵

| Gate | Unit | Contract | PostgreSQL | Worker | Environment |
| --- | --- | --- | --- | --- | --- |
| R1 Review | transition | Memory/PG shared | concurrent/replay | - | - |
| R1 Migration | parser | upgrade assertions | 0015 rows -> 0021 | - | - |
| R2 Repair | generations | output/artifact | crash replay | real Worker path | staging enforce |
| R2 Shadow | canonical trace | Memory/PG shared | persisted rebuild | Artifact ACK order | staging window |
| R3 Operator | validation | dry-run/apply | idempotency/version | context identity | operator rehearsal |
| Phase 10 | eval policies | schema/hash | evidence records | remote execution | staging/production |
| Phase 11 | inventory | unsupported behavior | drain/readers | old worker drain | release window |

本地全量命令：

```bash
cd backend-go && go test ./... && go vet ./...
python -m pytest agent-python/tests -q
python -m pytest -q
npm --prefix web test -- --run
npm --prefix web run typecheck
npm --prefix web run build
```

PostgreSQL Gate 必须使用每次新建的隔离数据库；不允许复用失败测试留下的 rollout stage、active
policy 或 drain record。

---

## 9. 实现顺序与退出条件

| 顺序 | 实现项 | 退出条件 |
| --- | --- | --- |
| 1 | Shared Review contract | Memory/PG 同 case 全通过 |
| 2 | Legacy migration fixture | 0015 data -> 0021 语义一致 |
| 3 | Repair Worker E2E | generation、Artifact、crash replay 通过 |
| 4 | Persisted trace validation | output/assignment/policy drift 全拒绝 |
| 5 | Migration 0021 + unified commands | dry-run/apply/replay/version contract 通过 |
| 6 | Durable readiness record | `STAGING_READY` 可重建且 hash 稳定 |
| 7 | Remote Eval | GateDecision PASSED |
| 8 | Staging Shadow | 时间/样本/effect Gate 通过 |
| 9 | Staging Enforce + rollback | 真实 E2E 与 rehearsal 通过 |
| 10 | Production canary | 100→10000 bps 逐档通过 |
| 11 | Production default | 稳定窗口通过 |
| 12 | v1 execution drain | execution inventory 全零 |
| 13 | Producer removal | symbol inventory 无 producer |
| 14 | Reader observation | 一个发布周期 hit=0 |
| 15 | Reader/protocol removal | legacy 显式 unsupported |

任一步失败：保持当前 stage，生成 failed GateDecision/evidence，必要时 PAUSE 或
ROLLBACK_NEW_ASSIGNMENTS；禁止继续执行下一步。

---

## 10. 完成定义

全部剩余工作只有在以下条件同时满足时完成：

- local contract、migration、repair、Shadow 与 operator Gate 全通过；
- readiness durable status 为 `STAGING_READY`；
- Phase 10 每次 stage/cohort 都有 hash-bound GateDecision；
- production default v4 稳定窗口通过；
- v1 execution drain 全零；
- producer removal 后一个发布周期 reader hit 为零；
- legacy reader/config/protocol compatibility 删除并显式 reserved；
- Feishu/GitHub content ownership仍满足 ADR-0001；
- production 仍满足 remote-LLM-only 与 bounded fair queue ADR；
- 最终 ADR、runbook、脱敏 evidence 和 removal inventory hash 完整。

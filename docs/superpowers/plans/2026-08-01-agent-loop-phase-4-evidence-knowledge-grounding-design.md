# Agent Loop Phase 4：Evidence Knowledge 与 Claim Grounding 设计及实施 Plan

> 状态：IMPLEMENTED / LOCALLY VERIFIED
> 日期：2026-08-01
> 前置阶段：Phase 3 Information Need Planning 已完成本地验证
> 后续阶段：Phase 5 Unified Investigation 依赖本阶段的 `KnowledgeBundle`、Coverage 与 progress 语义
> 对应总设计：[`2026-07-31-agent-loop-semantic-deepening-design.md`](../specs/2026-07-31-agent-loop-semantic-deepening-design.md)
> 对应总计划：[`2026-07-31-agent-loop-semantic-deepening-implementation-plan.md`](2026-07-31-agent-loop-semantic-deepening-implementation-plan.md)
> 目标版本：仅 `agent-runtime.v4`；`agent-runtime.v1` 保持只读兼容

> 本地验证：Agent Python 120 passed；根目录 312 passed / 15 skipped；Go `go test ./...` 与 `go vet ./...` 通过。未执行真实 PostgreSQL、Feishu/GitHub staging 或 production canary。

---

## 0. 结论

Phase 4 先解决一个语义错误：**检索命中只代表找到来源，不代表形成事实，更不代表来源支持 Draft Claim。**

本阶段新增一个较深的 `EvidenceKnowledgeModule`，把 Capability 输出转换为版本化的
`KnowledgeBundle`；同时深化 `GroundingModule`，让 Claim 只能通过 Verified Fact 获得
支持。Graph 只消费 Module 输出，不再解释 excerpt、Fact scope、Conflict 或 Coverage。

固定处理顺序为：

```text
Capability Outcome
  -> Source Authority / Access / Revision / Locator / Hash validation
  -> deterministic normalization and Fact extraction
  -> Unknown and Source Conflict construction
  -> Fact verification
  -> optional bounded semantic support classification
  -> KnowledgeBundle Artifact
  -> verdict-based Coverage update
  -> Claim-to-Fact Grounding
  -> GroundingReport Artifact
```

本阶段必须删除两条现有捷径：

1. `graph/runtime.py::observe_progress()` 不能再以 `Evidence count > 0` 完成 Coverage；
2. `graph/advanced.py::supplement()` 不能再把本轮所有 Evidence ref 附到所有 unsupported Claim。

Phase 4 不统一 Initial/Replan/Supplement 的控制流；Phase 5 在本阶段的 Knowledge 语义
稳定后完成该重构。

---

## 1. 当前基线与失败模式

### 1.1 已有可复用能力

- Phase 2 已提供共享 Run Execution Ledger、稳定 operation key、预算预留和 durable replay；
- Phase 3 已提供版本化 `InformationNeedPlan`、typed Coverage、Source Type 和 Need 子预算；
- Capability 输出已通过 Go ACK 保存为直接 Evidence；
- `DRAFT_BUNDLE`、`GROUNDING_REPORT` 和 snapshot v3 已有 Artifact/恢复骨架；
- 旧 `src/prd_agent/evidence/` 与 `src/prd_agent/grounding/service.py` 包含可迁移的纯规则。

### 1.2 当前错误路径

| 当前行为 | 错误 | Phase 4 处理 |
| --- | --- | --- |
| 任意非空 Capability 结果把 active gap 标为 `COVERED` | 相关命中被误当成信息充分 | Coverage 只消费 Knowledge verdict |
| Claim 的 ref 存在于全局 Evidence 集合即 `SUPPORTED` | 未证明 Evidence 支持 Claim | Claim 必须引用 Supported Fact |
| Supplement 把全部新 ref 附到全部 unsupported Claim | 引用关系被批量伪造 | 删除自动附加，仅输出 scoped Need |
| EvidenceItem 缺少结构化 binding/version/access authority | 历史 PRD 无法可靠校验权限与 revision | expand-only 来源权威合同 |
| `EMPTY` 只表现为零命中 | 容易被误推导成“不存在” | 形成显式 Unknown |
| 旧 Fact/Conflict 使用随机 UUID | replay 后 ID 与 Artifact hash 不稳定 | 全部改为 canonical content hash |

### 1.3 不采用的方向

- 不通过加长 Prompt 让模型自行判断来源是否可信；
- 不让模型把 `INFERRED` 或普通搜索摘要升级为 `CODE_VERIFIED`；
- 不新增长期 Fact 数据库或第二套内容权威；
- 不把旧 Python Store、应用状态机或随机 identity 迁入 Agent Runtime；
- 不在 Grounding 内部直接发起 Supplement；Grounding 只返回 disposition；
- 不先拆目录再保留“Evidence 非空即完成”的旧语义。

---

## 2. 目标、非目标与完成定义

### 2.1 目标

- 每个成功 Capability observation 都产生或重放一个 `KNOWLEDGE_BUNDLE`；
- Evidence 在进入 Fact extraction 前完成来源、权限、revision、locator 和 hash 校验；
- 确定性 Parser 优先产生 `CODE_VERIFIED` Fact；
- 空、部分、权限阻断和工具失败形成 typed Unknown；
- 同一 subject/predicate 的不一致确定性值形成 open Source Conflict；
- Coverage 只能由 Fact/Unknown/Conflict verdict 更新；
- 每个 blocking `CURRENT_STATE` Claim 都有可审计 `GroundingFinding`；
- 语义分类模型只判断“已验证 Fact 是否支持 Claim”，不能改变来源或 Fact 类型；
- Knowledge、Grounding 和恢复 identity/hash 稳定；
- `agent-runtime.v1` 与已有 Artifact reader 不被原地升级。

### 2.2 非目标

- 不实现真正 Replan、统一 Investigation 或 Supplement 执行；这是 Phase 5；
- 不按 Confirmation Unit 生成或 Repair；这是 Phase 6；
- 不切换生产默认 workflow；这是 Phase 7；
- 不存储完整仓库、完整历史 PRD 或无限期 Knowledge；
- 不允许 Python Worker 直连 PostgreSQL；
- 不把全文自由生成当作 Fact extractor。

### 2.3 完成定义

- 非空但仅相关的 Evidence 不完成 Coverage，也不支持 Claim；
- stale、越权或不可复现 Evidence 不进入 Supported Fact；
- `EMPTY` 形成 Unknown，且不会生成否定事实；
- open Conflict 阻止对应 Fact/Claim 变为 `SUPPORTED`；
- `CURRENT_STATE` 只能由 `CURRENT_STATE` 范围内的受支持 Fact 证明；
- `KNOWLEDGE_BUNDLE` durable 后才保存 `OBSERVED` checkpoint；
- Grounding 可从 Artifact 恢复，零重复物理 Capability/模型调用；
- 旧自动 ref 附加与 Evidence-count Coverage producer 被删除。

---

## 3. 核心不变量

### 3.1 Evidence、Fact 与 Claim 分层

```text
Retrieval Hit != Evidence
Evidence != Fact
Related Evidence != Supporting Evidence
Supported Fact != Supported Claim
```

只有同时满足以下条件，Claim 才可 `SUPPORTED`：

1. Evidence 来源权威、权限范围、revision、locator 与 hash 有效；
2. Fact 的类型、scope、verification status 和 Evidence 链接有效；
3. Evidence 直接或经有界分类支持 Fact，不只是主题相关；
4. Claim kind 与 Fact scope 匹配；
5. 没有覆盖该 Fact 的 open Conflict；
6. Claim 所需的全部 blocking Fact 均通过。

### 3.2 Fail-closed

- 缺 source authority、revision、locator 或 excerpt hash：`INVALID_SOURCE`；
- authority 匹配但版本不一致：`STALE_SOURCE`；
- access scope 不一致：`ACCESS_SCOPE_MISMATCH`，不得尝试绕到未授权来源；
- 无法证明支持：默认 `UNSUPPORTED`，而不是相似度阈值通过；
- Fact scope 不明确：保留为 `INFERRED` 或 Unknown，不能猜测 `CURRENT_STATE`；
- 模型输出非法或超界：拒绝该分类，不修改确定性结果。

### 3.3 Identity 稳定

所有 ID 使用 canonical JSON + SHA-256；禁止 UUID 和调用顺序参与 identity：

```text
evidence_id = H(source kind, binding/source id, source version,
                canonical locator, excerpt hash, access scope hash)

fact_id = H(fact scope, fact type, canonical subject/predicate/value,
            sorted evidence ids, extractor id/version)

unknown_id = H(need plan id, coverage key, reason code,
               action signature, source authority)

conflict_id = H(subject, predicate, sorted fact ids)
```

同一内容 replay 必须产生相同 ID、相同排序和相同 Artifact hash。

### 3.4 内容所有权与保留

- Go 继续持久化直接 Evidence 和来源元数据；
- `KNOWLEDGE_BUNDLE` 用于 active recovery、审阅与有界保留；
- 不新增第二个长期 Fact authority 表；
- Artifact 可保存有界 excerpt，但不得保存完整仓库或完整 PRD；
- 模型请求使用通过访问校验的最小 excerpt，日志只保存 hash、ID 和公开摘要。

### 3.5 Ledger 与 durable 顺序

Capability 路径固定为：

```text
ACTION_VALIDATED checkpoint ACK
  -> Capability Ledger reserve/physical call or replay
  -> direct Evidence ACK
  -> deterministic Knowledge build
  -> optional classifier Model Ledger call/replay
  -> KNOWLEDGE_BUNDLE Artifact ACK
  -> OBSERVED checkpoint ACK
```

Grounding 路径固定为：

```text
DRAFT_BUNDLE Artifact ACK
  -> deterministic Claim/Fact policy
  -> optional classifier Model Ledger call/replay
  -> GROUNDING_REPORT Artifact ACK
  -> GROUNDED / GROUNDING_PARTIAL / GROUNDING_SUPPLEMENT_REQUIRED checkpoint ACK
```

---

## 4. 模块架构

### 4.1 深 Module Interface

```python
@dataclass(frozen=True)
class KnowledgeBuildRequest:
    run_id: str
    task_id: str
    need_plan: InformationNeedPlan
    source_authorities: tuple[SourceAuthority, ...]
    action: ProposedAction
    capability_outcome: CapabilityOutcome
    prior_bundle: KnowledgeBundle | None


@dataclass(frozen=True)
class KnowledgeBuildResult:
    bundle: KnowledgeBundle
    progress: ProgressAssessment
    artifact_identity: ArtifactIdentity
    replayed: bool


class EvidenceKnowledgeModule:
    def build(self, request: KnowledgeBuildRequest) -> KnowledgeBuildResult: ...


@dataclass(frozen=True)
class GroundingRequest:
    draft: DraftBundle
    knowledge: KnowledgeBundle
    source_authorities: tuple[SourceAuthority, ...]
    remaining_supplements: int


class GroundingModule:
    def assess(self, request: GroundingRequest) -> GroundingReport: ...
```

Graph 只知道输入、输出、Artifact identity 和路由结果。来源校验、Fact extraction、
Conflict、分类器 schema、Coverage 和 canonicalization 均保持 Module 内部。

删除 `EvidenceKnowledgeModule` 时，上述复杂度会重新泄漏到 Investigation、Graph、
Grounding 与 Resume，因此该模块具有足够的 Depth、Leverage 和 Locality。

### 4.2 目标目录

```text
agent-python/agent/knowledge/
  __init__.py
  models.py              # versioned domain models
  identity.py            # canonical ID and fingerprint
  source_validator.py    # authority/revision/access/locator/hash
  normalizer.py          # capability outcome -> Evidence/Unknown
  fact_extractor.py      # deterministic parser facts
  fact_verifier.py       # support/status rules
  conflict_detector.py   # deterministic conflicts
  coverage.py            # verdict-based coverage projection
  artifact.py            # canonical encode/decode/hydrate
  module.py              # only public orchestration seam

agent-python/agent/grounding/
  models.py
  policies.py
  classifier.py
  prompts.py
  artifact.py
  module.py
```

只有远程语义分类存在 Adapter seam；确定性 Policy 只有一个实现，不创建无收益的
Protocol。GitHub 与 Feishu provenance 具有真实变化轴，可作为 `SourceValidator` 内部
的两个 Adapter，但不向 Graph 暴露。

### 4.3 旧逻辑迁移边界

可迁移思想：

- `src/prd_agent/evidence/deterministic_validator.py` 的 snapshot/path/hash 校验；
- `src/prd_agent/evidence/fact_builder.py` 的 OpenAPI/schema parser Fact；
- `src/prd_agent/evidence/conflict_detector.py` 的 subject/predicate 冲突分组；
- `src/prd_agent/grounding/service.py` 的 Fact type/scope 与 Claim kind 约束；
- `src/prd_agent/investigation/policies.py` 的 Coverage 状态思想。

禁止迁移：

- Store、数据库或应用状态机依赖；
- 随机 UUID；
- Grounding 内部递归调用 retry provider；
- 以 Fact value 字符串是否完整出现在 excerpt 中作为唯一通用支持算法；
- 旧的长期 persistence owner。

---

## 5. 来源权威合同

### 5.1 为什么需要 expand-only 扩展

当前 `EvidenceItem` 只有 `source_type/source_id/locator/excerpt_hash/excerpt`。代码来源可
借助 `RunContext.repository_binding_id/repository_revision` 部分校验；历史 PRD 则缺少
明确 binding、corpus/document version 与 access scope，无法满足 ADR-0001 的读取时
重新校验要求。

因此 Phase 4 首先扩展合同，不改变旧字段编号：

```proto
message SourceAuthority {
  string source_kind = 1;
  string binding_id = 2;
  string source_id = 3;
  string source_version = 4;
  string access_scope_hash = 5;
}

message AgentRunInput {
  // existing fields 1..22 unchanged
  repeated SourceAuthority allowed_source_authorities = 23;
}

message EvidenceItem {
  // existing fields 1..5 unchanged
  string source_kind = 6;
  string binding_id = 7;
  string source_version = 8;
  string access_scope_hash = 9;
  string outcome_kind = 10;
}
```

规则：

- Go Control Plane/Capability Gateway 产生 authority，Python 不自行发明；
- v4 Evidence 必须匹配 `allowed_source_authorities`；
- legacy v1 reader 继续读取 1..5；v4 producer 在过渡期 dual-write 兼容字段；
- `outcome_kind` 受控为 `HIT/EMPTY/PARTIAL/ACCESS_BLOCKED/TOOL_FAILED`；
- 原始错误消息不进入 Artifact，仅保存稳定 reason code 与公开摘要；
- 代码 revision 使用固定 40 位 commit SHA；历史 PRD 使用 immutable source revision；
- access scope hash 只用于等值校验，不暴露权限正文。

### 5.2 Source validation

GitHub Evidence：

- `binding_id` 与 Run authority 一致；
- `source_version` 与固定 Repository Snapshot commit 一致；
- locator 必须解析为该 binding/revision 下的允许 path + line/symbol；
- path 通过 repository path/content policy；
- excerpt 由 locator 重新读取后完全匹配，或匹配 Capability 的可信 blob receipt；
- excerpt hash 使用规范化换行后的 UTF-8 内容。

Feishu Evidence：

- corpus/document binding 与 Run authority 一致；
- `source_version` 与 PRD Source Revision 一致；
- Section Locator 属于该 source revision；
- 当前 owner 的 access scope hash 与检索时一致；
- section excerpt 与 locator/revision receipt 一致。

validation 结果只允许：`VALID / INVALID_SOURCE / STALE_SOURCE /
ACCESS_SCOPE_MISMATCH / LOCATOR_MISMATCH / HASH_MISMATCH`。

---

## 6. Knowledge 数据模型

### 6.1 Evidence 与 Fact

```python
class FactScope(StrEnum):
    CURRENT_STATE = "CURRENT_STATE"
    HISTORICAL_CONTEXT = "HISTORICAL_CONTEXT"
    TARGET_DECISION = "TARGET_DECISION"
    ASSUMPTION = "ASSUMPTION"


class FactType(StrEnum):
    USER_CONFIRMED = "USER_CONFIRMED"
    CODE_VERIFIED = "CODE_VERIFIED"
    DOCUMENT_SUPPORTED = "DOCUMENT_SUPPORTED"
    INFERRED = "INFERRED"


class VerificationStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    UNSUPPORTED = "UNSUPPORTED"
    CONFLICTING = "CONFLICTING"


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    source_authority: SourceAuthority
    locator: str
    excerpt_hash: str
    excerpt: str
    validation_status: str
    action_signature: str


@dataclass(frozen=True)
class VerifiedFact:
    fact_id: str
    subject: str
    predicate: str
    value: object
    fact_scope: FactScope
    fact_type: FactType
    verification_status: VerificationStatus
    evidence_ids: tuple[str, ...]
    extractor_id: str
    extractor_version: str
```

`VerifiedFact` 是统一类型名，不代表每一条都为 `SUPPORTED`；消费者必须检查
`verification_status`。`INFERRED` 可进入 Knowledge 供 Draft 标记 assumption/unknown，
但永远不能支持 `CURRENT_STATE`。

### 6.2 Unknown 与 Conflict

```python
class UnknownReason(StrEnum):
    EMPTY_RESULT = "EMPTY_RESULT"
    PARTIAL_RESULT = "PARTIAL_RESULT"
    PARSE_UNSUPPORTED = "PARSE_UNSUPPORTED"
    ACCESS_BLOCKED = "ACCESS_BLOCKED"
    TOOL_FAILED = "TOOL_FAILED"
    STALE_SOURCE = "STALE_SOURCE"
    UNSUPPORTED_EVIDENCE = "UNSUPPORTED_EVIDENCE"


@dataclass(frozen=True)
class Unknown:
    unknown_id: str
    coverage_key: str
    question: str
    reason_code: UnknownReason
    action_signature: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class SourceConflict:
    conflict_id: str
    subject: str
    predicate: str
    fact_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    status: Literal["OPEN", "RESOLVED"]
```

`EMPTY_RESULT` 表达“本次允许范围与查询下没有得到答案”，绝不表达目标对象不存在。
相同 Unknown replay 不产生新 progress；不同策略得到的新 Unknown 可记录，但仍不完成
blocking Coverage。

### 6.3 Coverage update

```python
@dataclass(frozen=True)
class CoverageUpdate:
    coverage_key: str
    before: CoverageStatus
    after: CoverageStatus
    supported_fact_ids: tuple[str, ...]
    unknown_ids: tuple[str, ...]
    conflict_ids: tuple[str, ...]
    reason_code: str
```

状态规则：

| Knowledge | Coverage after |
| --- | --- |
| 所需 Supported Fact 完整 | `COVERED` |
| 部分所需 Fact 或明确边界 Unknown | `PARTIAL` |
| open Conflict 覆盖 blocking 要求 | `CONFLICTING` |
| 仅相关/unsupported Evidence | 保持原状态 |
| `EMPTY/ACCESS_BLOCKED/TOOL_FAILED` | `MISSING` 或保持 `PARTIAL`，附 Unknown |

Coverage Requirement 的 requiredness/blocking 属性仍由 Phase 3 Plan 拥有；Phase 4 只能
更新状态，不能降低 Requiredness 或删除 Coverage key。

### 6.4 KnowledgeBundle v1

```python
@dataclass(frozen=True)
class KnowledgeBundle:
    schema_version: Literal["knowledge-bundle.v1"]
    bundle_id: str
    run_id: str
    task_id: str
    need_plan_id: str
    need_context_hash: str
    evidence: tuple[EvidenceRecord, ...]
    facts: tuple[VerifiedFact, ...]
    unknowns: tuple[Unknown, ...]
    conflicts: tuple[SourceConflict, ...]
    coverage: tuple[CoverageUpdate, ...]
    knowledge_fingerprint: str
    progress_fingerprint: str
    builder_version: str
```

- `knowledge_fingerprint` 包含全部有效记录，用于 Artifact identity/replay；
- `progress_fingerprint` 只包含 Supported Fact、Unknown、Conflict、Coverage 状态，以及
  与这些语义对象关联的有效 Evidence；
- 仅新增“相关但 unsupported”的 Evidence 可改变 Knowledge identity，但不能重置
  `no_progress_rounds`；
- tuple 在序列化前按稳定 ID 排序。

---

## 7. Fact extraction 与 verification

### 7.1 确定性优先

第一批 extractor：

- OpenAPI field/operation parser；
- database table/column/schema parser；
- 明确的 symbol/definition locator；
- 用户确认的目标决策和授权 assumption（由现有 ID 引用，不从自由文本猜测）。

parser 输出的 `subject/predicate/value/scope/type` 必须来自固定 schema。普通代码搜索命中
默认只是 Evidence；没有 parser 证明时不得自动产生 `CODE_VERIFIED` Fact。

### 7.2 有界语义分类

只有确定性规则无法判断“Evidence 是否支持候选 Fact”或“Fact 是否支持 Claim”时调用
远程模型。请求只包含：

- 单个 Claim 或候选 Fact；
- 已校验的最小 Evidence excerpt；
- Fact/Claim type 与允许 scope；
- 受控 verdict、reason code schema。

输出严格限制为：

```json
{
  "verdict": "SUPPORTED|PARTIAL|UNSUPPORTED|CONFLICTING",
  "reason_code": "...",
  "support_spans": ["evidence-id#offset"]
}
```

模型不能修改 `fact_type`、`fact_scope`、source authority、verification 上限或 Evidence
链接；支持 span 必须回指输入 excerpt。每次分类通过 Ledger 保存独立 Model Attempt，
operation key 基于 Claim/Fact/Evidence identity，而非循环计数。

### 7.3 Conflict 优先级

- 同一 `subject + predicate` 出现两个不同确定性 value，产生 open Conflict；
- open Conflict 将涉及 Fact 标记为 `CONFLICTING`；
- classifier 不能覆盖 deterministic Conflict；
- 只有新增的权威 Evidence 或用户决策可产生 `RESOLVED` 记录；不得原地删除历史 Conflict；
- Grounding 中 `CONFLICTING` 优先于 `PARTIAL/SUPPORTED`。

---

## 8. Claim Grounding

### 8.1 GroundingFinding v2

```python
class GroundingStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    UNSUPPORTED = "UNSUPPORTED"
    CONFLICTING = "CONFLICTING"
    NOT_REQUIRED = "NOT_REQUIRED"


@dataclass(frozen=True)
class GroundingFinding:
    claim_id: str
    claim_kind: ClaimKind
    criticality: ClaimCriticality
    status: GroundingStatus
    fact_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    reason_code: str
    disposition: GroundingDisposition
```

`GroundingDisposition` 受控为 `PASS / REQUEST_SUPPLEMENT / CONVERT_TO_UNKNOWN /
DOWNGRADE_TO_ASSUMPTION / HUMAN_CONFIRMATION_REQUIRED / DELETE_INFORMATIONAL`。

### 8.2 Claim-to-Fact scope matrix

| Claim kind | 可支持来源 |
| --- | --- |
| `CURRENT_STATE` | `CODE_VERIFIED + CURRENT_STATE`，或明确 `USER_CONFIRMED + CURRENT_STATE` |
| `HISTORICAL_CONTEXT` | `DOCUMENT_SUPPORTED + HISTORICAL_CONTEXT` |
| `TARGET_DECISION` | 已确认 decision ID；Evidence 可解释背景但不能代替决策 |
| `ASSUMPTION` | 已授权 assumption ID；必须展示为假设 |
| `RECOMMENDATION/RISK/UNKNOWN` | 必须正确标记；不得伪装成当前事实 |

规则：

- 只有 Evidence refs、没有 Fact IDs：`UNSUPPORTED/FACT_REQUIRED`；
- Fact scope 不匹配：`UNSUPPORTED/FACT_SCOPE_MISMATCH`；
- Fact 部分支持：Claim 至多 `PARTIAL`；
- 任一 blocking Fact 冲突：Claim `CONFLICTING`；
- 非 blocking recommendation 可 `NOT_REQUIRED`，但不能借此放行其中的当前状态陈述；
- unsupported blocking Claim 在预算允许时返回 scoped supplement request；不在 Grounding
  内执行 Capability。

### 8.3 GroundingReport v2

```python
@dataclass(frozen=True)
class GroundingReport:
    schema_version: Literal["grounding-report.v2"]
    run_id: str
    task_id: str
    draft_bundle_id: str
    knowledge_bundle_id: str
    outcome: Literal["GROUNDED", "GROUNDING_PARTIAL", "GROUNDING_SUPPLEMENT_REQUIRED"]
    findings: tuple[GroundingFinding, ...]
    supplement_scope: SupplementScope | None
    report_fingerprint: str
```

`SupplementScope` 只描述 unsupported blocking Claim 对应的 question、Coverage keys、Source
Types、Fact/Unknown/Conflict refs 和 reason，不包含直接 Tool Action。Phase 5 将其转换为
scoped Need Plan。

---

## 9. Graph、Artifact 与 Resume 集成

### 9.1 Investigation observation

将现有：

```text
execute_capability -> observe_progress(count based) -> OBSERVED
```

替换为：

```text
execute_capability
  -> build_knowledge
  -> save KNOWLEDGE_BUNDLE
  -> project Coverage/progress
  -> checkpoint OBSERVED
```

snapshot v3 的 `required_artifacts` 增加 `knowledge` 前缀，并保存：

- `knowledge_artifact_key/hash/type/generation/request_hash`；
- `knowledge_bundle_id`；
- `knowledge_fingerprint` 与 `progress_fingerprint`；
- append-only fact/unknown/conflict ID 摘要；
- Coverage 状态。

### 9.2 Crash windows

| 崩溃点 | 恢复行为 |
| --- | --- |
| Evidence ACK 前 | Ledger 按既有规则 reconcile/replay Capability |
| Evidence ACK 后、Knowledge 前 | 从 durable Capability outcome/Evidence 重建同一 Knowledge |
| Knowledge ACK 后、`OBSERVED` 前 | hydrate Artifact，补 checkpoint，不重调 Capability/classifier |
| `OBSERVED` 后 | hydrate Knowledge，进入下一轮/finish |
| Grounding Artifact 后、checkpoint 前 | hydrate report，补 checkpoint，不重分类 |

Artifact 的 `request_hash` 必须绑定 Need Plan、Action Signature、prior Knowledge fingerprint、
builder/classifier version 和 source authority；任一不一致 fail closed。

### 9.3 Legacy compatibility

- v1/v2 snapshot 不伪造 Knowledge；旧 workflow 保持原 reader；
- v4 snapshot 如果处于 `OBSERVED` 却缺 Knowledge identity，拒绝恢复；
- rollout 期间 `grounding-report.v1` 只读，v4 producer 只写 v2；
- Go Artifact API 无需理解 Fact 内容，只验证 identity/hash/size 并持久化。

---

## 10. 实现顺序

每一步独立可测试，后一项不得越过前一项。

### 10.1 Step 4.0：Characterization 与合同冻结

修改/新增：

```text
agent-python/tests/characterization/test_evidence_count_coverage.py
agent-python/tests/characterization/test_ref_presence_grounding.py
agent-python/tests/characterization/test_supplement_ref_fanout.py
contracts/proto/agent/v1/agent_execution.proto
backend-go/internal/runcontrol/model.go
agent-python/agent/context.py
```

工作：

- 固定三个当前错误行为的 characterization，明确后续测试将反转断言；
- expand-only 增加 SourceAuthority/Evidence provenance；
- 更新 Go/Python generated contracts；
- Control Plane 填充允许来源，Capability Gateway 填充 Evidence provenance；
- 验证 legacy 字段编号和 reader 未改变。

Gate：v4 每条 Evidence 都有匹配 authority；v1 contract 测试不变。

### 10.2 Step 4.1：Knowledge domain 与稳定 identity

新增 `knowledge/models.py`、`identity.py`、`artifact.py`：

- 定义 v1 模型与 strict validation；
- 实现 canonical JSON、稳定 ID、排序和 fingerprint；
- Artifact round-trip、hash/replay 测试；
- 禁止随机 UUID、NaN、无序 mapping 与未知 enum。

Gate：相同输入跨进程得到相同 IDs/fingerprint/hash。

### 10.3 Step 4.2：Source validation 与 normalization

新增 `source_validator.py`、`normalizer.py`：

- 校验 GitHub/Feishu authority、revision、access、locator 与 hash；
- `HIT` 形成 `EvidenceRecord`；
- `EMPTY/PARTIAL/ACCESS_BLOCKED/TOOL_FAILED` 形成 Unknown；
- invalid/stale Evidence 保留审计 finding，但不进入可支持 Fact 集合；
- 限制 excerpt 数量、单条大小与 Artifact 总大小。

Gate：stale、越权、locator/hash 错误全部 fail closed；EMPTY 无负面 Fact。

### 10.4 Step 4.3：Fact extraction、verification 与 Conflict

新增 `fact_extractor.py`、`fact_verifier.py`、`conflict_detector.py`：

- 移植 OpenAPI/schema 确定性 parser；
- 给 Fact 设置显式 type/scope/status/extractor version；
- 对普通搜索结果仅保留 Evidence 或候选 Fact，不自动 CODE_VERIFIED；
- 构造 deterministic Conflict；
- 可选 bounded classifier 经 Ledger 调用。

Gate：INFERRED 不可升级；open Conflict 覆盖 Supported verdict。

### 10.5 Step 4.4：Coverage 与 Knowledge Graph 接入

新增 `coverage.py`、`module.py`，修改 runtime/snapshot/state：

- 在每次 Capability 后调用 `EvidenceKnowledgeModule.build()`；
- 保存 `KNOWLEDGE_BUNDLE` 后更新 Coverage；
- 用 `progress_fingerprint` 决定 no-progress reset；
- 删除 count-based Coverage producer；
- 完成 crash/recovery matrix。

Gate：仅相关 Evidence 不改变 Coverage、不重置 no-progress。

### 10.6 Step 4.5：Claim Grounding v2

深化 `grounding/`，修改 `graph/advanced.py`：

- `GroundingFinding` 增加 Fact refs、Claim kind/criticality 与 disposition；
- 实现 scope matrix、Conflict 优先级和 bounded classifier；
- 保存 `GROUNDING_REPORT v2`；
- 删除 ref-presence policy；
- 删除 Supplement ref fan-out；Grounding 只返回 scoped request。

Gate：所有 blocking CURRENT_STATE Claim 都由合格 Fact 支持，否则降级/补采/人工确认。

### 10.7 Step 4.6：Eval 与 legacy producer 清理

- 扩展 trace：Evidence、valid Evidence、Fact、Unknown、Conflict、Grounding verdict、Coverage；
- 增加 Evidence Precision、Verified Fact Accuracy、Unsupported Claim Rate、Critical Unknown
  Recall 与 Conflict Detection；
- 对比 v1、v4 Need-only、v4 Knowledge+Grounding；
- 确认 gate 后删除 v4 路径中的旧 producer，保留 legacy reader。

---

## 11. 单元、集成与恢复测试方案

### 11.1 Source validation

- repository binding 正确、commit 错误 → `STALE_SOURCE`；
- binding/source/access scope 不匹配 → `INVALID_SOURCE/ACCESS_SCOPE_MISMATCH`；
- locator 超出 path/line 范围 → `LOCATOR_MISMATCH`；
- excerpt hash 或重新读取内容不一致 → `HASH_MISMATCH`；
- 历史 PRD section 不属于指定 source revision → `STALE_SOURCE`；
- legacy Evidence 只允许 v1 reader，v4 build fail closed。

### 11.2 Fact、Unknown 与 Conflict

- OpenAPI field 形成稳定 `CODE_VERIFIED/CURRENT_STATE` Fact；
- schema column 形成稳定 Fact；
- 普通相似代码命中不自动形成 CODE_VERIFIED；
- `EMPTY` → `EMPTY_RESULT` Unknown，facts 为空；
- `ACCESS_BLOCKED/TOOL_FAILED` → 对应 Unknown；
- 相同 subject/predicate/value 去重；
- 不同确定性 value → open Conflict，涉及 Fact 为 `CONFLICTING`；
- replay 顺序改变不影响 ID/Artifact hash；
- classifier 试图把 INFERRED 改为 CODE_VERIFIED → schema/policy 拒绝。

### 11.3 Coverage

- non-empty related Evidence、零 Supported Fact → 保持 `MISSING`；
- 部分所需 Fact → `PARTIAL`；
- 全部 blocking Fact → `COVERED`；
- open Conflict → `CONFLICTING`；
- 新 unsupported-only Evidence → knowledge fingerprint 变化，progress fingerprint 不变；
- 新 Unknown 首次记录可改变 progress fingerprint，重复同一 Unknown 不改变；
- Phase 4 不能删除 Phase 3 Coverage key 或降低 blocking 属性。

### 11.4 Grounding

- ref 存在但没有 Fact → `UNSUPPORTED/FACT_REQUIRED`；
- 相关但不蕴含 Claim → `UNSUPPORTED/EVIDENCE_ONLY_RELATED`；
- historical Fact 支持 CURRENT_STATE Claim → `FACT_SCOPE_MISMATCH`；
- INFERRED Fact 支持 CURRENT_STATE → 拒绝；
- open Conflict → `CONFLICTING`；
- TARGET_DECISION 无 decision ID → `HUMAN_CONFIRMATION_REQUIRED`；
- ASSUMPTION 无授权 ID → 不得 pass；
- informational recommendation 正确标记 → `NOT_REQUIRED`；
- unsupported blocking Claim 只产生 scoped SupplementScope，不调用 Capability；
- 一个 Claim 的 Evidence 不自动附到另一个 Claim。

### 11.5 Ledger 与 crash recovery

- Knowledge Artifact ACK 前崩溃 → 重建相同 Artifact；
- Artifact ACK 后 checkpoint 前崩溃 → 零 Capability/classifier 物理调用；
- Grounding Artifact ACK 后崩溃 → 零重复分类；
- request hash 与 source revision 不一致 → fail closed；
- snapshot `OBSERVED` 缺 Knowledge Artifact → 拒绝；
- replay 不重复预算消费。

### 11.6 Eval Gate

- 非空 Retrieval Hit 不再自动完成 Coverage；
- blocking CURRENT_STATE Claim finding 覆盖率 100%；
- Unsupported/Conflicting Claim 不静默通过；
- v4 Unsupported Claim Rate 不高于 v1；
- Critical Unknown Recall 不低于 v1；
- Verified Fact Accuracy 与 Evidence Precision 不低于 baseline；
- 固定 crash matrix 的重复物理调用数为 0。

---

## 12. 最小提交序列

1. `test: characterize evidence count and reference grounding gaps`
2. `feat: extend source authority and evidence provenance contracts`
3. `feat: define stable knowledge bundle models and artifacts`
4. `feat: validate evidence identity access and source revisions`
5. `feat: extract facts unknowns and source conflicts`
6. `feat: build and checkpoint knowledge after capabilities`
7. `feat: ground claims through verified facts`
8. `fix: remove evidence-count coverage and supplement ref fanout`
9. `eval: measure knowledge grounding and unknown quality`

每个提交都必须通过 Agent Python tests；涉及 proto/Go 的提交同时通过 generated contract
一致性、`go test ./...` 和 `go vet ./...`。不得在同一提交同时引入合同、切换 producer 和
删除 legacy reader。

---

## 13. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 严格校验后 Coverage 明显下降 | 这是暴露真实 Unknown；通过 Phase 5 Replan/Supplement 改善，不放宽 Fact |
| 语义分类增加 Token/延迟 | 确定性 parser 优先；只分类 blocking Claim；稳定 Ledger replay |
| 合同扩展影响旧 Worker | expand-only 字段、workflow version 路由、dual-read、先 deploy reader |
| Artifact 过大 | excerpt 条数/长度/总大小上限；只保留最小支持 span |
| 同一 Fact 因格式差异重复 | canonical value、稳定 extractor version、排序去重 |
| Conflict 误报 | 仅比较相同 scope 的确定性 subject/predicate；保留来源与人工 disposition |
| 模型越权改 Fact | strict output + deterministic upper bound；模型只给 support verdict |
| Knowledge 成为第二权威 | 有界 retention，无长期 Fact table；原始内容仍由 Feishu/GitHub 拥有 |

---

## 14. Phase 5 交接合同

Phase 5 只能依赖以下公开输出：

- `InformationNeedPlan`；
- `EvidenceKnowledgeModule.build(...) -> KnowledgeBuildResult`；
- `KnowledgeBundle` Artifact identity；
- verdict-based Coverage；
- `progress_fingerprint`；
- `GroundingReport.supplement_scope`；
- 统一 reason codes。

Phase 5 不得读取 Fact extractor 内部状态、直接拼装 Evidence refs、重新解释 excerpt 或
绕过 Knowledge Module 更新 Coverage。满足该边界后，Initial/Replan/Supplement 才能
共享同一 Investigation Implementation，而不会共享错误的“命中即完成”语义。

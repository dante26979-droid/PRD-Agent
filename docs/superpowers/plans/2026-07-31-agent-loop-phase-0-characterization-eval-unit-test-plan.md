# Agent Loop Phase 0：Characterization 与 Eval 单元测试方案

> 状态：IMPLEMENTED / LOCALLY VERIFIED
> 日期：2026-07-31
> 对应实现方案：[`Phase 0 Characterization 与 Eval 基线实现方案`](./2026-07-31-agent-loop-phase-0-characterization-eval-design.md)
> 上位计划：[`Agent Loop 语义深化实施计划`](./2026-07-31-agent-loop-semantic-deepening-implementation-plan.md)
> 测试框架：`pytest` + 标准库 fake；不访问网络，不要求 PostgreSQL

---

## 0. 测试目标

本方案验证 Phase 0 的 Eval 基础设施，而不是证明当前 Agent 产品语义正确。

测试必须同时覆盖：

1. Trace Schema 可重复、严格、可向后扩展；
2. Eval Adapter 能从现有事件和 checkpoint 无损形成 Trace；
3. 生产 `LangGraphAgentLoop` 能通过固定 Scripted Adapter 进入现有 Eval Runner；
4. 指标计算准确区分 measured、diagnostic 和 not-applicable；
5. 报告默认脱敏；
6. 当前五类已知限制被 Characterization Sentinel 明确记录；
7. 旧 Eval Variant 不回归。

测试不应：

- 断言私有函数调用顺序；
- 访问 DeepSeek、GitHub、Feishu 或 PostgreSQL；
- 把 Scripted Action 的命中率解释为模型质量；
- 永久保护当前错误语义；
- 将完整 Draft、Evidence 或 Prompt 写入 snapshot 文件。

---

## 1. 测试层次

| 层次 | 范围 | 主要文件 |
| --- | --- | --- |
| L0 | Trace 数据模型和验证 | `tests/eval/test_agent_trace.py` |
| L1 | Eval Adapter event folding | `agent-python/tests/test_eval_adapter.py` |
| L2 | LangGraph Runtime Baseline | `tests/eval/test_langgraph_runtime.py` |
| L3 | Operational Metrics | `tests/eval/test_agent_operational_metrics.py` |
| L4 | Report redaction | `tests/eval/test_report_redaction.py` |
| L5 | Known-limit Characterization | `agent-python/tests/characterization/` |
| L6 | CLI/legacy regression | `tests/eval/test_cli.py`、现有 Eval tests |

L0～L4 是目标行为测试，后续阶段持续保留。L5 是迁移 Sentinel；对应 Phase 修复后
必须替换为目标行为测试。

---

## 2. 通用 Fixture

### 2.1 Identity

```python
TRACE_IDENTITY = {
    "eval_run_id": "eval-run-case-006-1",
    "case_id": "case-006",
    "workflow_version": "agent-runtime.v1",
    "input_hash": "sha256:" + "a" * 64,
}
```

所有测试使用固定时间和固定 hash；禁止调用真实时钟后断言精确值。

### 2.2 Scripted Model

提供：

- `ActionThenDraftModel`：一轮 Action 后返回 Draft；
- `TwoActionThenDraftModel`：两轮不同 Action；
- `DuplicateActionModel`：重复相同 Action；
- `StructuredClaimModel`：返回 CURRENT_STATE Claim；
- `RepairModel`：触发/完成一次 Quality Repair；
- `FailingModel`：返回传输错误或非法 JSON。

每个 fake 记录：request hash、operation、physical call count、token usage 和 latency
fixture；不记录或暴露 Prompt 正文。

### 2.3 Instrumented Gateway

提供：

- non-empty repository hit；
- empty result；
- related-but-not-supporting hit；
- permission denied；
- deterministic timeout；
- replay receipt。

Gateway 记录 action signature hash 和 physical call count，不把 query/path/excerpt 写入 Trace。

### 2.4 Checkpoint Fixture

从真实 `CheckpointCodec` 生成：

- `ACTION_VALIDATED`；
- `OBSERVED`；
- `INVESTIGATION_FINISHED`；
- `DRAFTED`；
- `GROUNDED`；
- `READY_TO_SUBMIT`。

禁止手写无法被生产 Codec 解码的伪 checkpoint，除非测试目标就是非法 payload。

### 2.5 Sensitive Marker Fixture

Draft、Evidence、Prompt 和 Error 分别包含唯一 marker：

```text
DRAFT_SECRET_MARKER_9d2f
EVIDENCE_SECRET_MARKER_31aa
PROMPT_SECRET_MARKER_b819
ERROR_SECRET_MARKER_6c70
```

Report redaction 测试必须验证这些 marker 均不出现在 Markdown 和 JSON。

---

## 3. L0：Trace Schema 测试

文件：`tests/eval/test_agent_trace.py`

### TRACE-001：最小合法 Trace 可 round-trip

Given：只有 identity、status、空事件集合和 counters 的 completed Trace。
When：`as_dict -> JSON -> from_dict`。
Then：对象相等；时间为 ISO-8601；tuple 顺序保持。

### TRACE-002：未知可选字段不破坏读取

在顶层和子 Trace 注入未来字段。`from_dict` 应忽略未知字段，但 `as_dict` 不回写未知值。

### TRACE-003：未知 schema version 被拒绝

`agent-loop-trace.v999` 必须抛出明确 `ValueError`，不能按 v1 猜测。

### TRACE-004：身份字段缺失被拒绝

分别删除 eval run、case、workflow version、input hash，均失败。

### TRACE-005：负数计数被拒绝

Model Attempt、Capability、Token、duration、checkpoint bytes 任一为负均失败。

### TRACE-006：Attempt 状态机合法

允许：

```text
PLANNED -> SUCCEEDED
PLANNED -> FAILED
```

拒绝：terminal 后再次 terminal、SUCCEEDED 无 PLANNED、同 key request hash 漂移。

Characterization 的 direct-call local mode 如果历史事件缺少 PLANNED，必须显式标记
`legacy_incomplete=true`，不能静默伪造 PLANNED。

### TRACE-007：Coverage transition 合法

状态只允许 `MISSING/PARTIAL/COVERED/CONFLICTING`；相同 before/after 不应被记录为 transition。

### TRACE-008：None 与 0 不混淆

v1 缺少 Fact 层时 `fact_delta=None`；已测量但无新增 Fact 时为 `0`。序列化后保持区别。

### TRACE-009：Failure 仅接受分类

FailureTrace 允许 category、retryable 和 public code；拒绝 stack、raw_message、prompt 等字段。

### TRACE-010：Trace JSON 确定性

相同对象多次序列化 byte-for-byte 相同，用于稳定 output hash。

---

## 4. L1：Eval Adapter 测试

文件：`agent-python/tests/test_eval_adapter.py`

### ADAPTER-001：Model Attempt fold

输入 PLANNED + SUCCEEDED 两个事件，输出一个 terminal ModelAttemptTrace；Token 和 latency
来自 SUCCEEDED；physical call count 来自 Instrumented Model。

### ADAPTER-002：失败 Attempt fold

PLANNED + FAILED 输出失败 Trace，error category 保留，模型原始异常不保留。

### ADAPTER-003：孤立 terminal Attempt 失败

Eval Run 标记 `failed/INVALID_TRACE_SEQUENCE`，不能生成看似成功的报告。

### ADAPTER-004：Capability 与 Evidence 关联

一次 physical Gateway 调用返回两个 Evidence，Trace 的 call count=1、evidence count=2；
Evidence marker 不进入 Trace。

### ADAPTER-005：空 Capability 结果

call count=1、evidence count=0、status=`EMPTY`；不得解释为 permission error 或不存在 Fact。

### ADAPTER-006：Checkpoint 顺序

输入 sequence `1,2,3` 正常；重复、倒序或 gap 是否允许遵循当前合同：

- 重复/倒序失败；
- gap 记录 `sequence_gap=true` 并使 Eval Run 失败，因为本地完整 Trace 不应丢事件。

### ADAPTER-007：Coverage transition 由 checkpoint diff 产生

`MISSING -> COVERED` 生成一个 transition；后续相同 `COVERED` 不重复生成。

### ADAPTER-008：Advanced Artifact 摘要

Artifact Trace 只包含 type、generation、request/content hash、bytes；不含 content。

### ADAPTER-009：Draft result 摘要

从 Draft Patch 提取 schema、result outcome、stop reason、coverage、markdown bytes 和 output
hash；Trace 不含 markdown。

### ADAPTER-010：取消

取消产生 `status=failed`、`category=CANCELLED`，取消后的事件被视为非法尾随事件。

### ADAPTER-011：physical 与 replay 区分

同一 identity 的 replay 不增加 physical call count；Trace 保留 durable replay count。

### ADAPTER-012：Adapter 不修改 Runtime

对同一 scripted input，使用普通 Buffered Sink 与 Trace Adapter 得到相同 Draft hash、
stop reason、Coverage 和 checkpoint status 序列。

---

## 5. L2：LangGraph Runtime Baseline 测试

文件：`tests/eval/test_langgraph_runtime.py`

### BASE-001：input hash 稳定

Hash 包含：case payload、workflow version、prompt version、script fixture version、budget 和
trace schema。trial_no 不进入 input hash，但进入 run ID。

### BASE-002：配置变化改变 hash

分别改变 budget、workflow version、fixture version、prompt version，hash 均变化。

### BASE-003：确定性 run ID

相同 config/case/trial/hash 得到同一 ID；不同 trial 得到不同 ID。

### BASE-004：case-001 direct generation

成功运行，Capability physical count=0；Trace route 标记 `LEGACY_DIRECT_GENERATION`；
Requiredness Precision/Recall 为 not-applicable。

### BASE-005：required case investigation

以 case-006 运行一次 Repository Action；Trace 包含 ACTION_VALIDATED、OBSERVED、
INVESTIGATION_FINISHED、READY_TO_SUBMIT。

### BASE-006：cross-file multi-action

case-010 至少两轮不同 Action；每轮最多一个模型 Action 和一个 Capability；最终 stop reason
和剩余 Coverage 可追踪。

### BASE-007：失败转换

模型非法 JSON、Capability permission denied、checkpoint decode error 分别转换为稳定
error category；`BaselineRun.error` 不保存原始输入。

### BASE-008：completed replay

InMemoryRunStore 已有相同 input hash 的 completed Run 时，不再次调用模型或 Gateway。

### BASE-009：failed run 重试

已有 failed Run 时再次执行；如果 input hash 相同，run ID 相同且新结果覆盖规则遵循现有 Store。

### BASE-010：缺少 agent-python 明确失败

Bridge import 失败时提示安装/PYTHONPATH；不能回退 DirectPromptBaseline。

### BASE-011：metadata 类型化

`AgentEvalMetadata` round-trip 后 Trace counters 不丢失；非 JSON-safe value 被拒绝。

### BASE-012：旧 Baseline 选择不变

Direct、Minimal、Single、Bounded config 仍选择原实现；仅 `langgraph-runtime-*` 进入新 Baseline。

---

## 6. L3：Operational Metrics 测试

文件：`tests/eval/test_agent_operational_metrics.py`

### METRIC-001：Model Attempt 去重计数

PLANNED/SUCCEEDED 事件对计为一个 Attempt，不计为两个。

### METRIC-002：Token 聚合

多个 terminal Attempt 的 total token 求和；缺失 usage 时状态为 partial/measured，不把缺失当 0。

### METRIC-003：Evidence yield

2 次 physical call，新增 3 个唯一 Evidence，yield=1.5；重复 Evidence 不重复计数。

### METRIC-004：Coverage completion

空 Coverage、部分 Coverage、全部 Coverage 分别按照已有约定输出 not-applicable、比例、1.0。

### METRIC-005：Coverage without Fact

两个 COVERED transition：一个 `fact_delta=None`，一个 `fact_delta=1`，诊断比率为 0.5。

### METRIC-006：Reference-only support

只统计 `reason_code=EVIDENCE_REFERENCE_VALID` 的 Supported finding；无 finding 时 not-applicable。

### METRIC-007：Effective Replan

- Replan 后 signature 变化：1.0；
- Replan 后 signature 相同：0.0；
- 无 Replan：not-applicable。

### METRIC-008：Shadow extra calls

off 与 shadow 使用相同 case；差值分别计算 model 和 capability physical calls，不能用事件数代替。

### METRIC-009：Resume duplicate physical calls

同一 stable identity 在 resume 前后 physical 两次时计 1 个重复；durable replay 不计重复。

### METRIC-010：诊断指标标签

`coverage_without_fact_rate` 和 `reference_only_support_rate` 必须带
`measurement_kind=diagnostic_proxy`；Report 不显示为 Ground Truth Accuracy。

### METRIC-011：not-applicable 汇总

全部 not-applicable 时 summary status 不得变成 measured；部分 measured 时只对 measured 值聚合。

### METRIC-012：多 trial 统计

验证 mean/min/max，并增加 variance 或 standard deviation 时使用固定值断言，不依赖浮点近似之外的实现细节。

---

## 7. L4：Report Redaction 测试

文件：`tests/eval/test_report_redaction.py`

### REDACT-001：JSON 不包含 Draft

把 `DRAFT_SECRET_MARKER_9d2f` 放入 `BaselineRun.output`；`report.to_json()` 不得包含。

### REDACT-002：Markdown 不包含 Draft

同上，`to_markdown()` 不得包含 marker。

### REDACT-003：Evidence/Prompt 不泄漏

把 marker 放入 Trace Adapter 的内存输入；最终 Trace/report 不得包含。

### REDACT-004：完整异常不泄漏

异常 message 包含 token/path；报告只输出 `MODEL_INVALID_OUTPUT` 等 category。

### REDACT-005：run details 白名单

逐字段断言只存在：run ID、case、trial、status、hash、duration、measurement mode 和 counters。

### REDACT-006：低基数标识保留

workflow version、prompt version、model ID、stop reason、error category 可保留。

### REDACT-007：敏感键扫描

序列化结果中不得出现：

```text
authorization
bearer
api_key
access_token
refresh_token
private_key
prompt
excerpt
```

键名扫描大小写不敏感；`prompt_version` 是允许例外，使用显式 allowlist。

### REDACT-008：deterministic 标记

Scripted 报告必须包含 `deterministic_only=true` 和
`measurement_mode=scripted_characterization`。

### REDACT-009：远程结果不能伪装 deterministic

execution mode 为 remote 时 `deterministic_only=false`；缺失 mode 使报告构建失败。

### REDACT-010：报告稳定性

相同 runs 不因 dict 插入顺序产生不同 JSON；便于 diff。

---

## 8. L5：Known-limit Characterization

目录：`agent-python/tests/characterization/`

这些测试描述当前行为，测试名、docstring 和 marker 必须表明它们是待替换限制。

### CHAR-001：任意非空 Evidence 推进 Coverage

文件：`test_current_coverage_behavior.py`

构造与 active gap 无语义支持关系的 hit；当前预期仍为 `COVERED`。同时断言 Trace：

- evidence_delta=1；
- fact_delta=None；
- `coverage_without_fact_rate=1.0`。

Phase 4 替换目标：Coverage 保持 MISSING/PARTIAL。

### CHAR-002：Evidence ref presence 通过 Grounding

文件：`test_current_grounding_behavior.py`

Claim statement 与 Evidence excerpt 不相干，但 ref 合法存在；当前预期
`GroundingStatus.SUPPORTED/EVIDENCE_REFERENCE_VALID`。

Phase 4 替换目标：`UNSUPPORTED/EVIDENCE_ONLY_RELATED`。

### CHAR-003：Supplement 批量附加 ref

多个 unsupported Claim，只补查一个 statement；当前实现可能把全部新 refs 附加到多个
unsupported Claim。Trace 记录 affected claim count 与 query count。

Phase 5 替换目标：只更新 scoped Claim/Unit 并重新经过 Knowledge。

### CHAR-004：Replan 计数不代表策略变化

文件：`test_current_need_behavior.py`

重复 Action 使 `replan_count=1`，但没有独立 Replan operation/context；
`replan_effective_change_rate=0.0`。

Phase 5 替换目标：signature 变化或明确停止。

### CHAR-005：缺少正式 Information Need

纯新增 case 直接进入模型生成；Trace route 为 `LEGACY_DIRECT_GENERATION`，没有
Need Artifact。不得断言 requiredness=`NONE`。

Phase 3 替换目标：持久化 `NONE` Plan。

### CHAR-006：Shadow 可产生额外工作

文件：`test_current_shadow_behavior.py`

构造会触发 Supplement 或 Repair 的 Advanced State，对比 off/shadow physical call；记录当前
差值。若特定路径当前恰好为 0，测试必须选择确实会触发调用的 fixture，不能写空洞断言。

Phase 2 替换目标：差值恒为 0。

### CHAR-007：Resume 只校验部分状态不变量

文件：`test_current_resume_behavior.py`

分别构造当前可能未被拒绝的 Repository revision/append-only 集合漂移，并记录实际行为。
不得使用会造成跨租户或真实数据访问的输入。

Phase 1 替换目标：全部 `CHECKPOINT_INCOMPATIBLE`。

### CHAR-008：当前 Quality 检查面较窄

构造包含不可执行验收标准但非空、非重复的 Unit；当前可能 `QUALITY_PASSED`，Trace 标记
`quality_rule_set=legacy_minimal`。

Phase 6 替换目标：`AMBIGUOUS_ACCEPTANCE_CRITERION`。

---

## 9. L6：CLI 与 Legacy Regression

### CLI-001：新 config 路由

`langgraph-runtime-v1-characterization` 选择新 Baseline。

### CLI-002：旧 config 路由

四个已有 config 的 Baseline 类型不变。

### CLI-003：输出文件名稳定

生成 `<config_id>.json/.md`；非法 config ID 被拒绝，避免路径穿越。

### CLI-004：失败退出码

任一 Run failed 时 CLI 返回 1；全 completed 返回 0。

### CLI-005：默认报告脱敏

CLI 无需 flag 即使用 sanitized 模式。

### REG-001：Dataset 不变

10 个 Case、dataset version、Repository commit 和 Ground Truth hash 与 Phase 0 前一致。

### REG-002：旧报告指标不丢失

Direct/Minimal/Single/Bounded 报告继续包含已有产品指标；新增字段为兼容扩展。

### REG-003：旧 Store replay 不变

InMemory/Postgres store 对 completed/failed 的重放规则不因 Trace 引入改变。

---

## 10. 测试实施顺序

按测试先行顺序：

1. TRACE-001～010；
2. REDACT-001～010；
3. ADAPTER-001～012；
4. BASE-001～012；
5. METRIC-001～012；
6. CLI/REG；
7. CHAR-001～008；
8. 运行 10 Case Eval smoke；
9. 运行完整回归。

前六组定义长期正确行为；Characterization 最后添加，避免错误行为成为新 Module 的设计输入。

---

## 11. 测试命令

快速单元测试：

```bash
PYTHONPATH=agent-python venv/bin/python -m pytest -q \
  agent-python/tests/test_eval_adapter.py
PYTHONPATH=src:agent-python venv/bin/python -m pytest -q \
  tests/eval/test_agent_trace.py \
  tests/eval/test_langgraph_runtime.py \
  tests/eval/test_agent_operational_metrics.py \
  tests/eval/test_report_redaction.py
```

Characterization：

```bash
PYTHONPATH=agent-python venv/bin/python -m pytest -q \
  agent-python/tests/characterization
```

全部 Agent/Eval：

```bash
PYTHONPATH=agent-python venv/bin/python -m pytest -q agent-python/tests
PYTHONPATH=src:agent-python venv/bin/python -m pytest -q tests/eval
```

两个目录都定义了顶层 `tests` package，因此必须分两个 pytest 进程收集，避免模块名冲突。

固定 Eval smoke：

```bash
PYTHONPATH=src:agent-python venv/bin/python -m prd_agent.eval run-baseline \
  --manifest eval/cases/manifest.json \
  --config eval/configs/langgraph_v1_characterization.json \
  --output-dir eval/reports
```

脱敏扫描：

```bash
rg -n -i 'authorization:|bearer |api_key|access_token|refresh_token|private_key' \
  eval/reports
rg -n 'DRAFT_SECRET_MARKER|EVIDENCE_SECRET_MARKER|PROMPT_SECRET_MARKER|ERROR_SECRET_MARKER' \
  eval/reports
```

两次 `rg` 均应无输出。

---

## 12. 覆盖率与质量 Gate

### 12.1 必须 100% 分支覆盖的 Module

- Trace `from_dict` validation；
- event folding 状态机；
- report redaction allowlist；
- metric measured/not-applicable 路由；
- CLI baseline selection。

不要求整个仓库达到统一覆盖率百分比；本阶段新增安全/协议 Module 的分支必须完整。

### 12.2 Mutation-sensitive 规则

以下断言必须能杀死明显 mutation：

- attempt key/hash 漂移被拒绝；
- token 缺失不当成 0；
- replay 不增加 physical count；
- `None` fact delta 不当成 0；
- output/excerpt/prompt/error marker 不进入报告；
- Requiredness legacy route 不冒充 NONE；
- unknown trace schema 不兼容。

### 12.3 Flake Gate

所有 Phase 0 测试连续运行 20 次：

- 不依赖 wall clock 排序；
- 不依赖随机 UUID；
- 不访问网络；
- 不产生 Repository 内未跟踪报告；
- JSON/hash byte-for-byte 稳定。

---

## 13. 完成清单

- [ ] TRACE-001～010 通过
- [ ] ADAPTER-001～012 通过
- [ ] BASE-001～012 通过
- [ ] METRIC-001～012 通过
- [ ] REDACT-001～010 通过
- [ ] CHAR-001～008 通过并带迁移说明
- [ ] CLI-001～005、REG-001～003 通过
- [x] 现有 Agent Python 测试无回归（本次共 65 个）
- [x] 现有 Eval 测试无回归（本次共 63 个，含新增测试）
- [x] 10 个固定 Case 的 characterization run 全部形成 Trace
- [x] 报告敏感 marker 扫描无命中
- [x] Scripted 报告明确标记 deterministic-only
- [ ] Phase 0 新增协议/脱敏 Module 分支覆盖完整
- [x] 连续 20 次运行无 flake
- [x] 没有修改生产 Agent Graph、Go Control Plane 或 RPC 合同语义

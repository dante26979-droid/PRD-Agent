# M0 Agent Core 第六步单元测试与集成门禁计划

> 文档状态：待实现  
> 版本：0.1  
> 日期：2026-07-23  
> 对应设计：[M0 Agent Core 第六步设计方案：完整 PRD Workflow](./2026-07-23-m0-agent-core-step-6-complete-prd-workflow-design.md)  
> 基线：Step 1～4 `82 passed`；Step 5 代码完成后更新进入 Step 6 的真实基线

## 1. 目的

本文档将 Step 6 设计拆成可执行的单元测试、合同测试、工作流集成测试和真实 PostgreSQL 门禁。测试重点是证明：

- 动态 Outline/Unit Plan 合法。
- 一次只生成和确认一个 Unit。
- 未确认或失效内容不会进入上下文和最终文档。
- Grounding 与 Quality 门禁不能被跳过。
- 已确认 Section 不被原地修改。
- 全文问题只能经用户批准的 Revision Plan 修订。
- 最终确认绑定精确版本和内容哈希。
- 并发、重试和崩溃不会产生半完成状态。

## 2. 测试策略

### 2.1 测试金字塔

| 层 | 范围 | 默认依赖 |
| --- | --- | --- |
| 纯单元测试 | Model、Policy、Scheduler、Context、Checks、Renderer | 无数据库、无网络、无真实模型 |
| 服务测试 | Workflow/Quality/Revision/Finalize 编排 | Memory Store + Scripted Model |
| 合同测试 | Memory/PostgreSQL Store 行为一致 | 参数化 Store Factory |
| PostgreSQL 集成 | 约束、事务、并发、恢复 | 真实 PostgreSQL |
| Eval/E2E | 完整多 Unit PRD | 固定 Dataset + Scripted User/Checker |

### 2.2 原则

1. Policy 测试不 Mock 被测 Policy。
2. Renderer 使用 Golden + 属性断言，不只做 Snapshot。
3. 时间和 ID 使用 Fake Clock/ID Factory。
4. 模型使用 Scripted Adapter，普通测试不依赖网络。
5. 每个命令测试成功、非法状态、版本冲突和幂等重放。
6. 每个状态推进同时断言实体、事件、Checkpoint 和持久化。
7. 真实 PostgreSQL 门禁不得用 SQLite 或 Memory 代替。

## 3. 建议测试目录

```text
tests/domain/
  test_outline_plan_models.py
  test_unit_models.py
  test_section_document_models.py
  test_quality_models.py

tests/policies/
  test_outline_plan_policy.py
  test_unit_scheduling_policy.py
  test_unit_confirmation_policy.py
  test_quality_policy.py
  test_revision_policy.py
  test_finalization_policy.py

tests/workflow/
  test_dynamic_outline.py
  test_multi_unit_generation.py
  test_unit_context_builder.py
  test_unit_versioning.py
  test_quality_workflow.py
  test_revision_workflow.py
  test_finalization.py
  test_complete_workflow_recovery.py

tests/quality/
  test_deterministic_checks.py
  test_semantic_checker_contract.py

tests/rendering/
  test_document_renderer.py
  test_evidence_appendix.py

tests/storage/
  test_workflow_store_contract.py
  test_postgres_complete_workflow_integration.py

tests/eval/
  test_complete_workflow.py
  test_complete_workflow_metrics.py
```

## 4. Outline Plan 模型单测

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S6-OP-001 | 合法三级 Outline | 构造成功，顺序稳定 | P0 |
| S6-OP-002 | 节点超过三级 | 校验失败 | P0 |
| S6-OP-003 | `node_key` 重复 | 校验失败 | P0 |
| S6-OP-004 | 父节点不存在 | 校验失败 | P0 |
| S6-OP-005 | 节点引用自己为父 | 校验失败 | P0 |
| S6-OP-006 | 同级 sequence 重复 | 校验失败 | P0 |
| S6-OP-007 | 同级 sequence 有空洞 | 归一化或明确失败 | P1 |
| S6-OP-008 | 标题或 purpose 为空 | 校验失败 | P0 |
| S6-OP-009 | Unit key 重复 | 校验失败 | P0 |
| S6-OP-010 | Unit 没有节点 | 校验失败 | P0 |
| S6-OP-011 | Outline Node 未映射 Unit | 校验失败 | P0 |
| S6-OP-012 | 同一 Node 映射两个 Unit | 校验失败 | P0 |
| S6-OP-013 | Unit 引用不存在 Node | 校验失败 | P0 |
| S6-OP-014 | Unit 自依赖 | 校验失败 | P0 |
| S6-OP-015 | Unit 依赖形成环 | 校验失败并报告环 | P0 |
| S6-OP-016 | Unit 顺序不是合法拓扑序 | 校验失败 | P0 |
| S6-OP-017 | 5～12 个 Unit | 正常接受 | P1 |
| S6-OP-018 | 小需求少于 5 个且有理由 | 正常接受 | P1 |
| S6-OP-019 | 少于 5 个但无规模理由 | Policy Warning/Error | P1 |
| S6-OP-020 | 超过 15 个 Unit | 硬拒绝，不截断 Node | P0 |
| S6-OP-021 | 13～15 个 Unit | 接受并产生规模 Warning | P2 |
| S6-OP-022 | Unknown extra field | Schema 拒绝 | P1 |

## 5. Outline 锁定与版本单测

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S6-OV-001 | Pending Outline 可被用户确认 | 状态变 `CONFIRMED` | P0 |
| S6-OV-002 | Draft Outline 不能直接生成 Unit | `InvalidTransition` | P0 |
| S6-OV-003 | 已确认 Outline 再次被 Agent 修改 | 拒绝 | P0 |
| S6-OV-004 | 用户调整 Pending Outline | 创建新版本，旧版 `SUPERSEDED` | P0 |
| S6-OV-005 | 确认旧 Outline Version | `VersionConflict` | P0 |
| S6-OV-006 | 相同确认命令幂等重放 | 返回原快照 | P0 |
| S6-OV-007 | 相同幂等键不同 Outline | `IdempotencyConflict` | P0 |
| S6-OV-008 | Outline 锁定与 Unit Plan 非原子 | 故障时整体回滚 | P0 |
| S6-OV-009 | 锁定时 Node/Unit Plan 已失效 | 拒绝 | P0 |
| S6-OV-010 | Outline Version 递增 | 严格单调，无覆盖 | P1 |

## 6. Unit Scheduler 单测

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S6-US-001 | 第一 Unit 无依赖 | 被选为唯一 Ready Unit | P0 |
| S6-US-002 | 前一 Unit 未确认 | 下一 Unit 不 Ready | P0 |
| S6-US-003 | 所有依赖已确认 | 最小 sequence Unit Ready | P0 |
| S6-US-004 | 依赖 Unit Failed | 消费 Unit 不 Ready | P0 |
| S6-US-005 | 依赖 Section Invalidated | 消费 Unit 不 Ready | P0 |
| S6-US-006 | 两个理论可并行 Unit | Portfolio Core 选最小 sequence | P1 |
| S6-US-007 | 当前已有 Active Unit | 不选择第二个 | P0 |
| S6-US-008 | Task 非 `GENERATING` | 无 Ready Unit | P0 |
| S6-US-009 | Outline 未确认 | 无 Ready Unit | P0 |
| S6-US-010 | Unit 为 `REVISION_REQUIRED` 且依赖有效 | 可重新 Ready | P0 |
| S6-US-011 | 所有 Unit Confirmed | 返回 All Units Complete | P0 |
| S6-US-012 | 数据损坏导致两个 Active Unit | Policy Error，不随机选择 | P0 |
| S6-US-013 | sequence 缺失 | Policy Error | P1 |
| S6-US-014 | Unit 属于旧 Outline | 不可调度 | P0 |

## 7. Unit 状态机与确认单测

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S6-UT-001 | PENDING → PREPARING | 允许 | P0 |
| S6-UT-002 | PREPARING → INVESTIGATING | 有 Required Need 时允许 | P0 |
| S6-UT-003 | PREPARING → GENERATING | Need 为 NONE 时允许 | P0 |
| S6-UT-004 | INVESTIGATING → GENERATING | Investigation 合法终态后允许 | P0 |
| S6-UT-005 | GENERATING → PENDING_CONFIRMATION | Grounding/Quality 门禁通过 | P0 |
| S6-UT-006 | Grounding 未完成 | 禁止进入 Pending Confirmation | P0 |
| S6-UT-007 | Critical Unsupported Claim | 禁止进入 Pending Confirmation | P0 |
| S6-UT-008 | Unit Quality 有 Blocker | 禁止进入 Pending Confirmation | P0 |
| S6-UT-009 | Pending Confirmation → Confirmed | 版本/hash/check IDs 全匹配 | P0 |
| S6-UT-010 | 确认非当前 Unit | 拒绝 | P0 |
| S6-UT-011 | 确认旧 Unit Version | `VersionConflict` | P0 |
| S6-UT-012 | 确认旧 Draft hash | `VersionConflict` | P0 |
| S6-UT-013 | Grounding Run ID 不匹配 | 拒绝 | P0 |
| S6-UT-014 | Quality Run ID 不匹配 | 拒绝 | P0 |
| S6-UT-015 | Unit Content 为空 | 拒绝 | P0 |
| S6-UT-016 | 用户要求修改 Pending Draft | 创建新 Draft Version | P0 |
| S6-UT-017 | 旧 Draft 不能再确认 | 拒绝 | P0 |
| S6-UT-018 | Confirmed Unit 原地修改 | 拒绝 | P0 |
| S6-UT-019 | Confirmed → Revision Required | 仅有批准的 Revision Plan 时允许 | P0 |
| S6-UT-020 | 非法状态边 | `InvalidTransition` | P0 |
| S6-UT-021 | Model/Checker 失败 | Unit `FAILED`，内容可审计 | P1 |
| S6-UT-022 | Human Input Required | 不自动推进下一 Unit | P0 |

## 8. Context Builder 单测

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S6-CX-001 | 无上游的第一 Unit | 只含 Brief/Outline/当前 Unit | P0 |
| S6-CX-002 | 上游已确认 | 包含最新有效 Section 摘要 | P0 |
| S6-CX-003 | 上游只有 Pending Draft | 不进入 Context | P0 |
| S6-CX-004 | 上游 Section 已 Invalidated | 不进入 Context | P0 |
| S6-CX-005 | 同一 Node 有旧/新 Confirmed 历史版本 | 只选择当前有效版本 | P0 |
| S6-CX-006 | 非依赖 Unit 内容 | 默认不进入 Context | P1 |
| S6-CX-007 | 全局术语/角色/范围约束 | 最小必要字段进入 | P1 |
| S6-CX-008 | Grounded Fact IDs | 仅传 ID 和允许摘要 | P0 |
| S6-CX-009 | Unsupported/Stale Fact | 不进入可用事实集 | P0 |
| S6-CX-010 | 其他 Task Section | 权限拒绝 | P0 |
| S6-CX-011 | 完整 Evidence/源码意外传入 | Schema/Builder 拒绝 | P0 |
| S6-CX-012 | 相同输入 | context hash 完全相同 | P1 |
| S6-CX-013 | 上游 Section Version 变化 | context hash 变化 | P0 |
| S6-CX-014 | 保存实际 Dependency Snapshot | producer version/hash 正确 | P0 |
| S6-CX-015 | Revision Instruction | 只传用户批准的 Item | P0 |
| S6-CX-016 | 模型私有推理字段 | 不进入 Context | P0 |

## 9. Draft 与 Section Version 单测

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S6-SV-001 | 保存初次 Draft | version=1，hash 正确 | P0 |
| S6-SV-002 | 相同内容重复保存 | 按幂等策略返回原 Draft | P1 |
| S6-SV-003 | 修改 Draft | 创建 version=2，不覆盖 version=1 | P0 |
| S6-SV-004 | 确认 Unit 含三个 Nodes | 创建三个 Section Versions | P0 |
| S6-SV-005 | Section Node 不属于 Unit | 拒绝 | P0 |
| S6-SV-006 | 少一个 Unit Node 的 Section | 拒绝确认 | P0 |
| S6-SV-007 | 一个 Node 出现两次 | 拒绝确认 | P0 |
| S6-SV-008 | Section content hash 错误 | 拒绝 | P0 |
| S6-SV-009 | Section 引用错误 Grounding Run | 拒绝 | P0 |
| S6-SV-010 | Confirmed Section 修改内容 | 不允许 | P0 |
| S6-SV-011 | 修订确认 | 创建下一 Section Version | P0 |
| S6-SV-012 | 新版本确认 | 旧当前版本变 `SUPERSEDED` | P0 |
| S6-SV-013 | 历史 Document 仍引用旧 Section | 可完整重现 | P0 |
| S6-SV-014 | Confirm Unit 中途失败 | 不创建半套 Sections | P0 |
| S6-SV-015 | Section title 与锁定 Outline 不一致 | 拒绝或由 Renderer 使用 Outline title | P1 |

## 10. Deterministic Quality Checks 单测

| ID | 场景 | 预期 Issue | 优先级 |
| --- | --- | --- | --- |
| S6-DQ-001 | Outline Node 无 Section | `OUTLINE_COVERAGE/BLOCKER` | P0 |
| S6-DQ-002 | 同一 Node 两个当前 Section | `OUTLINE_COVERAGE/BLOCKER` | P0 |
| S6-DQ-003 | Section 空白 | `EMPTY_OR_DUPLICATE_CONTENT/ERROR` | P0 |
| S6-DQ-004 | 两 Section 内容完全重复 | Duplicate Issue | P1 |
| S6-DQ-005 | 只有标题没有正文 | Empty Issue | P0 |
| S6-DQ-006 | 必需验收章节无可执行条件 | `ACCEPTANCE_NOT_EXECUTABLE` | P0 |
| S6-DQ-007 | Claim/Fact Link 缺失 | `SOURCE_LINK_INCOMPLETE/BLOCKER` | P0 |
| S6-DQ-008 | Unknown 标记完整 | 不产生未标记假设 Issue | P1 |
| S6-DQ-009 | 开放 Critical Unknown | `OPEN_ITEM_UNRESOLVED/BLOCKER` | P0 |
| S6-DQ-010 | Warning Unknown | Warning，可确认接受 | P1 |
| S6-DQ-011 | 相同输入重复执行 | Issue IDs/排序确定 | P1 |
| S6-DQ-012 | 非当前 Section 版本混入 | Blocker | P0 |
| S6-DQ-013 | Document Section 顺序错误 | Coverage/Order Error | P0 |
| S6-DQ-014 | Evidence Appendix 缺少合法 Claim | Source Link Issue | P0 |

## 11. Semantic Quality Checker 合同单测

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S6-QC-001 | 术语前后不一致 | 结构化 Terminology Issue | P1 |
| S6-QC-002 | 权限规则冲突 | `ROLE_PERMISSION_CONFLICT` | P0 |
| S6-QC-003 | 状态流缺少失败分支 | `STATE_FLOW_GAP` | P1 |
| S6-QC-004 | 异常没有用户反馈 | `EXCEPTION_GAP` | P1 |
| S6-QC-005 | 验收不可验证 | `ACCEPTANCE_NOT_EXECUTABLE` | P0 |
| S6-QC-006 | Checker 返回未知 Section ID | 引用校验失败 | P0 |
| S6-QC-007 | Checker 试图直接返回改写正文 | Schema 拒绝或忽略正文 | P0 |
| S6-QC-008 | Checker Timeout | Quality Run 安全失败 | P0 |
| S6-QC-009 | Malformed 后 Repair 成功 | 只接受合法结构 | P1 |
| S6-QC-010 | Repair 仍失败 | 不进入 Final Review | P0 |
| S6-QC-011 | 仓库内容包含 Prompt Injection | 作为数据处理 | P0 |
| S6-QC-012 | Checker 将 Grounding 问题判为质量通过 | Grounding Gate 仍阻断 | P0 |
| S6-QC-013 | Checker Severity 超出 Policy 上限/下限 | Policy 归一化 | P1 |
| S6-QC-014 | rationale 含完整正文/私有推理 | 截断和脱敏 | P0 |

## 12. Quality Policy 单测

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S6-QP-001 | Unit 有 Blocker | 不可等待确认 | P0 |
| S6-QP-002 | Unit 只有 Info | 可等待确认 | P1 |
| S6-QP-003 | Document 有 Error | 不可 Finalize | P0 |
| S6-QP-004 | Document 只有 Warning | 可经用户确认进入 Final Review | P1 |
| S6-QP-005 | Warning 未 acknowledgement | Finalize 拒绝 | P0 |
| S6-QP-006 | acknowledgement 属于其他 Document | 拒绝 | P0 |
| S6-QP-007 | 已解决 Issue | 不阻断新 Document Version | P1 |
| S6-QP-008 | 旧 Quality Run | 不能支持当前 Document | P0 |
| S6-QP-009 | Grounding Run 非合法终态 | Quality 通过也不能推进 | P0 |
| S6-QP-010 | 模型建议忽略 Blocker | Policy 拒绝 | P0 |

## 13. 全文 Renderer 单测

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S6-RD-001 | 多级 Outline + 多 Section | 按 Outline 顺序输出 | P0 |
| S6-RD-002 | 三级节点 | 标题层级正确 | P1 |
| S6-RD-003 | 未确认 Draft 存在 | 不进入 Markdown | P0 |
| S6-RD-004 | Superseded Section 存在 | 不进入当前 Markdown | P0 |
| S6-RD-005 | Section 缺失 | Renderer 拒绝 | P0 |
| S6-RD-006 | 相同输入调用两次 | 字节级相同 | P0 |
| S6-RD-007 | 内容顺序不同但 snapshot 顺序固定 | 输出按 snapshot/outline | P1 |
| S6-RD-008 | 标题含 Markdown 特殊字符 | 安全规范化 | P1 |
| S6-RD-009 | 尾部空白/换行 | 格式确定 | P2 |
| S6-RD-010 | Renderer 被要求生成业务段落 | 无此能力，只拼装输入 | P0 |
| S6-RD-011 | Document hash | 覆盖正文与 Appendix | P0 |
| S6-RD-012 | 旧 Document Snapshot | 仍可重现原 Markdown/hash | P0 |

## 14. Evidence Appendix 单测

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S6-EA-001 | 一个 Claim→Fact→Evidence | 输出完整来源链 | P0 |
| S6-EA-002 | 多 Claim 使用同一 Evidence | 来源去重、使用关系保留 | P1 |
| S6-EA-003 | Unsupported Fact | 不进入“已验证依据” | P0 |
| S6-EA-004 | Stale/Invalid Evidence | 标记问题，不伪装有效 | P0 |
| S6-EA-005 | Target Decision | 进入目标决策分区 | P1 |
| S6-EA-006 | Authorized Assumption | 进入假设分区并带状态 | P1 |
| S6-EA-007 | Unknown/Conflict | 进入待确认/冲突分区 | P0 |
| S6-EA-008 | repository-relative path | 输出相对定位 | P0 |
| S6-EA-009 | 绝对路径 | 拒绝或规范化，不泄漏 | P0 |
| S6-EA-010 | Secret/大段 excerpt | 脱敏且不复制原文 | P0 |
| S6-EA-011 | 完整 SHA 保存、短 SHA 展示 | 两者正确 | P1 |
| S6-EA-012 | 相同输入 | Appendix 字节级一致 | P1 |
| S6-EA-013 | Link 属于其他 Task | 权限拒绝 | P0 |
| S6-EA-014 | Claim 没有 Section Link | Quality Blocker | P0 |

## 15. Revision Plan 单测

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S6-RV-001 | Issue 指向一个 Unit | Proposal 精确包含该 Unit | P0 |
| S6-RV-002 | Issue 指向两个 Section | 映射到正确 Units | P0 |
| S6-RV-003 | Proposal 直接带改写正文 | Schema 拒绝 | P0 |
| S6-RV-004 | 用户未批准 | Unit 保持 Confirmed | P0 |
| S6-RV-005 | 用户批准 | 目标 Unit 变 Revision Required | P0 |
| S6-RV-006 | Proposal 基于旧 Document | Version Conflict | P0 |
| S6-RV-007 | 只批准部分 Warning | 只重开批准项 | P1 |
| S6-RV-008 | 尝试拒绝 Blocker 且 Finalize | Finalize 仍拒绝 | P0 |
| S6-RV-009 | 上游 Section 改变 | 下游 Context Dependency 检测 Stale | P0 |
| S6-RV-010 | 下游不消费该 Section | 不误重开 | P1 |
| S6-RV-011 | 下游实际消费旧版本 | 标记 Revision Required | P0 |
| S6-RV-012 | 无关 Unit | 保持 Confirmed | P0 |
| S6-RV-013 | 修订顺序 | 按 Scheduler 串行 | P0 |
| S6-RV-014 | 修订完成 | 创建新 Candidate Document | P0 |
| S6-RV-015 | 旧 Quality Issues | 只保留审计，不阻断错误版本 | P1 |
| S6-RV-016 | Revision Plan 幂等重放 | 不重复重开/增版本 | P0 |

## 16. Finalization Policy 单测

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S6-FN-001 | 全部条件满足 | Task → COMPLETED | P0 |
| S6-FN-002 | Task 非 FINAL_REVIEW | 拒绝 | P0 |
| S6-FN-003 | Document 不是当前版本 | Version Conflict | P0 |
| S6-FN-004 | content hash 不匹配 | Version Conflict | P0 |
| S6-FN-005 | 一个 Unit 未确认 | 拒绝 | P0 |
| S6-FN-006 | 一个 Node 无 Section | 拒绝 | P0 |
| S6-FN-007 | Full Grounding 未通过 | 拒绝 | P0 |
| S6-FN-008 | Quality 有 Blocker | 拒绝 | P0 |
| S6-FN-009 | Quality 有 Error | 拒绝 | P0 |
| S6-FN-010 | Warning 未确认 | 拒绝 | P0 |
| S6-FN-011 | 所有 Warning 正确确认 | 允许 | P1 |
| S6-FN-012 | Warning ID 属于其他版本 | 拒绝 | P0 |
| S6-FN-013 | Section Snapshot 已变化 | 拒绝 | P0 |
| S6-FN-014 | 相同 Finalize 幂等重放 | 返回同一完成结果 | P0 |
| S6-FN-015 | 相同 key 不同 hash | Idempotency Conflict | P0 |
| S6-FN-016 | 成功后事件 | 单一 `PrdFinalized` | P1 |
| S6-FN-017 | 成功后不触发外部导出 | 无外部副作用 | P0 |
| S6-FN-018 | Completed 后再次普通 Confirm Unit | 拒绝 | P0 |

## 17. Reopen 单测

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S6-RO-001 | Completed + 合法原因 | 创建 Revision Session | P1 |
| S6-RO-002 | 非 Completed 普通 Reopen | 拒绝 | P1 |
| S6-RO-003 | Reopen 不修改旧 Document | 历史版本保持 Confirmed | P0 |
| S6-RO-004 | 选择受影响 Unit | 只重开指定和失效下游 | P0 |
| S6-RO-005 | 新版本完成 | 新 Document 成为当前 | P1 |
| S6-RO-006 | Reopen 幂等 | 不重复 Session | P1 |

## 18. Workflow Service 集成测试

| ID | 场景 | 关键断言 | 优先级 |
| --- | --- | --- | --- |
| S6-WF-001 | 6 Unit Happy Path | 顺序生成、6 次确认、Final Review | P0 |
| S6-WF-002 | 第一 Unit 要调查 | Investigation/Grounding 后才生成 | P0 |
| S6-WF-003 | Optional 调查跳过 | 保留记录，不阻断合法 Unit | P1 |
| S6-WF-004 | Required 调查失败 | Human Input/Unknown，不自动推进 | P0 |
| S6-WF-005 | Grounding 安全降级 | 降级正文可确认，状态可审计 | P0 |
| S6-WF-006 | Unit Quality Blocker | 保持当前 Unit | P0 |
| S6-WF-007 | 用户修改 Draft | 新版本重新检查后确认 | P0 |
| S6-WF-008 | 所有 Units Confirmed | 自动组装 Candidate，不自动 Finalize | P0 |
| S6-WF-009 | Full Quality 通过 | Task 进入 FINAL_REVIEW | P0 |
| S6-WF-010 | Full Quality 失败 | Proposal 产生，Task 不 Completed | P0 |
| S6-WF-011 | 用户批准 Revision | 精确重开并再次串行确认 | P0 |
| S6-WF-012 | Revision 后检查通过 | 新 Document 进入 Final Review | P0 |
| S6-WF-013 | 用户 Finalize | Task/Document 原子 Completed | P0 |
| S6-WF-014 | Complete Workflow 重放命令 | 无重复版本/事件 | P0 |
| S6-WF-015 | 不同 actor 访问 Task | 权限拒绝 | P0 |

## 19. 事件与 Checkpoint 单测

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S6-EC-001 | Unit 生命周期 | 事件顺序与状态一致 | P1 |
| S6-EC-002 | Event sequence | Task 内严格递增 | P0 |
| S6-EC-003 | 重放命令 | 不追加重复业务事件 | P0 |
| S6-EC-004 | Checkpoint 含当前 Unit/version | 正确 | P0 |
| S6-EC-005 | Checkpoint 含 Grounding/Quality Run | 正确 | P0 |
| S6-EC-006 | Final Review Checkpoint | 绑定 Document Version | P0 |
| S6-EC-007 | 事件 Payload | 不含完整正文/Evidence/Secret | P0 |
| S6-EC-008 | 事务失败 | 无孤立事件 | P0 |

## 20. 崩溃恢复单测

| ID | 故障点 | 恢复预期 | 优先级 |
| --- | --- | --- | --- |
| S6-RC-001 | Context 保存后崩溃 | 不重复上游消费记录 | P1 |
| S6-RC-002 | Investigation 完成后崩溃 | 不重复调查 | P0 |
| S6-RC-003 | Grounding 完成后崩溃 | 不重复 Grounding Retry | P0 |
| S6-RC-004 | Draft 保存前崩溃 | 重新生成或安全失败，无半 Draft | P1 |
| S6-RC-005 | Draft 保存后、等待确认前崩溃 | 恢复同一 Draft/hash | P0 |
| S6-RC-006 | Confirm Unit 事务中崩溃 | Unit/Sections/sequence 全提交或全回滚 | P0 |
| S6-RC-007 | Candidate Document 组装中崩溃 | 不产生不完整 Snapshot | P0 |
| S6-RC-008 | Quality 完成后崩溃 | 复用同一合法 Run | P1 |
| S6-RC-009 | Revision 批准中崩溃 | Units 原子重开 | P0 |
| S6-RC-010 | Finalize 中崩溃 | Task/Document 同时完成或均未完成 | P0 |
| S6-RC-011 | 恢复后 current sequence | 不重复推进 | P0 |
| S6-RC-012 | 恢复时版本已被其他 Worker 更新 | 乐观锁冲突 | P0 |

## 21. Store 合同测试

同一测试集分别运行 Memory 和 PostgreSQL：

| ID | 合同 | 优先级 |
| --- | --- | --- |
| S6-ST-001 | Outline/Nodes/Units 完整往返 | P0 |
| S6-ST-002 | Unit Dependencies 顺序稳定 | P0 |
| S6-ST-003 | Draft Versions 不可变 | P0 |
| S6-ST-004 | Section Versions/状态往返 | P0 |
| S6-ST-005 | Context Dependencies 往返 | P0 |
| S6-ST-006 | Quality Run/Issues 往返 | P0 |
| S6-ST-007 | Revision Plan/Items 往返 | P0 |
| S6-ST-008 | Document Snapshot 有序往返 | P0 |
| S6-ST-009 | Warning Acknowledgement 往返 | P1 |
| S6-ST-010 | Snapshot 选择当前有效版本 | P0 |
| S6-ST-011 | Domain Event 顺序 | P0 |
| S6-ST-012 | Idempotency Lookup | P0 |
| S6-ST-013 | 时间均为 UTC aware | P1 |
| S6-ST-014 | JSON/Enum 无损往返 | P0 |

## 22. PostgreSQL 真实集成门禁

| ID | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S6-PG-001 | Step 6 Migration 在 Step 1～5 Schema 上执行 | 成功 | P0 |
| S6-PG-002 | Unit sequence 唯一约束 | 冲突失败 | P0 |
| S6-PG-003 | Section node/version 唯一 | 冲突失败 | P0 |
| S6-PG-004 | Document task/version 唯一 | 冲突失败 | P0 |
| S6-PG-005 | 跨 Task Link | FK/应用校验拒绝 | P0 |
| S6-PG-006 | Confirm Unit 事务故障 | 全回滚 | P0 |
| S6-PG-007 | Assemble Document 事务故障 | 全回滚 | P0 |
| S6-PG-008 | Approve Revision 事务故障 | 全回滚 | P0 |
| S6-PG-009 | Finalize 事务故障 | 全回滚 | P0 |
| S6-PG-010 | 两并发 Confirm Unit | 一个成功、一个 Version Conflict | P0 |
| S6-PG-011 | 两并发 Finalize | 一个业务提交、结果一致 | P0 |
| S6-PG-012 | 同幂等键并发 | 单一结果 | P0 |
| S6-PG-013 | 历史 Finalized Snapshot | 外键保护，不可破坏 | P0 |
| S6-PG-014 | stale Run 扫描 | 正确收敛/恢复 | P1 |
| S6-PG-015 | 连接中断 | 不把任务误标完成 | P0 |

## 23. 完整 E2E 场景

### S6-E2E-001：中等需求完整完成

生成 6 个 Unit，逐个经过 Scripted Investigation/Grounding/Quality 和用户确认，汇编全文、检查、最终确认。断言：

- 每次只有一个 Active Unit。
- 6 个 Unit 均有确认 Section。
- Candidate Document 包含所有 Outline Nodes。
- Evidence Appendix 可追溯。
- Task 最终 `COMPLETED`。

### S6-E2E-002：用户两次修改草稿

同一 Unit 生成 3 个 Draft Version，只确认最后版本。最终文档不得包含前两版内容。

### S6-E2E-003：全文权限冲突修订

Full Quality 发现角色权限冲突；系统提出 Proposal，用户批准后重开权限和验收 Unit，重新确认并生成 Document v2。Document v1 仍可复现。

### S6-E2E-004：用户拒绝 Warning

只有 Warning 时，用户可记录接受理由并 Finalize；未确认 Warning 时 Finalize 失败。

### S6-E2E-005：Blocker 不能绕过

存在缺失核心章节或开放 Critical Unknown，即使用户发送普通 Finalize，也必须拒绝并保持 `FINAL_REVIEW/GENERATING` 合法状态。

### S6-E2E-006：上游修订使下游失效

范围 Unit 修订改变角色定义；使用旧 Context 的流程和验收 Unit 被精确标记 `REVISION_REQUIRED`，无关数据模型 Unit 保持 Confirmed。

### S6-E2E-007：崩溃恢复

在第三 Unit 确认事务和 Finalize 事务分别注入崩溃。恢复后没有重复 Section、重复事件、跳序或半完成状态。

### S6-E2E-008：Completed 后 Reopen

Document v1 完成后显式 Reopen，修订一个 Unit，最终生成 v2；v1 和 v2 均可按各自 Snapshot/hash 重现。

## 24. Eval 指标单测

| ID | 指标场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| S6-MT-001 | 所有 Node 有 Section | Outline Coverage=1 | P1 |
| S6-MT-002 | 缺一个 Node | Coverage 正确下降 | P1 |
| S6-MT-003 | Unit DAG 非法 Run | Unit Plan Validity=0 | P1 |
| S6-MT-004 | 尝试越序生成被 Policy 拒绝 | Violation attempt 与 escaped violation 分开 | P1 |
| S6-MT-005 | Draft 泄漏最终文档 | Context Purity/Finalization Safety 失败 | P0 |
| S6-MT-006 | 只重开必要 Units | Revision Precision=1 | P1 |
| S6-MT-007 | 误重开无关 Unit | Precision 下降 | P1 |
| S6-MT-008 | Appendix Link 完整 | Traceability=1 | P1 |
| S6-MT-009 | Workflow Run 失败 | 单列失败，不伪造 0 或忽略 | P0 |
| S6-MT-010 | 指标分母为 0 | `not_applicable` | P1 |

## 25. Test Fixtures 与替身

建议提供：

- `FixedClock`
- `SequentialIdFactory`
- `OutlinePlanBuilder`
- `ConfirmationUnitBuilder`
- `SectionVersionBuilder`
- `DocumentVersionBuilder`
- `ScriptedWorkflowModel`
- `ScriptedGroundingService`
- `ScriptedQualityChecker`
- `ScriptedUser`
- `FailAtCheckpointRepository`
- `ConcurrentCommandBarrier`
- `MemoryWorkflowStoreFactory`
- `PostgresWorkflowStoreFactory`

Builder 默认创建最小合法对象；测试只覆盖与场景相关的字段，避免每个测试复制大型 Fixture。

## 26. TDD 执行顺序

1. Outline/Unit Model 与 Plan Policy。
2. Scheduler 和 Unit State Policy。
3. Context Builder 与 Dependency Snapshot。
4. Draft/Section Version。
5. Unit Pipeline 和多 Unit Workflow。
6. Deterministic Quality Checks。
7. Semantic Checker Contract 与 Quality Policy。
8. Renderer 和 Evidence Appendix。
9. Revision Plan 与依赖失效。
10. Finalization/Reopen。
11. Store Contract、PostgreSQL 和 Recovery。
12. E2E 与 Eval Metrics。

每个切片遵循：

```text
新增失败测试 → 最小实现 → 切片测试通过 → 全量回归 → 重构
```

## 27. 建议执行命令

```bash
venv/bin/python -m pytest -q tests/domain tests/policies
venv/bin/python -m pytest -q tests/quality tests/rendering
venv/bin/python -m pytest -q tests/workflow
venv/bin/python -m pytest -q tests/storage/test_workflow_store_contract.py
PRD_AGENT_TEST_POSTGRES_DSN="$PRD_AGENT_TEST_POSTGRES_DSN" \
  venv/bin/python -m pytest -q tests/storage/test_postgres_complete_workflow_integration.py
venv/bin/python -m pytest -q tests/eval/test_complete_workflow.py tests/eval/test_complete_workflow_metrics.py
venv/bin/python -m pytest -q
```

完整门禁报告必须列出 PostgreSQL 实际版本、DSN 测试是否执行和 Skip 数量。缺少 DSN 可以作为普通开发提示，不能被记录为完整自测通过。

## 28. 覆盖率与质量门禁

- 新增纯领域/Policy/Renderer 代码：branch coverage 不低于 90%。
- 新增 Application/Workflow Service：branch coverage 不低于 85%。
- 所有状态边和非法状态边至少一个测试。
- 所有命令覆盖版本冲突、幂等重放和相同 key 不同输入。
- 所有 P0/P1 测试通过。
- P0 测试不得 skip。
- 真实 PostgreSQL P0 门禁全部执行。
- 现有 Step 1～5 测试全部回归通过。

覆盖率不是替代验收的指标；即使覆盖率达标，存在草稿泄漏、越序生成、Blocker 绕过或非原子 Finalize 仍判定失败。

## 29. 完成记录模板

实现完成后追加：

```text
实现日期：
代码版本：
测试总数：
P0/P1/P2 通过数：
Skip 数：
Branch Coverage：
PostgreSQL 版本：
PostgreSQL 集成结果：
完整 E2E 结果：
Complete Workflow Eval Runs：
End-to-end Completion Rate：
Outline Coverage：
Revision Precision：
Evidence Appendix Traceability：
Finalization Safety Violations：
已知问题：
```

## 30. 2026-07-23 当前执行记录

```text
实现日期：2026-07-23
测试总数：105
测试结果：105 passed
Skip 数：0（注入真实 PostgreSQL DSN）
PostgreSQL 版本：16.14 Homebrew/aarch64
PostgreSQL 集成结果：通过
空库 Schema 初始化：通过
完整 E2E：多 Unit、Grounding、Quality、Revision、Finalize、Reopen 通过
Finalization Safety Violations：0
```

已自动化覆盖的核心类别：

- 动态多节点/多 Unit 规划。
- 三级父子 Outline。
- Unit 顺序与依赖门禁。
- 多节点单 Unit Section Version。
- Grounding 成功、缺来源、跨 commit、一次补充和 Claim Inventory。
- Quality Issue 阻断和用户批准修订。
- Document hash、Finalize 幂等和历史版本。
- Completed Reopen 与下游依赖重开。
- Evidence Appendix。
- Memory/PostgreSQL 往返。
- Migration、空库 Schema、真实 PostgreSQL 全量联通。

本文档的 285 个编号项是完整回归目标，不等于当前已实现 285 条独立测试。尚未自动化的主要部分是所有排列组合、真实语义模型故障注入、细粒度并发崩溃点和完整 Eval 多 Trial；这些不应被表述为已经通过。

# M0 Agent Core 第一部分单测方案

> 对应实现：`docs/superpowers/plans/2026-07-21-m0-agent-core-step-1-eval-baseline.md`  
> 测试目标：验证评测数据契约、Direct Prompt Baseline、幂等 Runner、确定性指标和报告生成的公共行为。

## 1. 测试原则

单测通过公共接口验证行为，不绑定私有函数、内部变量或具体实现结构。每个测试描述一个可观察结果，例如“相同输入生成相同哈希”或“重复运行不再次调用模型”。

测试使用垂直切片推进：先写一个失败测试，再实现最小行为，测试通过后再进入下一个行为。禁止一次性写完全部测试再批量实现代码。

单测默认离线运行。模型使用 Stub/Spy Adapter，数据库使用内存 Store；PostgreSQL 真实连接只放在独立集成测试，不阻塞普通单测。

## 2. 测试分层

| 层级 | 范围 | 依赖 | 运行频率 |
| --- | --- | --- | --- |
| Unit | Case、Prompt、Runner、指标、报告 | Python 标准库、Stub Model | 每次提交 |
| Contract | PostgreSQL SQL Schema、RunStore 字段映射 | 本地 PostgreSQL | 每次数据库变更 |
| Integration | 真实 PostgreSQL 持久化和断点续跑 | Docker PostgreSQL、`psycopg` | PR 或发布前 |
| Evaluation | 10～20 Cases、每配置至少 3 Trial | 真实模型或固定 Stub | Baseline/Agent 版本变更 |

## 3. 测试矩阵

### 3.1 Case Loader

测试文件：`tests/eval/test_case_loader.py`

| 行为 | 期望 |
| --- | --- |
| 合法 Manifest 和 Case | 返回固定数据集版本、仓库 ID、Case 顺序和 Ground Truth |
| 缺少必填字段 | 抛出 `DatasetValidationError`，指出文件和字段 |
| 非法 Information Need 类型 | 拒绝加载，不降级成普通字符串 |
| 非 40 位十六进制 revision | 拒绝加载 |
| 重复 `case_id` | 拒绝加载 |
| Case 与 Manifest revision 不一致 | 拒绝加载 |
| 空 Case 列表 | 拒绝加载 |
| 同一数据集重复加载 | 规范化顺序和输入哈希保持一致 |

### 3.2 Direct Prompt Baseline

测试文件：`tests/eval/test_prompt_baseline.py`

| 行为 | 期望 |
| --- | --- |
| 相同 Case、配置和 Prompt 版本 | `input_hash` 完全相同 |
| Baseline 调用模型 | 只提交固定 System Prompt 和 User Prompt |
| Baseline 运行 | 不注册 Tool Schema，不触发 Repository Adapter 或历史检索 |
| 成功响应 | 保存输出、输出哈希、模型 ID、Token 和耗时 |
| 超时、限流、无效响应 | 保存失败 Run，不生成伪造 PRD |
| 非法 Trial 编号 | 在公共入口拒绝 |

### 3.3 Baseline Runner

测试文件：`tests/eval/test_runner.py`

| 行为 | 期望 |
| --- | --- |
| 2 个 Case、2 次 Trial | 生成 4 条独立 Run |
| 已完成 Run 且输入哈希一致 | 重跑不调用模型，复用原 Run |
| 失败 Run | 下次允许重试，并保持同一幂等 ID |
| Run ID 冲突且输入哈希不同 | 拒绝覆盖，报告数据错误 |
| 配置 Dataset Version 不匹配 | Runner 初始化失败 |
| 每个 Run | 绑定 Case、Prompt、Model、Dataset 和 Repository revision |

### 3.4 Deterministic Metrics

测试文件：`tests/eval/test_metrics.py`

| 指标 | 测试重点 |
| --- | --- |
| `required_section_coverage` | 全章节、缺章节、空输出、重复章节 |
| `requirement_coverage` | 全部关键词、部分关键词、无关键词配置 |
| `boundary_recall` | 明确范围外、待确认和未知标记 |
| `unknown_preservation` | 期望未知项被保留、被遗漏 |
| `acceptance_criteria_executability` | 条件/动作/预期结果齐全或缺失 |
| `unsupported_claim_rate` | Direct Prompt 阶段必须为 `not_applicable`，不能误报为 0 |

所有指标测试使用固定文本，结果必须可重复计算，不能依赖模型 Judge 或时间。

### 3.5 Report

测试文件：`tests/eval/test_report.py`

| 行为 | 期望 |
| --- | --- |
| 成功 Run | Markdown 和 JSON 含数据集、配置、Case、指标和哈希 |
| 失败 Run | 列出错误和失败轨迹，不生成任何指标假值 |
| 混合成功/失败 | 汇总成功指标，同时保留失败数量和明细 |
| 无 Grounding 指标 | 报告状态为 `not_applicable` |
| 重复生成 | 同一 Run 集合的 JSON 结构和数值一致 |

## 4. PostgreSQL 合约与集成测试

单测不启动数据库；`PostgresRunStore` 的真实验证使用 `infra/local/docker-compose.yml` 启动的 PostgreSQL。

集成测试必须覆盖：

1. Schema 可重复初始化，表、约束和唯一索引存在。
2. `BaselineRun` 每个字段可写入并完整读回。
3. 相同 `eval_run_id` 和相同 `input_hash` 重复保存不会产生重复行。
4. 相同 `eval_run_id` 但不同 `input_hash` 会失败，不得覆盖已有 Run。
5. `eval_run` 的 `status` 只允许 `completed` 或 `failed`。
6. 事务提交前失败不会产生半条业务结果。

集成测试使用独立数据库或事务回滚，禁止连接生产数据库，禁止把真实模型输出写入固定测试库。

## 5. 数据集与模型测试替身

`tests/eval` 中的 Case 使用最小内存对象，不复用生产报告。Stub Model 必须提供：

- 固定输出，用于报告结构测试。
- 可计数调用，用于幂等测试。
- 可控超时、限流和异常，用于失败路径测试。

Ground Truth 使用独立 fixture。测试不得从模型输出反向生成期望值，也不得把 `expected_facts` 当作 Baseline 的输入上下文。

## 6. 质量门禁

每次提交运行：

```bash
PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py'
```

进入下一步前必须满足：

- 单测全部通过。
- Case 数量不少于 10 个。
- 每种配置至少运行 3 个 Trial。
- 失败 Run 有明确错误类型和报告记录。
- Direct Prompt 未调用任何工具或检索。
- 输入哈希、输出哈希、数据集版本和仓库 revision 可追溯。
- PostgreSQL 合约测试通过，且不存在 SQLite 分支。

## 7. 后续扩展

进入 Repository Tools 后新增 Evidence Contract、Tool Action Signature 和固定 commit 校验测试；进入 Investigation Loop 后新增预算、重复动作、Coverage、无进展停止和恢复测试；进入 Grounding 后启用 Evidence Precision、Verified Fact Accuracy 和 Unsupported Claim Rate。

这些后续测试应新增指标和 fixture 版本，不得修改已冻结 Baseline 的历史结果。

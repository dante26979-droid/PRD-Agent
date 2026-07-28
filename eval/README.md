# M0 Eval：第一步运行说明

本目录实现 M0 Agent Core 的固定 Demo Repository、Ground Truth Cases、Direct Prompt Baseline，
第二步的 Minimal Workflow Ablation、第三步的 Single Retrieval、第四步带 Coverage、
预算、重复检测和停止原因的 Bounded Investigation Loop，以及第五步的 Grounding 运行指标。

本阶段默认使用 Stub Model 离线运行，确认数据契约、Runner、指标和报告链路；接入真实模型时只替换 Model Adapter，不修改 Case、指标和报告协议。

## 数据与版本

- `cases/manifest.json` 固定数据集版本、Demo Repository 和 40 位 revision。
- `cases/case-*.json` 是 Ground Truth，不允许被模型输出覆盖。
- `fixtures/demo-repo` 只读，Baseline 不执行其中代码，不访问网络。
- `configs/direct_prompt.json` 默认每个 Case 运行 3 次。

## 本地运行

只验证数据集：

```bash
PYTHONPATH=src python -m prd_agent.eval validate-dataset \
  --manifest eval/cases/manifest.json
```

启动统一 PostgreSQL：

```bash
docker compose -f infra/local/docker-compose.yml up -d postgres
export PRD_AGENT_DATABASE_DSN='postgresql://prd_agent:prd_agent_local_only@localhost:5432/prd_agent'
```

使用 Stub Model 生成 Baseline 报告（不传 `--dsn` 时使用内存存储，仅适合离线冒烟）：

```bash
PYTHONPATH=src python -m prd_agent.eval run-baseline \
  --manifest eval/cases/manifest.json \
  --config eval/configs/direct_prompt.json \
  --dsn "$PRD_AGENT_DATABASE_DSN" \
  --output-dir eval/reports

PYTHONPATH=src python -m prd_agent.eval run-baseline \
  --manifest eval/cases/manifest.json \
  --config eval/configs/bounded_investigation.json \
  --output-dir eval/reports
```

安装 PostgreSQL 适配器：

```bash
python -m pip install '.[postgres]'
```

## 运行测试

当前测试使用 Python 标准库 `unittest`，无需访问 PostgreSQL 或模型 API：

```bash
PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py'
```

单测策略见：

[`docs/superpowers/plans/2026-07-21-m0-agent-core-step-1-unit-test-plan.md`](../docs/superpowers/plans/2026-07-21-m0-agent-core-step-1-unit-test-plan.md)

## 约束

- M0 不维护 SQLite/PostgreSQL 双业务实现。
- Direct Prompt 不注册工具，不调用仓库、历史 PRD、向量检索或外部 Coding Agent。
- Minimal Workflow 使用真实 `WorkflowService`，自动通过 Outline/Unit 两个确认门以便批量评测；
  它仍不注册调查工具，也不伪造 Evidence/Grounding 指标。
- Single Retrieval 为每个固定 Case 执行一个版本锁定的 Repository Tool Action，
  将经过校验的 Evidence/Parser Fact 作为受限上下文；Action 由 Fixture 固定，不包含调查 Loop。
- Bounded Investigation 使用确定性的 Scripted Selector 验证真实 Loop 控制；报告新增 Coverage、
  Tool Call、Duplicate Action 和 Replan 指标，但不把离线 Selector 结果表述为真实模型质量。
- Grounding 结果通过 `evaluate_grounding` 计量 Unsupported Claim、Evidence Precision、
  Verified Fact 运行支持率、首次通过率和一次回退率；不适用场景保持 `not_applicable`。
- 报告不得预填未经真实运行验证的质量数字。
- 生产数据、模型输出和代码片段不得提交到 Git；只提交脱敏摘要、哈希和报告。

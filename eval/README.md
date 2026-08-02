# M0 Eval：第一步运行说明

本目录实现 M0 Agent Core 的固定 Demo Repository、Ground Truth Cases、Direct Prompt Baseline，
第二步的 Minimal Workflow Ablation、第三步的 Single Retrieval、第四步带 Coverage、
预算、重复检测和停止原因的 Bounded Investigation Loop，以及第五步的 Grounding 运行指标。
Phase 0 额外通过固定 Scripted Model/Gateway 运行生产 `LangGraphAgentLoop`，生成
`agent-loop-trace.v1` 和默认脱敏报告；它用于 Characterization，不代表真实模型质量。

本阶段默认使用 Stub Model 离线运行，确认数据契约、Runner、指标和报告链路；接入真实模型时只替换 Model Adapter，不修改 Case、指标和报告协议。

## 数据与版本

- `cases/manifest.json` 固定数据集版本、Demo Repository 和 40 位 revision。
- `cases/case-*.json` 是 Ground Truth，不允许被模型输出覆盖。
- `fixtures/demo-repo` 只读，Baseline 不执行其中代码，不访问网络。
- `configs/direct_prompt.json` 默认每个 Case 运行 3 次。
- `configs/langgraph_v1_characterization.json` 固定生产 Loop、预算和 Trace 版本，每个 Case
  运行一次确定性 trial。
- `fixtures/langgraph_v1_actions.json` 只包含公开 fixture 数据；Trace 不保存 query、路径、
  excerpt、Prompt 或 Draft 正文。

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

PYTHONPATH=src:agent-python venv/bin/python -m prd_agent.eval run-baseline \
  --manifest eval/cases/manifest.json \
  --config eval/configs/langgraph_v1_characterization.json \
  --output-dir eval/reports
```

安装 PostgreSQL 适配器：

```bash
python -m pip install '.[postgres]'
```

## 运行测试

测试无需访问 PostgreSQL、模型 API 或网络。由于根工程与 `agent-python` 各自包含一个
顶层 `tests` package，必须分两个 pytest 进程运行：

```bash
PYTHONPATH=agent-python venv/bin/python -m pytest -q agent-python/tests
PYTHONPATH=src:agent-python venv/bin/python -m pytest -q tests/eval
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
- Characterization 报告必须标记 `measurement_mode=scripted_characterization` 和
  `deterministic_only=true`；Requiredness 在 Phase 3 前保持 legacy/not-applicable。
- JSON/Markdown 报告只输出 run identity、hash、枚举、计数和时延白名单；完整 Draft、
  Prompt、Evidence、路径和异常不得进入报告。
- 生产数据、模型输出和代码片段不得提交到 Git；只提交脱敏摘要、哈希和报告。

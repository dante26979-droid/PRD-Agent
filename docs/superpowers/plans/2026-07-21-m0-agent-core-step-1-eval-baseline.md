# M0 Agent Core 第一部分 Step Plan：评测场景与 Direct Prompt Baseline

> 日期：2026-07-21  
> 适用范围：M0 Agent Core / 阶段 0  
> 对应总领方案：`docs/architecture/2026-07-21-prd-agent-v1.1-feasibility-review.md` 第 8.2 节第 1 步  
> 目标：固定可复现的 Demo Repository、Ground Truth Cases 和 Direct Prompt Baseline，形成后续 Agent 能力的可比较基线。

## 1. 这一步要解决什么问题

第一步不追求“生成一个看起来完整的 PRD”，而是先建立一个可以重复运行、可以解释失败、可以比较后续方案收益的实验基线。

完成后，团队必须能够回答：

1. 同一需求、同一上下文和同一模型配置是否能稳定复现输入与结果。
2. Direct Prompt 在不调用工具、不执行 Investigation Loop、不做 Grounding 时能达到什么质量水平。
3. 后续加入单次检索、受控 Investigation Loop 和 Grounding 后，质量提升是否来自真实能力，而不是数据、Prompt 或模型版本变化。
4. 当前案例是否覆盖“无须调查、可选调查、必须调查、冲突、空结果、澄清和预算不足”等关键路径。

## 2. 范围锁定

### 2.1 本阶段包含

- 固定一个本地只读 Demo Repository，并绑定不可变 commit。
- 建立不少于 10 个 Eval Cases，实施目标为 15～20 个。
- 为每个 Case 编写需求、信息需求类型、期望事实、未知项、冲突和 PRD 验收标准。
- 实现 `Direct Prompt` Baseline：需求与固定上下文直接提交给模型，不调用仓库工具和历史检索。
- 实现可重复的 Baseline Runner、结果持久化和报告生成。
- 实现第一版确定性评测指标，并为主观质量指标保留 Rubric 和人工复核入口。
- 从 M0 开始统一使用 PostgreSQL，不实现 SQLite/PostgreSQL 双业务实现。
- 记录模型、Prompt、Case、仓库 commit、配置和每次 Trial 的版本信息。

### 2.2 本阶段不包含

- LangGraph `PrdWorkflowGraph` 或 `InvestigationGraph` 的正式实现。
- `repo_tree`、`search_text`、`read_file` 等 Repository Tools 的实现。
- Information Need Planner、Coverage、预算、重复检测和停止策略。
- Evidence、Fact、Source Grounding 和最终 PRD 的完整链路。
- Next.js、FastAPI、Web 工作台、人机确认和飞书写入。
- Celery、Outbox、生产级恢复、多租户、OIDC 和生产部署。
- pgvector、外部 Coding Agent 和远程仓库 API。

本阶段可以为后续模块定义接口和测试夹具，但不得以 Stub 伪装成已经实现的 Agent 能力。

## 3. 目标目录与产出物

建议建立以下目录；如果实现仓库采用不同的 Python 包名，保留相同的逻辑边界即可：

```text
eval/
  cases/                         # 版本化 Ground Truth Cases
  fixtures/demo-repo/            # 固定只读 Demo Repository
  configs/direct_prompt.yaml     # Baseline 配置
  reports/                       # 运行生成物，不提交未审查的大文件
  schemas/                       # Case、Result、Report Schema
src/prd_agent/eval/
  models.py                      # Pydantic 数据模型
  case_loader.py                 # Case 和 fixture 校验
  prompt_baseline.py             # Direct Prompt 输入组装和模型适配
  runner.py                      # Trial / Eval Run 调度
  metrics.py                     # 确定性指标和 Rubric 接口
  report.py                      # Markdown / JSON 报告
tests/eval/
  test_case_loader.py
  test_prompt_baseline.py
  test_runner.py
  test_metrics.py
  test_report.py
infra/local/
  docker-compose.yml              # 仅包含本地 PostgreSQL 依赖
docs/evals/
  2026-07-21-m0-step-1-baseline-report.md
```

必须提交的文档和配置：

- `eval/README.md`：运行前置条件、命令、数据版本和禁止事项。
- `eval/configs/direct_prompt.yaml`：模型、Prompt、Trial 数、超时和输出目录。
- `docs/evals/2026-07-21-m0-step-1-baseline-report.md`：真实运行后的报告，不预填未经运行验证的数字。
- `eval/cases/manifest.yaml`：Case 列表、数据集版本、Demo Repository commit 和 Ground Truth 版本。

## 4. 固定数据契约

### 4.1 Demo Repository 契约

Demo Repository 使用一个小型、可公开审查的业务示例，至少包含 API、参数校验、数据库 Schema、状态流转、权限判断、下游引用和测试。

固定规则：

- 所有文件内容都在仓库中版本化，Case 只引用 `resolved_commit_sha`，不引用浮动分支。
- 运行时默认离线，不允许从互联网读取额外代码或文档。
- Fixture 只读，Baseline 阶段不执行代码、不执行任意 Shell、不写回仓库。
- 每个 Case 必须记录 `repository_id`、`resolved_commit_sha` 和可选的文件定位，供后续工具阶段复用。
- 仓库内容、Case 期望和 Baseline 输出分开存储，避免模型输出污染 Ground Truth。

### 4.2 Eval Case 契约

每个 Case 使用 YAML 或 JSON 保存，最低字段如下：

```yaml
case_id: case-001
title: 字段校验规则变更
requirement: "……"
context: "……"
tags: [required_investigation, validation]
repository_id: demo-repo
resolved_commit_sha: "<40-char-sha>"
expected_information_needs:
  - type: REQUIRED
    category: validation_logic
    reason: "需求影响输入约束"
required_sources:
  - kind: source_file
    locator: "src/api/validators.py"
expected_facts:
  - fact_id: fact-001
    statement: "……"
    source_locators: ["src/api/validators.py:10-18"]
expected_unknowns: []
expected_conflicts: []
required_prd_sections:
  - requirement_summary
  - business_rules
  - acceptance_criteria
clarification_questions: []
```

Case 校验必须拒绝缺少 `case_id`、`requirement`、数据版本、Ground Truth 或 `resolved_commit_sha` 的条目。

案例集至少覆盖以下 10 种场景：纯新增需求、OPTIONAL 历史资料、REQUIRED 代码调查、字段类型或校验限制、状态流转、权限规则、数据库约束、历史 PRD 与代码冲突、工具无结果、用户澄清、跨文件引用、调查无进展或预算不足。目标集建议 15～20 个，场景可组合但不能用重复文本凑数。

### 4.3 Baseline Run 契约

每次 Trial 生成一条不可变结果记录，至少包含：

```yaml
eval_run_id: run-20260721-001
case_id: case-001
config_id: direct-prompt-v1
trial_no: 1
model_id: "<provider/model>"
prompt_version: direct-prompt-v1
dataset_version: eval-v1
repository_commit: "<40-char-sha>"
input_hash: "sha256:..."
output_hash: "sha256:..."
started_at: "2026-07-21T00:00:00Z"
duration_ms: 0
token_usage: {}
status: completed
output: "<model output>"
error: null
```

`input_hash` 必须覆盖规范化后的需求、上下文、固定 System Prompt、Prompt 版本和数据版本；`output_hash` 用于报告去重和回归比较。

## 5. 具体实施任务

### Task 1：初始化 M0 评测运行环境

目标：让开发者可以在本地启动 PostgreSQL，并以单条命令加载评测配置。

实现内容：

1. 添加 `infra/local/docker-compose.yml`，只启动 PostgreSQL 和健康检查。
2. 添加环境变量模板，区分数据库连接、模型凭证、模型名称和运行目录。
3. 添加最小数据库迁移，至少包含 `eval_dataset`、`eval_case`、`eval_run`、`eval_metric` 和 `eval_artifact`。
4. 使用 SQLAlchemy + Alembic；不添加 SQLite 兼容分支。
5. 添加 `eval/README.md`，说明启动、迁移、运行、报告查看和数据清理命令。

先写测试：

- 数据库连接失败时，命令输出可定位的配置错误。
- 迁移可重复执行，不产生重复表或重复索引。
- 同一 `eval_run_id` 重复写入不会生成两条业务结果。
- 测试和 Eval 代码不会读取 SQLite URL。

完成标准：新环境按 README 操作后，可以启动 PostgreSQL、执行迁移并加载 `manifest.yaml`。

### Task 2：创建并冻结 Demo Repository

目标：形成可审查、可复现、可供后续工具复用的固定代码样本。

实现内容：

1. 选择一个小型业务主题，覆盖 API、校验、数据模型、状态、权限、下游引用和测试。
2. 为每个关键事实补充最小可读代码，不加入无关框架和外部依赖。
3. 创建初始 commit，并将完整 SHA 写入 `eval/cases/manifest.yaml`。
4. 增加 fixture 校验脚本，校验文件存在、SHA 长度和内容哈希。
5. 明确 Demo Repository 的许可证、脱敏状态和可否被报告引用。

先写测试：

- 固定 commit 下所有必需 locator 可解析。
- 修改任一 fixture 文件后，校验脚本失败。
- 测试运行不会访问网络或执行仓库代码。

完成标准：在另一份干净目录中复制同一 fixture 和 manifest，生成的文件清单与 SHA 完全一致。

### Task 3：建立 Ground Truth Case 集

目标：把需求、信息缺口、当前事实和 PRD 验收要求变成结构化数据。

实现内容：

1. 实现 `src/prd_agent/eval/models.py` 的 Case、Need、Fact、Unknown、Conflict 和 PRD Requirement 模型。
2. 实现 `case_loader.py`，加载单 Case、Manifest 和数据集版本。
3. 先完成最低 10 个 Case，再补到 15～20 个目标集。
4. 每个 Case 至少配置 3 个可判断的 `required_prd_sections` 或验收条目。
5. 为每个 Case 编写 Ground Truth 说明，标注“必须事实”和“允许未知”，禁止把模型推测写成事实。
6. 把案例按 `no_investigation`、`optional_investigation`、`required_investigation`、`conflict`、`unknown`、`clarification` 等标签分组。

先写测试：

- Case 缺字段、重复 `case_id`、非法 Need 类型和空 Ground Truth 时加载失败。
- Manifest 引用不存在的 Case 或错误 commit 时加载失败。
- Case 顺序、规范化序列化和数据集版本固定，生成的 `input_hash` 稳定。

完成标准：所有 Case 可一次性加载，报告可以按标签、版本和仓库 commit 过滤。

### Task 4：实现 Direct Prompt Baseline

目标：建立不依赖任何 Agent 工具能力的第一条可比较基线。

实现内容：

1. 定义固定 System Prompt，说明角色、输出格式、不得假设未知事实和必须标注边界。
2. 定义 User Prompt 模板，只注入 Case 的 `requirement`、`context` 和固定允许上下文。
3. 禁止注册工具、仓库检索、历史 PRD 检索、外部 Coding Agent 和自动澄清。
4. 输出使用结构化 PRD 文档 Schema 或稳定 Markdown 模板，至少包含需求摘要、范围边界、业务规则、流程、验收标准、异常和待确认事项。
5. 记录 Prompt 版本、模型版本、输入规范化结果、原始输出、错误、延迟和 Token 使用。
6. 模型调用失败时记录失败 Run，不伪造空 PRD，不自动切换成另一模型配置。

先写测试：

- Baseline 请求不携带 Tool Schema，也不触发 Repository Adapter。
- 相同 Case、配置和版本生成相同 `input_hash`。
- 输出缺少必需章节时仍保存原始结果，并由评测层报告失败。
- 模型超时、限流和无效 JSON 分别映射到可区分的错误类型。

完成标准：使用 Stub Model 可以离线完成全套 Runner 流程；切换真实模型只需替换 Model Adapter，不改变 Case 和指标协议。

### Task 5：实现第一版评测指标

目标：先用确定性指标建立可重复的比较框架，再逐步加入模型 Judge 和人工复核。

确定性指标：

- `required_section_coverage`：必需章节是否存在。
- `requirement_coverage`：需求中的验收条目是否被覆盖。
- `boundary_recall`：范围内、范围外和待确认项是否分别表达。
- `acceptance_criteria_executability`：验收标准是否具备对象、条件、动作和预期结果。
- `unknown_preservation`：Ground Truth 要求保留的未知项是否被保留。
- `run_stability`：多 Trial 的成功率、输出哈希差异和指标离散程度。

首版评分型指标只定义 Rubric 和接口，不把模型 Judge 分数混入确定性总分。Evidence Precision、Verified Fact Accuracy 和 Unsupported Claim Rate 在 Direct Prompt 阶段标记为 `not_applicable`，待工具与 Grounding 阶段启用。

先写测试：

- 用固定的好、坏、缺章节和越界输出验证每个指标。
- 边界值、空输出和模型错误必须有明确结果。
- 指标计算不得修改原始 Run 或 Ground Truth。

完成标准：同一输入和同一结果重复计算得到相同 JSON 指标；指标版本变化时可以区分历史报告。

### Task 6：运行 Trial 并生成 Baseline 报告

目标：生成第一份真实可审查的基线报告，为后续 Ablation 提供参照。

实现内容：

1. 在配置中把目标 Trial 数设置为每种配置至少 3 次；最低 10 个 Case，目标 15～20 个。
2. 为每个配置生成唯一 `config_id`，至少包含模型、Prompt、数据集和指标版本。
3. Runner 支持断点续跑：已完成且输入哈希一致的 Run 不重复调用模型。
4. 结果写入 PostgreSQL，原始输出和报告附件写入受控 artifact 目录，并保存内容哈希。
5. 生成 Markdown 摘要、JSON 明细和失败轨迹清单。
6. 报告显示真实数据，不填充目标数字，不把小样本结果描述为普遍结论。

报告至少包含：

- 数据集版本、Case 数、仓库 commit、模型和 Prompt 版本。
- 每个 Case 的 Trial 状态、输入/输出哈希、延迟和 Token。
- 指标均值、最小值、最大值和离散程度。
- 至少一个失败案例、完整失败轨迹、失败分类和待修复项。
- 当前 Baseline 的已知限制，以及与下一阶段 Single Retrieval 的比较计划。

完成标准：删除报告目录后重新运行，可以生成同结构、可追溯、可复核的新报告；数据库中不存在重复 Run。

### Task 7：阶段验收与冻结

验收命令建议：

```bash
docker compose -f infra/local/docker-compose.yml up -d postgres
python -m prd_agent.eval.migrate
python -m prd_agent.eval.validate_dataset --manifest eval/cases/manifest.yaml
python -m prd_agent.eval.run_baseline --config eval/configs/direct_prompt.yaml
python -m pytest tests/eval -q
```

验收清单：

- [ ] Case 数量不少于 10 个，且已经排入补齐至 15～20 个的任务。
- [ ] Demo Repository 绑定固定 commit，所有 Ground Truth locator 可复核。
- [ ] 每种配置至少完成 3 次 Trial，失败 Run 也有记录。
- [ ] Baseline 未调用任何工具、检索或外部 Agent。
- [ ] 输入、输出、Prompt、模型、数据和仓库版本均可追溯。
- [ ] 指标可重复计算，报告未预填未经运行验证的结果。
- [ ] 至少有一个失败案例和回归记录。
- [ ] PostgreSQL 是唯一业务结果存储，不存在 SQLite 分支。

冻结产物后，才能进入总领方案的第二步：实现纯领域类型、Policy 和最小 PRD Workflow。第二步不得修改已冻结 Case 的 Ground Truth；若必须修订，必须提升数据集版本并保留旧报告。

## 6. 建议提交顺序

按可回滚的小提交拆分：

1. `chore: bootstrap m0 eval postgres`
2. `test: add eval case and dataset contracts`
3. `feat: freeze demo repository fixture`
4. `feat: add ground truth cases`
5. `feat: add direct prompt baseline runner`
6. `test: add deterministic eval metrics`
7. `feat: generate baseline report`
8. `docs: freeze m0 step 1 acceptance`

每个提交都应能单独通过已有测试；真实模型运行产生的原始大文件不直接提交 Git，只提交脱敏摘要、哈希和报告。

## 7. 与总领方案的对应关系

| 总领方案 | 本 Step Plan 的落实 |
| --- | --- |
| M0 Agent Core 可行性高 | 只验证评测、基线和可复现运行，不宣称完成产品 MVP |
| M0 统一 PostgreSQL | 本阶段即建立 PostgreSQL 连接、迁移和结果表 |
| 首版工具限定 | 本阶段不实现工具，但 Case 预留固定仓库和来源定位 |
| Grounding 硬门禁 | 本阶段只定义后续指标的 `not_applicable` 边界，不提前伪造 Grounding 能力 |
| Eval 至少 10、目标 15～20 | Case 与 Trial 验收直接锁定该目标 |
| Celery 只传递 `run_id` | 本阶段不引入 Celery，只为未来 Run 字段和幂等协议留接口 |
| 远程仓库固定 commit 快照 | Demo Repository 先采用本地固定 commit，远程 Adapter 后置复用相同协议 |

## 8. 阶段结论

第一步完成的标志不是“模型能生成 PRD”，而是“任何后续 Agent 改动都能在同一数据集、同一版本协议和同一 PostgreSQL 结果模型上被重复测量”。

只有在这份 Baseline 报告生成并审查后，才进入 Repository Tools 和 Evidence 实现；否则后续质量变化无法归因。

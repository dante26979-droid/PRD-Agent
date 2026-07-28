# M0 Agent Core 第三步实现方案与错误测试案例

> 文档类型：基于当前代码的 As-Built 实施方案与补强计划  
> 日期：2026-07-22  
> 对应总方案阶段：第 3 步——Repository Tools 与确定性 Evidence  
> 前置阶段：第 1 步 Eval Baseline、第 2 步最小可恢复 PRD Workflow

## 1. 文档目的

本方案以仓库中已经实现的代码为事实基线，对照总方案第 3 步，回答四个问题：

1. 当前第三步已经实现了什么，真实执行边界是什么。
2. 现有模块如何协作，哪些约束属于必须保持的不变量。
3. 哪些能力仍需补强，按什么顺序完成。
4. 哪些错误必须被自动化测试覆盖，以及发生错误时系统必须返回什么、绝不能做什么。

本文不重复早期概念设计，而是把设计收敛成可直接开发、评审和验收的实施清单。原始设计仍见：

- `docs/superpowers/plans/2026-07-21-m0-agent-core-step-3-repository-tools-evidence-design.md`
- `docs/architecture/2026-07-21-prd-agent-v1.1-feasibility-review.md`

## 2. 第三步在总方案中的位置

总方案的 M0 实施顺序是：

1. 固定 Demo Repository、Ground Truth 和 Direct Prompt Baseline。
2. 实现纯领域类型、Policy 和最小 PRD Workflow。
3. 实现 Repository Tools 和确定性 Evidence。
4. 实现 Information Need、Coverage 和 Investigation Loop。
5. 实现 Fact Extractor 与两阶段 Grounding。
6. 运行 Ablation，修正预算、停止和 Coverage 策略。

因此第三步只建立“可重复读取固定代码版本并产生可核查证据”的底座，不负责决定下一步调查什么，也不负责判断任意自然语言结论是否被语义支持。

### 2.1 本阶段范围

- 通过服务端 Repository Binding 将 `repository_id` 映射为受控本地仓库。
- 将 revision 解析为固定的 40 位 commit SHA。
- 仅从 Git Object 读取内容，不依赖可变工作树。
- 注册并执行六个版本化只读工具：
  - `repo_tree`
  - `search_text`
  - `read_file`
  - `parse_openapi`
  - `parse_database_schema`
  - `find_related_tests`
- 将工具输出归一化为 Evidence、Unknown、Deterministic Fact 和 Source Conflict。
- 对 Evidence 的仓库、commit、blob、locator、excerpt 和 hash 做确定性复核。
- 对工具调用做幂等记录和 PostgreSQL 持久化建模。
- 增加 Single Retrieval Eval 配置，与 Direct Prompt、Minimal Workflow 形成消融对比。

### 2.2 明确不在本阶段实现

- Information Need、Coverage 模板和多轮 Investigation Loop。
- 模型选择工具、重规划、停止策略和调查预算调度。
- 任意自然语言 Fact 抽取与语义 Grounding。
- `find_symbol`、`find_references`、Import Graph、事件消费者和状态机解析。
- Git 历史调查、历史 PRD、远程仓库、网络访问或任意 Shell。
- FastAPI、Next.js、异步队列和生产部署能力。

## 3. 当前实现盘点

### 3.1 已落地模块

| 能力 | 当前实现 | 状态 |
| --- | --- | --- |
| 应用编排 | `src/prd_agent/application/repository_evidence_service.py` | 已实现 |
| Repository Binding 与 Snapshot | `src/prd_agent/repository/bindings.py`、`models.py` | 已实现 |
| Git Object Reader | `src/prd_agent/repository/git_cli_reader.py` | 已实现 |
| 路径与内容策略 | `path_policy.py`、`content_policy.py` | 已实现 |
| Tool Schema 与 Registry | `src/prd_agent/tools/models.py`、`registry.py`、`default_registry.py` | 已实现 |
| 六个 Repository Tools | `src/prd_agent/tools/repository/` | 已实现 |
| Evidence 模型和归一化 | `src/prd_agent/evidence/models.py`、`normalizer.py` | 已实现 |
| 确定性证据校验 | `src/prd_agent/evidence/deterministic_validator.py` | 已实现 |
| Parser Fact 生成 | `src/prd_agent/evidence/fact_builder.py` | 已实现 |
| 确定性冲突检测 | `src/prd_agent/evidence/conflict_detector.py` | 已实现 |
| 内存存储 | `src/prd_agent/storage/memory_evidence.py` | 已实现 |
| PostgreSQL 存储 | `src/prd_agent/storage/postgres_evidence.py`、`infra/local/schema.sql` | 已实现，待真实数据库集成验证 |
| Single Retrieval Eval | `src/prd_agent/eval/single_retrieval.py`、`eval/configs/single_retrieval.json` | 已实现 |

### 3.2 已实现状态和错误语义

工具结果状态固定为：

- `SUCCEEDED`：完整成功。
- `PARTIAL`：有可用结果，但因预算或解析能力限制不完整。
- `EMPTY`：执行成功但没有匹配结果，不代表目标不存在。
- `FAILED`：执行或确定性校验失败。
- `BLOCKED`：因访问、安全或资源策略主动拒绝。

归一化规则是：

| ToolResult | Evidence | Unknown | Fact |
| --- | --- | --- | --- |
| `SUCCEEDED` | 有结果时生成 | 否 | 仅受信 Parser 可生成 |
| `PARTIAL` | 为已返回部分生成 | `PARTIAL_RESULT` | 仅为可复核部分生成 |
| `EMPTY` | 无 | `EMPTY_RESULT` | 无，禁止推导“不存在” |
| `FAILED` | 无 | `TOOL_FAILED` | 无 |
| `BLOCKED` | 无 | `ACCESS_BLOCKED` | 无 |

Repository 边界的公开错误码包括：

- `UNKNOWN_REPOSITORY`
- `INVALID_REVISION`
- `BLOCKED_PATH`
- `BLOB_NOT_FOUND`
- `BLOCKED_FILE_TYPE`
- `FILE_TOO_LARGE`

工具和服务层还使用：

- `READ_RANGE_LIMIT`
- `EXCERPT_BYTE_LIMIT`
- `MALFORMED_OPENAPI`
- `UNSUPPORTED_OPENAPI_VERSION`
- `BLOCKED_EXTERNAL_REFERENCE`
- `MALFORMED_DDL`
- `TOOL_EXECUTION_FAILED`
- `EVIDENCE_VALIDATION_FAILED`

### 3.3 当前已验证基线

截至本文生成时，仓库自动化测试基线为 63 个测试全部通过。第三步现有测试已经覆盖以下关键路径：

- 固定 commit 读取不受工作树修改影响。
- 精确文件范围读取和 locator 生成。
- Git symlink 被拒绝。
- 敏感内容在形成工具结果前脱敏。
- Tree 排序和路径范围限制。
- Search 精确定位、结果上限与 `PARTIAL`。
- OpenAPI 字段提取和外部引用拒绝。
- PostgreSQL DDL 字段解析。
- Related Test 的符号排序。
- Action Signature 规范化且不包含 `purpose`。
- Evidence hash 复核、篡改拒绝。
- 文本 Evidence 不会自动升级为 Fact。
- 只有受信 Parser 才能生成 `CODE_VERIFIED`、`SUPPORTED` Fact。
- 确定性 Fact 冲突被保留并标记为 `CONFLICTING`。
- 相同幂等键同输入重放、不同输入冲突。
- 未预期工具异常被收敛为公开 `TOOL_EXECUTION_FAILED`，不泄漏底层异常。
- Single Retrieval Eval 可以完成固定案例运行。

本轮补强还新增了：

- 非规范相对路径、显式空 revision 和 Git 超时的拒绝/错误封装。
- Repository Tool prefix 的敏感路径阻止。
- Evidence Validator 对敏感路径的二次策略校验。
- Parser 部分结果的 `PARSE_UNSUPPORTED` Unknown 语义。
- Normalizer/Fact/Conflict 处理异常的失败终态收敛。
- 读取范围、缺失 blob、二进制文件、OpenAPI 损坏/版本不支持、DDL 损坏/部分支持测试。

上述基线说明主链路已经成立，但还不等于错误边界和持久化行为已完整验收。

## 4. 当前执行链路

一次 Repository Tool 调用按以下顺序执行：

1. 接收严格的 `ToolAction`：工具 ID、Schema Version、Repository ID、固定 commit、结构化参数和 purpose。
2. 计算包含 Action 与任务上下文的输入哈希。
3. 按 `(actor_id, idempotency_key)` 查询历史结果：
   - 同输入直接重放；
   - 不同输入抛出 `IdempotencyConflict`。
4. Tool Registry 校验工具是否注册、版本是否匹配、参数是否满足 Pydantic Schema。
5. Reader 再次解析并确认 Repository Snapshot。
6. 保存 `RUNNING` ToolCallRecord。
7. 在异常边界内执行只读工具；未预期异常统一转换为 `FAILED/TOOL_EXECUTION_FAILED`。
8. 将 ToolResult 归一化为 Evidence 或 Unknown。
9. 对每条 Evidence 从固定 commit 重新读取并复核：
   - Repository ID；
   - resolved commit SHA；
   - blob ID；
   - 行号范围；
   - excerpt；
   - excerpt hash；
   - 脱敏策略。
10. Evidence 校验失败时丢弃候选证据，转为 `FAILED/EVIDENCE_VALIDATION_FAILED`。
11. 仅对 OpenAPI 与 DDL Parser 的结构化输出构造确定性 Fact。
12. 对相同 subject/predicate 的不同规范化 value 做冲突检测，保留所有来源。
13. 将终态 ToolCall、ToolResult 和 Evidence Bundle 一并持久化。

这条链路的核心不变量是：

- 没有固定 commit，就没有代码 Evidence。
- 没有通过 locator/hash 复核的 Evidence，就没有 Fact。
- `EMPTY`、`PARTIAL`、`FAILED`、`BLOCKED` 都必须成为显式 Unknown，不能被模型补成确定性结论。
- 任意错误都不能退化为读取工作树、扩大路径、执行 Shell 或访问网络。

## 5. 第三步后续实现计划

当前第三步不是从零实现，而是进入“错误契约补全和集成验收”阶段。建议按下面四个切片推进。

### Slice A：安全和输入错误契约补全（P0）

目标：证明所有入口在恶意、越界和不合法输入下默认拒绝，并返回稳定公开错误。

实施项：

1. 为 Registry 增加未知工具、错误 Schema Version、缺参、额外参数、错误类型测试。
2. 为 Binding 和 Snapshot 增加未知仓库、禁用仓库、非法 revision、不属于仓库的对象测试。
3. 为 Path Policy 增加绝对路径、`..`、NUL、反斜杠、敏感文件和依赖目录测试。
4. 为 Content Policy 增加二进制、非 UTF-8、超大文件、密钥模式和脱敏后 hash 一致性测试。
5. 确认公开 summary/error code 中不包含本地绝对路径、Git stderr、密钥或异常堆栈。

完成标准：P0 错误案例全部自动化，任何拒绝都不会生成 Evidence/Fact。

### Slice B：工具边界、Parser 和 Truthfulness 补全（P1）

目标：证明六个工具在空结果、截断、格式损坏和边界输入下仍保持确定性语义。

实施项：

1. 覆盖 Tree/Search/Read 的每一个 limit 边界和 `PARTIAL`/`BLOCKED` 转换。
2. 覆盖 OpenAPI 版本、局部引用、循环引用、外部引用、格式损坏和输入大小。
3. 覆盖 DDL 多行约束、过滤、部分不支持、全不支持和语法损坏。
4. 覆盖 Related Tests 的空结果、稳定排序、上限截断和二进制跳过。
5. 对每类 `EMPTY` 和 `PARTIAL` 断言 Unknown 语义，特别断言没有负向 Fact。

完成标准：每个工具至少具备成功、空、部分、失败/阻止四类代表性测试。

### Slice C：Evidence、幂等和 PostgreSQL 原子性（P1）

目标：证明证据无法伪造，重复或并发调用不会产生分叉记录，持久化不会留下半完成数据。

实施项：

1. 补齐 repository/commit/blob/line/excerpt/hash 各字段单独篡改的参数化测试。
2. 增加 Parser item 与 Evidence 数量、路径、locator 不匹配测试。
3. 增加相同幂等键并发请求测试，保证只存在一个逻辑执行结果。
4. 使用真实 PostgreSQL 运行事务集成测试：
   - ToolCall、Evidence、Fact、Unknown、Conflict 同事务提交；
   - 中途异常全部回滚；
   - 唯一约束阻止重复；
   - 不存在孤立 Evidence/Fact。
5. 设计并实现 `RUNNING` 失联恢复策略；建议将超时记录收敛为 `FAILED/WORKER_LOST`，并保留原调用审计信息。

完成标准：真实 PostgreSQL 集成测试通过；并发和崩溃恢复语义有确定结论。

### Slice D：Single Retrieval Eval 与回归门禁（P2）

目标：保证第三步接入不破坏前两步，并能量化一次仓库检索的收益和失败形态。

实施项：

1. 为 Single Retrieval 增加固定 commit 错误、工具空结果、工具阻止和工具失败案例。
2. 在 Eval 报告中增加工具调用成功率、Evidence 数量、Unknown 原因和错误码分布。
3. 固定 Direct Prompt、Minimal Workflow、Single Retrieval 三组配置并运行相同 Manifest。
4. 保证 Step 2 的确认边界、状态版本和恢复测试不受 Repository 模块影响。
5. 将完整 `pytest`、Eval 运行和 PostgreSQL 集成测试纳入持续集成门禁。

完成标准：三种配置可重复运行，失败可归因，前两步回归保持全绿。

## 6. 错误测试设计原则

每个错误测试至少验证四个维度：

1. **公开结果**：status、error code、public summary 是否稳定。
2. **真实性**：是否生成了正确 Unknown，是否错误地产生 Evidence 或 Fact。
3. **安全性**：是否泄漏绝对路径、密钥、Git stderr、异常类型或堆栈。
4. **持久化**：ToolCall 是否进入明确终态，是否存在重复或孤立记录。

错误测试优先使用真实临时 Git Repository，只有纯策略或异常注入场景使用 Stub/Fake。所有列表结果必须断言稳定排序，所有 hash 必须由测试重新计算而不是复用被测函数的输出。

## 7. 错误测试案例矩阵

“覆盖”列含义：

- `已有`：当前测试已经直接覆盖。
- `部分`：相关行为有测试，但错误分支或断言不完整。
- `待补`：当前没有直接自动化覆盖。
- `待实现`：产品代码本身还需要先补能力。

### 7.1 Tool Action 与 Registry

| ID | 场景与输入 | 期望结果 | 禁止行为 | 覆盖 |
| --- | --- | --- | --- | --- |
| ACT-ERR-01 | `tool_id` 未注册 | 在执行前拒绝，稳定 Registry 错误 | 不解析 snapshot，不保存 Evidence | 待补 |
| ACT-ERR-02 | 工具存在但 `tool_schema_version` 不匹配 | 在执行前拒绝 | 不降级到其他版本 | 待补 |
| ACT-ERR-03 | 参数缺少必填字段 | Pydantic validation error | 不使用默认猜测补齐 | 待补 |
| ACT-ERR-04 | 参数包含额外字段 | 因 `extra=forbid` 拒绝 | 不静默忽略未知参数 | 待补 |
| ACT-ERR-05 | 参数类型错误或超范围 | Schema 校验失败 | 不进入 Handler | 待补 |
| ACT-ERR-06 | commit 不是 40 位小写 SHA | `ToolAction` 构造失败 | 不接受 branch/tag 作为已解析 commit | 待补 |
| ACT-ERR-07 | 相同语义参数仅 key 顺序不同 | Action Signature 相同 | 不产生不同幂等身份 | 部分 |
| ACT-ERR-08 | 仅 `purpose` 不同 | Action Signature 相同 | 不让自然语言 purpose 改变工具身份 | 已有 |

### 7.2 Repository Binding、Revision 与 Snapshot

| ID | 场景与输入 | 期望结果 | 禁止行为 | 覆盖 |
| --- | --- | --- | --- | --- |
| SNAP-ERR-01 | 未配置的 `repository_id` | `UNKNOWN_REPOSITORY` | 不把 ID 当本地路径 | 待补 |
| SNAP-ERR-02 | 已配置但禁用的仓库 | `UNKNOWN_REPOSITORY` 或明确禁用错误 | 不读取仓库 | 待补 |
| SNAP-ERR-03 | revision 不存在 | `INVALID_REVISION` | 不回退到 HEAD | 待补 |
| SNAP-ERR-04 | revision 指向 tree/blob 而非 commit | `INVALID_REVISION` | 不把任意 Git Object 当 snapshot | 待补 |
| SNAP-ERR-05 | Action commit 与实际 snapshot 不一致 | 执行拒绝或 Evidence 校验失败 | 不混合两个版本 | 待补 |
| SNAP-ERR-06 | commit 后工作树发生修改 | 仍返回 commit 中内容 | 不读取工作树新内容 | 已有 |
| SNAP-ERR-07 | commit 后工作树删除文件 | 仍能从 Git Object 读取 | 不因工作树缺失变为 `BLOB_NOT_FOUND` | 待补 |
| SNAP-ERR-08 | Git 命令超时或 Git 仓库损坏 | 公开 Repository 错误 | 不泄漏 Git stderr/绝对路径 | 待补 |

### 7.3 路径与内容安全

| ID | 场景与输入 | 期望结果 | 禁止行为 | 覆盖 |
| --- | --- | --- | --- | --- |
| SEC-ERR-01 | 绝对路径 `/etc/passwd` | `BLOCKED/BLOCKED_PATH` | 不读取仓库外内容 | 待补 |
| SEC-ERR-02 | `../` 路径穿越 | `BLOCKED/BLOCKED_PATH` | 不做规范化后越界读取 | 待补 |
| SEC-ERR-03 | 路径包含 NUL | `BLOCKED/BLOCKED_PATH` | 不传给 Git/OS | 待补 |
| SEC-ERR-04 | Windows 反斜杠或盘符路径 | `BLOCKED/BLOCKED_PATH` | 不形成跨平台绕过 | 待补 |
| SEC-ERR-05 | `.env`、私钥、证书等敏感文件 | `BLOCKED/BLOCKED_PATH` | 不返回任何 excerpt | 待补 |
| SEC-ERR-06 | `.git`、依赖或构建目录 | `BLOCKED/BLOCKED_PATH` 或从候选中排除 | 不扫描无关大目录 | 待补 |
| SEC-ERR-07 | Git symlink 指向仓库内文件 | `BLOCKED/BLOCKED_FILE_TYPE` | 不跟随 symlink | 已有 |
| SEC-ERR-08 | Git symlink 指向仓库外文件 | `BLOCKED/BLOCKED_FILE_TYPE` | 不越界读取 | 已有行为，待补专门案例 |
| SEC-ERR-09 | 二进制文件 | `BLOCKED/BLOCKED_FILE_TYPE` 或搜索时跳过 | 不把二进制解码为文本 | 待补 |
| SEC-ERR-10 | 非 UTF-8 文本 | 明确阻止或稳定跳过 | 不以替换字符伪造 excerpt | 待补 |
| SEC-ERR-11 | 文件超过单文件上限 | `BLOCKED/FILE_TOO_LARGE` | 不部分读取后伪装成功 | 待补 |
| SEC-ERR-12 | 内容命中 token/password/private-key 模式 | 先脱敏，再构造 result/evidence/hash | 不在 ToolResult、Evidence、日志中出现原文 | 部分 |
| SEC-ERR-13 | 异常消息包含绝对路径和 secret | 公开错误只保留通用 summary/code | 不返回 provider details | 部分 |

### 7.4 `repo_tree`

| ID | 场景与输入 | 期望结果 | 禁止行为 | 覆盖 |
| --- | --- | --- | --- | --- |
| TREE-ERR-01 | scope path 不存在 | `EMPTY` + `EMPTY_RESULT` | 不生成“不存在”Fact | 待补 |
| TREE-ERR-02 | scope path 被策略阻止 | `BLOCKED/BLOCKED_PATH` | 不列出子项 | 待补 |
| TREE-ERR-03 | entry 数超过上限 | `PARTIAL` + truncation + `PARTIAL_RESULT` | 不返回超过预算的项 | 待补 |
| TREE-ERR-04 | 同一 snapshot 重复执行 | 字典序结果完全一致 | 不依赖文件系统遍历顺序 | 已有排序覆盖 |
| TREE-ERR-05 | tree 中包含 symlink/敏感目录 | 稳定过滤或标记，不跟随 | 不间接读取目标 | 待补 |

### 7.5 `search_text`

| ID | 场景与输入 | 期望结果 | 禁止行为 | 覆盖 |
| --- | --- | --- | --- | --- |
| SEARCH-ERR-01 | 无匹配 | `EMPTY` + `EMPTY_RESULT` | 不生成 negative Fact | 已有服务层同类覆盖 |
| SEARCH-ERR-02 | 匹配数超过结果上限 | `PARTIAL` + 正确 truncation | 不把部分结果标为完整 | 已有 |
| SEARCH-ERR-03 | 候选文件数超过上限 | `PARTIAL`，reason 指向候选预算 | 不继续无限扫描 | 待补 |
| SEARCH-ERR-04 | 扫描字节数超过上限 | `PARTIAL`，保留已得结果 | 不越过扫描预算 | 待补 |
| SEARCH-ERR-05 | query 含 `.*[]()` 等正则字符 | 按 literal 搜索 | 不解释为正则表达式 | 待补 |
| SEARCH-ERR-06 | 大小写不敏感选项 | 结果符合参数且排序稳定 | 不改变默认大小写语义 | 待补 |
| SEARCH-ERR-07 | 候选包含二进制、敏感或超大文件 | 跳过并保持确定性 | 不泄漏内容、不让单文件拖垮搜索 | 待补 |
| SEARCH-ERR-08 | scope 被阻止 | `BLOCKED/BLOCKED_PATH` | 不扩大到仓库根目录 | 待补 |

### 7.6 `read_file`

| ID | 场景与输入 | 期望结果 | 禁止行为 | 覆盖 |
| --- | --- | --- | --- | --- |
| READ-ERR-01 | 文件不存在 | `EMPTY` 或 `BLOB_NOT_FOUND`，按公开契约固定 | 不读取近似路径 | 待补 |
| READ-ERR-02 | `line_start=0` 或负数 | Schema 拒绝 | 不自动纠正为 1 | 待补 |
| READ-ERR-03 | `line_end < line_start` | Schema 拒绝 | 不交换范围 | 待补 |
| READ-ERR-04 | 请求行数超过上限 | `BLOCKED/READ_RANGE_LIMIT` | 不截断后伪装成功 | 待补 |
| READ-ERR-05 | start 超过文件末尾 | `EMPTY` + `EMPTY_RESULT` | 不返回最后一行 | 待补 |
| READ-ERR-06 | end 超过文件末尾 | 返回到 EOF，locator 与实际行一致 | 不伪造请求 end | 待补 |
| READ-ERR-07 | excerpt 字节超过上限 | `BLOCKED/EXCERPT_BYTE_LIMIT` | 不落库超大 excerpt | 待补 |
| READ-ERR-08 | 二进制/非 UTF-8/超大文件 | 对应 Content Policy 错误 | 不生成 Evidence | 待补 |
| READ-ERR-09 | 敏感内容被脱敏 | hash 基于最终脱敏 excerpt | 不保留原文 hash 与脱敏文本错配 | 部分 |
| READ-ERR-10 | 精确合法范围 | `SUCCEEDED`，line/blob/hash 可复算 | 不包含范围外行 | 已有 |

### 7.7 `parse_openapi`

| ID | 场景与输入 | 期望结果 | 禁止行为 | 覆盖 |
| --- | --- | --- | --- | --- |
| OAPI-ERR-01 | YAML/JSON 语法损坏 | `FAILED/MALFORMED_OPENAPI` + `TOOL_FAILED` | 不产生 Evidence/Fact | 待补 |
| OAPI-ERR-02 | 缺少或不支持 OpenAPI 版本 | `FAILED/UNSUPPORTED_OPENAPI_VERSION` | 不猜测版本 | 待补 |
| OAPI-ERR-03 | `$ref` 指向 HTTP/远程地址 | `BLOCKED/BLOCKED_EXTERNAL_REFERENCE` | 不访问网络 | 已有 |
| OAPI-ERR-04 | `$ref` 逃逸仓库或允许前缀 | `BLOCKED/BLOCKED_PATH` | 不读取越界文件 | 待补 |
| OAPI-ERR-05 | 合法本地 `$ref` | 固定 commit 内解析且 Evidence 可复核 | 不读取工作树版本 | 待补 |
| OAPI-ERR-06 | 本地 `$ref` 循环 | 稳定失败或阻止，错误码固定 | 不递归耗尽资源 | 待补 |
| OAPI-ERR-07 | 文档合法但无目标字段 | `EMPTY` + `EMPTY_RESULT` | 不生成空 Fact | 待补 |
| OAPI-ERR-08 | 文档或解析节点超过预算 | `BLOCKED` 或 `PARTIAL`，契约固定 | 不无限展开 | 待补 |
| OAPI-ERR-09 | Parser item 与源 locator 不一致 | `FAILED/EVIDENCE_VALIDATION_FAILED` | 不生成 Fact | 待补 |

### 7.8 `parse_database_schema`

| ID | 场景与输入 | 期望结果 | 禁止行为 | 覆盖 |
| --- | --- | --- | --- | --- |
| DDL-ERR-01 | SQL 语法损坏 | `FAILED/MALFORMED_DDL` | 不从残片猜测 Fact | 待补 |
| DDL-ERR-02 | 只有不支持语句 | `EMPTY` 或 `PARTIAL/PARSE_UNSUPPORTED`，契约固定 | 不标记完整成功 | 待补 |
| DDL-ERR-03 | 支持与不支持语句混合 | `PARTIAL/PARSE_UNSUPPORTED` + 有效 Evidence | 不丢弃已验证项 | 待补 |
| DDL-ERR-04 | table filter 无匹配 | `EMPTY` + `EMPTY_RESULT` | 不生成表不存在 Fact | 待补 |
| DDL-ERR-05 | 多行 constraint/default/type | 结构化结果和 locator 可复现 | 不截断表达式后生成错误 Fact | 待补 |
| DDL-ERR-06 | 超大 DDL/节点预算超限 | 明确 `BLOCKED` 或 `PARTIAL` | 不无界解析 | 待补 |
| DDL-ERR-07 | Parser 输出字段路径与源文件不一致 | `FAILED/EVIDENCE_VALIDATION_FAILED` | 不生成 `CODE_VERIFIED` Fact | 待补 |
| DDL-ERR-08 | 合法 PostgreSQL DDL | 生成字段 Evidence/Fact | 不让普通文本工具获得同等 Fact 权限 | 已有主路径 |

### 7.9 `find_related_tests`

| ID | 场景与输入 | 期望结果 | 禁止行为 | 覆盖 |
| --- | --- | --- | --- | --- |
| TEST-ERR-01 | 无相关测试 | `EMPTY` + `EMPTY_RESULT` | 不生成“没有测试”Fact | 待补 |
| TEST-ERR-02 | 同分候选多个 | 按稳定次级键排序 | 不依赖遍历顺序 | 部分 |
| TEST-ERR-03 | 候选超过结果上限 | `PARTIAL` + truncation | 不把部分结果标为完整 | 待补 |
| TEST-ERR-04 | 测试目录含二进制/敏感/超大文件 | 跳过并保持稳定结果 | 不读取或返回受阻内容 | 待补 |
| TEST-ERR-05 | symbol/path 含特殊字符 | literal 匹配，Schema 约束生效 | 不执行正则或 Shell | 待补 |
| TEST-ERR-06 | 明确 symbol 命中 | 排名高于仅路径相关项 | 不将排名解释为确定性语义支持 | 已有 |

### 7.10 Evidence、Fact、Unknown 与 Conflict

| ID | 场景与输入 | 期望结果 | 禁止行为 | 覆盖 |
| --- | --- | --- | --- | --- |
| EVD-ERR-01 | Evidence repository ID 被篡改 | `FAILED/EVIDENCE_VALIDATION_FAILED` | 不保存候选 Evidence/Fact | 待补 |
| EVD-ERR-02 | commit SHA 被篡改 | 同上 | 不跨版本复用 Evidence | 待补 |
| EVD-ERR-03 | blob ID 被篡改 | 同上 | 不只依赖 path | 待补 |
| EVD-ERR-04 | line start/end 被篡改 | 同上 | 不接受相同文本在错误位置 | 待补 |
| EVD-ERR-05 | excerpt 被篡改 | 同上 | 不保存错误片段 | 已有 |
| EVD-ERR-06 | excerpt hash 被篡改 | 同上 | 不重新覆盖攻击者提供的 hash 后放行 | 已有主行为，待补独立断言 |
| EVD-ERR-07 | 原文在多处重复但 locator 错位 | 校验 locator 对应的精确范围 | 不通过全文模糊查找放行 | 待补 |
| EVD-ERR-08 | 脱敏策略前后不一致 | Evidence 校验失败 | 不泄漏原始 secret | 待补 |
| FACT-ERR-01 | Search/Read/Tree/Test Evidence 请求升级 Fact | 返回 Evidence，Fact 为空 | 不自动生成 `CODE_VERIFIED` | 已有 |
| FACT-ERR-02 | Parser 没有有效 Evidence | Fact 为空 | 不生成无来源 Fact | 待补 |
| FACT-ERR-03 | Parser item 数与 Evidence 不一致 | 只生成一一对应且复核通过的 Fact，或整体失败 | 不错误配对 | 待补 |
| FACT-ERR-04 | 同 subject/predicate 不同 value | 保留双方并标记 `CONFLICTING` | 不自动选择一个覆盖另一个 | 已有 |
| FACT-ERR-05 | 语义相同、格式不同的 value | 规范化后不产生伪冲突 | 不因空格/大小写造成重复冲突 | 待补 |
| UNK-ERR-01 | 工具 `EMPTY` | `EMPTY_RESULT` Unknown | 不生成否定 Fact | 已有 |
| UNK-ERR-02 | 工具 `PARTIAL` | 保留 Evidence + `PARTIAL_RESULT` | 不把结果视为穷尽 | 部分 |
| UNK-ERR-03 | 工具 `FAILED` | `TOOL_FAILED` | 不生成 Evidence/Fact | 已有异常路径 |
| UNK-ERR-04 | 工具 `BLOCKED` | `ACCESS_BLOCKED` | 不把拒绝解释为不存在 | 部分 |

### 7.11 应用服务、幂等、并发与崩溃

| ID | 场景与输入 | 期望结果 | 禁止行为 | 覆盖 |
| --- | --- | --- | --- | --- |
| SVC-ERR-01 | 相同 actor/key/输入重复请求 | 重放同一逻辑结果 | 不再次执行工具 | 已有 |
| SVC-ERR-02 | 相同 actor/key、不同输入 | `IdempotencyConflict` | 不覆盖首次结果 | 已有 |
| SVC-ERR-03 | 不同 actor 使用相同 key | 彼此隔离 | 不跨 actor 重放 | 待补 |
| SVC-ERR-04 | 同 key 同输入并发到达 | 仅一次获得执行权，其他重放 | 不生成两个 ToolCall | 待补 |
| SVC-ERR-05 | Handler 抛出含敏感信息异常 | `FAILED/TOOL_EXECUTION_FAILED` | 不泄漏异常文本/类型 | 已有 |
| SVC-ERR-06 | Normalizer 抛出异常 | ToolCall 收敛到明确失败终态 | 不永久停留 `RUNNING` | 待实现 |
| SVC-ERR-07 | Validator 抛出确定性校验错误 | `FAILED/EVIDENCE_VALIDATION_FAILED` | 不保留候选 Evidence/Fact | 已有主行为 |
| SVC-ERR-08 | Store 在 `save_running` 后宕机 | 可识别并恢复/终结失联调用 | 不永久不可解释地 `RUNNING` | 待实现 |
| SVC-ERR-09 | Store 完成事务失败 | 整个 bundle 回滚，可安全重试 | 不出现半条 Evidence 链 | 待补 |
| SVC-ERR-10 | 查询不存在的 `tool_call_id` | 稳定 not-found 错误 | 不返回空伪结果 | 待补 |

### 7.12 PostgreSQL 持久化

| ID | 场景与输入 | 期望结果 | 禁止行为 | 覆盖 |
| --- | --- | --- | --- | --- |
| DB-ERR-01 | 完整成功调用 | ToolCall/Result/Evidence/Fact 原子提交 | 不出现孤立子记录 | 待真实 DB 验证 |
| DB-ERR-02 | `EMPTY` 调用 | ToolCall/Result/Unknown 原子提交 | 不插入 Evidence/Fact | 待真实 DB 验证 |
| DB-ERR-03 | Evidence 插入中途违反约束 | 整个事务回滚 | 不保留已插入前半部分 | 待真实 DB 验证 |
| DB-ERR-04 | 重复 `(actor_id, idempotency_key)` | 唯一约束和应用语义一致 | 不产生双执行 | 待真实 DB 验证 |
| DB-ERR-05 | 重复 Evidence/Fact 主键 | 明确冲突并回滚 | 不静默覆盖审计数据 | 待真实 DB 验证 |
| DB-ERR-06 | 外键目标 ToolCall 不存在 | 数据库拒绝 | 不允许孤立 Evidence | 待真实 DB 验证 |
| DB-ERR-07 | 数据库连接中断 | 调用返回可归因的基础设施失败 | 不伪装成工具 `EMPTY` | 待补 |
| DB-ERR-08 | terminal record 重启后读取 | 内容与提交前一致 | 不依赖内存缓存 | 待真实 DB 验证 |
| DB-ERR-09 | stale `RUNNING` 超过恢复阈值 | 转为 `FAILED/WORKER_LOST` 或被同输入安全接管 | 不无限悬挂 | 待实现 |

### 7.13 Single Retrieval Eval 与回归

| ID | 场景与输入 | 期望结果 | 禁止行为 | 覆盖 |
| --- | --- | --- | --- | --- |
| EVAL-ERR-01 | Fixture commit 与配置 commit 不一致 | Case 明确失败且可归因 | 不读取当前工作树蒙混通过 | 待补 |
| EVAL-ERR-02 | 单次检索返回 `EMPTY` | Eval 完成并记录 Unknown | 不生成不存在结论 | 待补 |
| EVAL-ERR-03 | 单次检索被阻止 | Eval 记录 `BLOCKED` 和错误码 | 不吞掉失败 | 待补 |
| EVAL-ERR-04 | 工具异常 | 当前 Case 失败或带失败证据完成，报告语义固定 | 不中断全部 Case | 待补 |
| EVAL-ERR-05 | Manifest 中 Case 缺少仓库上下文 | 配置校验失败 | 不隐式使用任意仓库 | 待补 |
| EVAL-ERR-06 | 重复运行相同配置和输入 | 结果结构、调用数和状态可重复 | 不因工作树变化漂移 | 部分 |
| EVAL-ERR-07 | 引入 Step 3 后运行 Direct Prompt | 原基线行为和指标仍通过 | 不隐式调用 Repository Tool | 已有全量回归覆盖 |
| EVAL-ERR-08 | 引入 Step 3 后运行 Minimal Workflow | Step 2 确认/恢复语义仍通过 | 不改变用户确认边界 | 已有全量回归覆盖 |

## 8. 优先执行的错误测试批次

### P0：安全与证据真实性

优先实现以下 18 类测试：

- ACT-ERR-01～06。
- SNAP-ERR-01、03、04、05。
- SEC-ERR-01～05、09、11、13。
- EVD-ERR-01～04、07。

P0 的退出标准是：所有未授权、越界、伪造或版本不一致输入都在形成 Fact 前被拒绝，且公开输出不泄漏敏感信息。

### P1：边界、Parser、幂等和数据库

随后实现：

- Tree/Search/Read 的预算和边界案例。
- OpenAPI、DDL 的 malformed/unsupported/reference 案例。
- Unknown 和 Fact 一一对应规则。
- 并发幂等、事务回滚、外键和唯一约束。
- stale `RUNNING` 的恢复策略。

P1 的退出标准是：每个工具都能稳定区分成功、空、部分、失败和阻止，真实 PostgreSQL 不产生半完成证据链。

### P2：Eval 可观测性与长期回归

最后补齐：

- Single Retrieval 的错误注入案例。
- 工具状态、错误码、Evidence/Unknown 数量指标。
- 三种 Eval 配置重复运行对比。
- 全量测试与 Eval CI 门禁。

## 9. 需要补强的产品代码

错误矩阵中有四项不是单纯补测试，而是当前实现还需要增强：

1. **异常终态兜底**：目前 Handler 异常和 EvidenceValidationError 已被收敛，但 Normalizer、Fact Builder、Conflict Detector 或 Store 完成阶段的异常仍可能让记录停在 `RUNNING`。应在应用服务外层增加失败终态和事务补偿边界。
2. **失联调用恢复**：增加 stale `RUNNING` 扫描与 `WORKER_LOST`/安全接管策略，并记录 `retry_of` 或等价关联。
3. **数据库并发幂等**：仅靠先查再写不足以证明并发安全，必须依赖唯一约束、锁或冲突后重放。
4. **Eval 工具指标**：当前 Single Retrieval 能运行，但尚未完整输出工具状态分布、Unknown 原因、Evidence 数量和错误码分布。

此外，当前确定性校验会重新读取并核对 Evidence，但 Parser Fact 的独立“重新解析并复现”能力仍应作为后续硬化项。现阶段 Fact Builder 使用本次受信 Parser 的结构化结果，不等同于用第二个独立步骤完整重跑 Parser。

## 10. 单测和集成测试组织建议

建议在现有目录上增量扩展：

```text
tests/
├── application/
│   ├── test_repository_evidence_service.py
│   └── test_repository_evidence_failures.py
├── evidence/
│   ├── test_validator_failures.py
│   ├── test_fact_builder_failures.py
│   └── test_normalizer_truthfulness.py
├── repository/
│   ├── test_binding_failures.py
│   ├── test_path_policy.py
│   ├── test_content_policy.py
│   └── test_snapshot_failures.py
├── tools/
│   ├── test_registry_failures.py
│   └── repository/
│       ├── test_repo_tree_failures.py
│       ├── test_search_text_failures.py
│       ├── test_read_file_failures.py
│       ├── test_parse_openapi_failures.py
│       ├── test_parse_database_schema_failures.py
│       └── test_find_related_tests_failures.py
├── storage/
│   └── test_postgres_evidence_integration.py
└── eval/
    └── test_single_retrieval_failures.py
```

建议把临时 Git 仓库 fixture 统一扩展为包含：

- 两个 commit 和未提交工作树修改。
- 普通文本、重复文本、二进制、非 UTF-8、超大文件。
- 仓库内外 symlink。
- `.env`、PEM、token/password 示例。
- 合法/损坏/循环引用的 OpenAPI。
- 合法/部分支持/损坏的 PostgreSQL DDL。
- 多个同分测试文件。

数据库测试必须使用真实 PostgreSQL，不应用 SQLite 模拟 PostgreSQL 事务和约束语义。无数据库环境时可以跳过集成测试，但发布门禁环境不得跳过。

## 11. 验收标准

第三步完成需同时满足：

1. 六个工具均只读取固定 commit 的 Git Object，无法访问任意路径、网络或 Shell。
2. 每个工具的 `SUCCEEDED/PARTIAL/EMPTY/FAILED/BLOCKED` 语义有自动化测试。
3. 所有 Evidence 都可通过 repository/commit/blob/locator/excerpt/hash 独立复核。
4. 非 Parser 工具永远不会自动生成 `CODE_VERIFIED` Fact。
5. Parser Fact 必须有至少一条已复核 Evidence，冲突不得被自动覆盖。
6. `EMPTY` 和 `PARTIAL` 必须形成明确 Unknown，不能推出否定结论。
7. 幂等键在重复、冲突和并发情况下行为一致。
8. ToolCall 与 Evidence Bundle 在真实 PostgreSQL 中原子提交，失败不留孤立记录。
9. 失联 `RUNNING` 调用可以被检测并进入可审计终态。
10. 公开错误、ToolResult、Evidence、日志和 Eval 报告不泄漏密钥、绝对路径和 provider 异常细节。
11. Direct Prompt、Minimal Workflow、Single Retrieval 三套 Eval 均可重复运行。
12. Step 1、Step 2 和 Step 3 全量测试保持通过。

## 12. 进入第四步前的交付物

在开始 Information Need、Coverage 和 Investigation Loop 前，应交付：

- 本文错误矩阵中全部 P0、P1 自动化测试。
- PostgreSQL 原子性与并发幂等测试报告。
- stale `RUNNING` 恢复策略及实现。
- Single Retrieval 工具级指标报告。
- 固定 commit 的 Demo Repository Fixture 和可重现运行命令。
- 更新后的公开错误码清单和 Tool Schema 版本说明。

完成这些交付物后，第四步可以安全地在此底座上循环调用工具，而无需重新定义代码事实、证据真实性和错误语义。

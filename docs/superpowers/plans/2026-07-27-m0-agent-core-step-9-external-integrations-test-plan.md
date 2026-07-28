# M0 Agent Core 第九步测试计划：受控外部集成

> 文档状态：核心自动化已实现；完整发布矩阵待 Sandbox、Browser E2E 与 Ablation 结论  
> 设计日期：2026-07-27  
> 最近执行：2026-07-27  
> 对应设计：`2026-07-27-m0-agent-core-step-9-external-integrations-design.md`  
> 测试对象：GitHub/GitLab 只读 Adapter、飞书导出、外部 Coding Agent Adapter

## 1. 测试目标

本计划验证的不是“Provider API 能返回 200”，而是以下业务结论：

1. 远程仓库调查与本地调查遵守同一只读、固定 Commit、Evidence 和 Grounding 合同。
2. 未授权资源、凭证、敏感代码和 Provider 原始错误不会越过服务端安全边界。
3. 飞书创建与覆盖必须基于当前已完成 PRD、不可变预览和显式确认。
4. 超时、限流、响应丢失、并发与重试不会重复创建或错误覆盖文档。
5. 外部 Coding Agent 的输出只有在本系统重读、校验和 Grounding 后才可成为事实。
6. Native/External 比较固定唯一变量，能形成可复现的保留或关闭结论。

### 1.1 当前自动化覆盖记录

已落地的自动化覆盖：

- Credential 不透明、HTTPS 目标校验、Provider 错误正文不泄漏和响应大小限制。
- GitHub/GitLab Commit、Tree、Blob、分页、凭证仅 Header、限流语义和 `Retry-After` 上限。
- 分支移动后继续读取固定 Commit、远程 Reader 复用现有 Repository Tool，以及授权失败不降级成 Not Found。
- 飞书格式化确定性、创建块映射、完成态/版本/绑定/确认策略、响应未知人工复核、串行与并发执行幂等、并发预览原子幂等。
- PostgreSQL Migration、受保护 External ID、预览和执行幂等预留/回放。
- 外部 Agent 正确候选、伪造 Locator、越权路径、敏感信息脱敏、Native 默认/显式回退和固定 Ablation。
- API Schema、跨任务 Intent 拒绝、公开 Read Model 与事件 Payload 白名单；Web 预览后确认并展示安全链接。

尚未作为本次完成项：

- S9 的真实 Provider Sandbox Suite。
- Step 9 三个浏览器全栈 E2E 文件。
- 测试计划中所有大仓库、Redirect、崩溃点和 Provider 故障矩阵的穷举。
- 外部 Coding Agent 真实调用的质量、费用与时延门禁报告。

验收命令：

```bash
venv/bin/python -m compileall -q src tests
venv/bin/pytest -q
PRD_AGENT_TEST_DATABASE_DSN=... venv/bin/pytest -q tests/storage/test_postgres_step9_integration.py
cd web && npm run typecheck && npm test && npm run build
```

## 2. 测试原则

1. **标准 CI 不依赖真实外网**：使用协议级 Fake Server、固定 Fixture 和故障注入。
2. **Provider Contract 共用**：GitHub/GitLab、Fake/真实 Sandbox 运行同一语义合同。
3. **安全断言优先**：权限、凭证、目标绑定和未复核 Evidence 属于硬门禁。
4. **幂等必须测未知结果**：不能只测“请求失败前未写入”，必须测 Provider 已写入但响应丢失。
5. **版本必须可移动**：测试分支移动、任务版本变化和远程文档 Revision 变化。
6. **不以 Mock 调用次数替代业务结果**：最终断言数据库唯一性、公开 Read Model、远程 Fake 状态和审计记录。
7. **真实 Sandbox 独立运行**：Provider 凭证缺失可以跳过 Sandbox Suite，但 P0 Fake/Contract Suite 不得 Skip/XFail。

## 3. 测试层级

| 层级 | 范围 | 外部网络 | 发布属性 |
| --- | --- | --- | --- |
| Unit | Policy、Hash、Formatter、Validator、Error Mapping | 无 | 每次提交阻断 |
| Provider Contract | Tree/Blob、Create/Overwrite、幂等、错误语义 | 本地 Fake HTTP | 每次提交阻断 |
| Integration | PostgreSQL、Application Service、并发、恢复 | 本地 Fake HTTP | 每次提交阻断 |
| API | owner、Schema、版本、Idempotency、Read Model、SSE | 本地 Fake HTTP | 每次提交阻断 |
| Web Component | 预览、确认、状态和安全链接 | Mock Service Worker/Fake API | 每次提交阻断 |
| Browser E2E | 远程来源与飞书完整主路径 | 本地全栈 + Fake Provider | 合并/发布阻断 |
| Eval/Ablation | Native 与 External 固定 Case | 可重复 Fixture | 发布阻断 |
| Provider Sandbox | GitHub/GitLab/飞书真实测试资源 | 受控外网 | 发布前/定时 |

## 4. 建议目录

```text
tests/
  integrations/
    test_credentials.py
    test_error_mapping.py
    test_audit_redaction.py
  repository/
    remote/
      provider_contract.py
      test_github_adapter.py
      test_gitlab_adapter.py
      test_remote_object_reader.py
      test_remote_grounding.py
  export/
    test_formatter.py
    test_export_policy.py
    test_export_service.py
    test_feishu_adapter.py
    test_export_concurrency.py
    golden/
  external_investigator/
    test_adapter_contract.py
    test_candidate_validator.py
    test_profile_router.py
    test_security.py
  api/
    test_repository_bindings.py
    test_feishu_exports.py
    test_integration_events.py
  integration/
    test_step9_postgres.py
    test_step9_recovery.py

web/
  components/
    *.test.tsx
  e2e/
    step9-remote-repository.spec.ts
    step9-feishu-export.spec.ts
    step9-security.spec.ts

eval/
  cases/
    external-investigator/
  fixtures/
    providers/
  reports/
```

## 5. 固定 Fixture

### 5.1 Repository

至少准备：

- `repo-small`：OpenAPI、Schema、Service、Tests 和中文注释齐全。
- `repo-branch-move`：`main` 可从 Commit A 移到 Commit B。
- `repo-sensitive`：包含 Token、私钥、连接串、`.env` 和 Prompt Injection 文本。
- `repo-large`：分页 Tree、超大文本、Binary、LFS Pointer、Submodule、Symlink。
- `repo-conflict`：Schema、API、实现和测试存在可识别冲突。
- `repo-unicode`：中文路径、空格、组合字符和不同换行。

GitHub/GitLab Fake 以不同 Provider 响应表示同一逻辑仓库。

### 5.2 飞书

Fake Provider 保存：

- Folder/Document/Revision。
- 创建请求的 Provider Idempotency Metadata。
- 块列表和写入次数。
- 权限状态、限流计数和可注入故障。
- “写入成功后丢弃响应”的故障模式。
- 远程人工编辑导致 Revision 变化的模式。

### 5.3 外部 Coding Agent

固定响应类型：

- 正确 Locator。
- 不存在文件。
- 错误行号或 Symbol。
- 其他 Commit 的内容。
- 越权路径。
- 重复候选。
- 超预算候选。
- 带敏感正文/凭证样式的候选。
- 服从 Repository Prompt Injection 的恶意动作。
- 超时、结构非法、部分结果和 Provider 限流。

## 6. 通用集成合同测试

### S9-COMMON-001：Credential 不透明

步骤：

1. 配置包含可识别 Marker 的测试 Token。
2. 执行成功、失败、超时、限流和非法响应。
3. 检查数据库正文、API、SSE、日志、Trace、Metrics 和测试 Artifact。

断言：

- Marker、Authorization Header、Cookie 和 Credential Ref 解析值出现次数为 0。
- 业务对象只保存允许的内部 `connection_id/credential_ref`。

### S9-COMMON-002：资源白名单先于 Provider 调用

- 请求不属于 owner 或不在 `allowed_resource_ids` 的 Binding。
- 断言返回稳定拒绝，Fake Provider 调用次数为 0。
- 不返回资源存在性、仓库名、外部 ID 或 Provider 差异。

### S9-COMMON-003：标准错误映射

参数化 Provider 的 401、403、404、409、429、5xx、Timeout、Invalid JSON 和 Oversized Response。

断言：

- 映射到稳定 `IntegrationErrorCode` 和正确 `retryable`。
- 公开消息不含 Provider 原始正文/Header/堆栈。
- 429 的 `Retry-After` 被限制在系统允许范围。

### S9-COMMON-004：有界重试

- 可重试错误使用配置的有限退避。
- 非幂等或业务冲突不自动重试。
- 达到上限后状态稳定，不继续后台调用。

### S9-COMMON-005：审计最小化

- 每次允许/拒绝/失败操作都有主体、任务、动作、目标 Binding、时间和结果。
- 审计不保存代码摘录、PRD 正文、外部 ID 明文或凭证。

### S9-COMMON-006：Owner 隔离

- Owner A 不能读取或触发 Owner B 的 Connection、Repository Binding、Snapshot、Document Binding、Intent、Export Run 或 External Investigation Run。
- 404/403 策略不能泄露跨 Owner 资源存在性。

## 7. Remote Repository Provider Contract

同一测试 Suite 必须由 Fake GitHub 和 Fake GitLab Adapter 运行。

### S9-REPO-001：Ref 解析为完整 Commit

- 输入 Branch、Tag 和完整 SHA。
- 输出统一完整 Commit SHA。
- 不存在或缩写歧义返回 `VERSION_NOT_FOUND`。

### S9-REPO-002：固定 Commit 读取

1. `main` 解析到 Commit A。
2. Provider 把 `main` 移动到 Commit B。
3. 使用已保存 Snapshot 读取文件。

断言内容、Tree、Action Signature 和 Evidence 全部仍属于 Commit A。

### S9-REPO-003：Tree 分页

- 覆盖空页、单页、多页、重复 Page Token、循环 Token、缺失 Token 和超页数。
- 合并结果稳定去重、排序确定。
- 循环/超限停止为标准错误或 `PARTIAL`，不无限请求。

### S9-REPO-004：Blob 完整性

- 校验 Provider Content Hash/ETag 与实际 Bytes。
- Provider 声称长度与正文不符时拒绝。
- 编码、CRLF、无末尾换行和 Unicode 不改变定位规则。

### S9-REPO-005：路径策略

参数化：

- `../secret`
- 绝对路径
- 双重 URL 编码
- NUL/控制字符
- Unicode 规范化碰撞
- Symlink 越界

断言均在 Provider 调用前或内容返回边界拒绝。

### S9-REPO-006：特殊对象

Binary、LFS Pointer、Submodule、Symlink、空文件和超大文件产生明确类型化结果；不得当作普通文本或“文件不存在”。

### S9-REPO-007：下载预算

- Tree 数、文件数、单文件、总字节和 Provider 调用分别达到上限。
- 断言硬停止、Coverage 标记未检查项、Stop Reason 正确。
- 已保存 Evidence 保留，不执行额外请求。

### S9-REPO-008：限流与授权失效

- 调查中途 429/401。
- 断言有限重试后 `PARTIAL`/`REAUTH_REQUIRED`。
- 已固定 Snapshot 与已验证 Evidence 可恢复。
- 不把授权失败转成 Empty。

### S9-REPO-009：404 与未授权不可枚举

对真实不存在和无权限资源，公开响应的状态、大小和文案不暴露可利用差异；内部审计仍能保留标准原因。

### S9-REPO-010：Provider Redirect

- 允许 Host 内重定向可继续。
- 外部 Host、HTTP 降级和携带 Credential 的重定向被拒绝。

## 8. Repository Object Reader 与现有 Tool 回归

### S9-READER-001：本地/远程合同等价

对同一 Git Object Fixture，分别通过 `GitCliObjectReader`、GitHub 和 GitLab Reader 执行：

- `read_file`
- `search_text`
- `parse_openapi`
- `parse_database_schema`
- `find_related_tests`

断言标准化 Tool Result、Action Signature 输入和 Evidence 语义等价；Provider 元数据只出现在允许字段。

### S9-READER-002：Action Signature

- 相同 Tool、标准化参数和 Commit 只执行一次。
- 可移动 Branch 名、调用时间、分页 Token 和 Provider Request ID 不进入已固定 Action Signature。
- Provider/内部 Repository 身份和 `access_scope_hash` 通过 `SourceBinding` 进入 Signature。
- Commit 改变必须产生新 Signature。
- 授权范围改变必须产生新 Signature，旧结果不得重放。

### S9-READER-003：统一内容策略

每个 Tool 在远程路径上重复敏感 Fixture 参数化测试：

- Token
- 密码
- 私钥
- DSN
- Cookie
- OpenAPI Example Secret
- Schema Default Secret
- Test Fixture Secret

Tool Result、Evidence、Read Model、Context 和日志均只出现脱敏值。

### S9-READER-004：Evidence Locator

- Evidence 使用内部 Repository ID、完整 Commit、规范路径和准确行号。
- 公开链接使用固定 Commit。
- Branch 移动后链接和 Grounding 仍定位原内容。

### S9-READER-005：Grounding

- 正确摘录通过。
- 错 Commit、错 Path、错行号、Hash 不符和脱敏前后不一致被拒绝。
- Provider Search 摘要本身不能直接支持 Fact。

### S9-READER-006：恢复

Snapshot、Tool Call 或 Evidence 保存后重启 Application：

- 从已保存 Commit/Action 继续。
- 不重新解析到新 Branch。
- 不重复成功 Tool Action。

## 9. 飞书 Formatter Unit/Golden Test

### S9-FMT-001：支持格式

Golden 覆盖：

- 文档标题。
- H1/H2/H3。
- 普通段落。
- 有序/无序列表及合理嵌套。
- 表格。
- 引用。
- 加粗和安全超链接。
- Code Block、Divider 的定义降级或映射。
- 中文、Emoji、特殊标点和长英文单词。

### S9-FMT-002：不支持格式降级

HTML、复杂嵌套表格、脚注、任务列表、图片、Mermaid 和未知节点降级为可读文本；单块失败不丢失后续章节。

### S9-FMT-003：确定性

相同文档快照重复格式化 100 次：

- Block 顺序和序列化 Bytes 相同。
- `content_hash` 相同。
- 时间、随机 ID 和数据库读取顺序不影响 Hash。

### S9-FMT-004：Hash 敏感性

- 语义内容、标题、未解决事项或链接目标变化会改变 Hash。
- 仅允许忽略的换行/尾随空白变化按规范不改变 Hash。

### S9-FMT-005：块大小与切分

- 单段、表格、列表和全文达到 Provider 限制。
- 切分不破坏 Unicode、链接、加粗范围或列表顺序。
- 无法安全切分时返回 `PAYLOAD_TOO_LARGE`，不发送部分文档。

### S9-FMT-006：内部内容移除

输入包含 Agent Step、Run ID、内部 Trace、私有推理、Credential Marker 和未公开 Evidence 字段。

断言输出均不包含；允许公开的假设、风险、Unknown 和来源保留。

### S9-FMT-007：安全链接

- `https` 且 Host Allowlist 内保留。
- `javascript:`、`data:`、凭证 URL、未知 Host 和控制字符链接降级为纯文本。

## 10. Export Policy

### S9-POLICY-001：只有 Completed 可预览

参数化所有任务阶段。只有 `COMPLETED` 返回 Preview；其他阶段不创建 Intent。

### S9-POLICY-002：预览不写入

生成 Preview 后 Fake Feishu Document 数和调用次数仍为 0。

### S9-POLICY-003：版本固定

Preview 后修改任务版本、文档版本、章节状态或内容：

- 原 Intent 失效。
- 写命令返回 409/稳定业务错误。
- Provider 调用次数为 0。

### S9-POLICY-004：确认绑定

Confirmation 只能用于相同 owner、task、intent、mode、document version 和 content hash；过期、已用或篡改确认被拒绝。

### S9-POLICY-005：创建/覆盖模式

- 无 Binding 只能 `CREATE`。
- 有 Binding 不能再次 `CREATE`。
- 无 Binding 不能 `OVERWRITE_BOUND`。
- 模式错误在 Provider 调用前拒绝。

### S9-POLICY-006：禁止任意文档 ID

- API Schema 对 `external_id/document_id/url` 使用 `extra=forbid`。
- 即使绕过 API 直接调用 Application，也只能通过 Binding 解析目标。

### S9-POLICY-007：任务删除

- 删除任务不调用 Feishu Delete。
- 外部 Fake 文档保持存在。
- 本地敏感绑定引用按删除策略清理，最小审计保留。

## 11. 飞书创建、幂等与恢复

### S9-EXPORT-001：首次创建

- 完成 Preview、确认和创建。
- 远程恰好一个文档。
- 本地恰好一个 Binding、一个成功 Export Run。
- Hash、版本、Revision 和 Safe URL 正确。

### S9-EXPORT-002：HTTP 重复提交

相同 `Idempotency-Key` 和相同业务输入并发/串行提交：

- 返回同一 Export Run/结果。
- Provider 逻辑创建一次。

### S9-EXPORT-003：Idempotency Key 冲突

相同 Key 配不同 Task、Mode、Version 或 Hash：

- 返回 `IDEMPOTENCY_CONFLICT`。
- 不调用 Provider。

### S9-EXPORT-004：写入成功后响应丢失

Fake Provider 创建文档、记录 Metadata 后抛 Timeout：

1. Run 进入结果未知/可重试状态。
2. 使用原 Key 重试。
3. Adapter 根据 Metadata 查回原文档。

断言最终只有一个远程文档和一个 Binding。

同一 Case 还要用 `CreateIdempotencyCapability.NONE` 运行：

- Run 进入 `MANUAL_REVIEW`。
- 自动重试入口关闭。
- Provider Create 调用仍为 1。
- 系统不以标题模糊搜索猜测目标，也不建立未经确认的 Binding。

### S9-EXPORT-005：进程崩溃点

分别在以下位置模拟崩溃：

- Run 标记 `RUNNING` 后、调用前。
- Provider 成功后、数据库提交前。
- Binding 插入后、Event 写入前。
- 响应返回前。

恢复后不重复创建，Run/Binding/Event 最终一致。

### S9-EXPORT-006：并发首次创建

10 个并发请求使用相同和不同 HTTP Key：

- 业务唯一约束确保只有一个逻辑创建成功。
- 其他请求返回已存在/冲突/同一结果。
- 不产生孤儿 Binding 或第二份远程文档。

### S9-EXPORT-007：Provider 失败不改变完成态

401、429、5xx、Timeout、Invalid Response、Payload Too Large 后：

- Task 仍为 `COMPLETED`。
- PRD 版本、确认单元和正文不变。
- Export Run 保存安全错误和可重试性。

### S9-EXPORT-008：事务不跨网络

使用 Instrumented Repository/Fake Provider 断言外部调用期间无打开的业务写事务/被长期占用的连接。

## 12. 飞书覆盖

### S9-OVERWRITE-001：绑定目标

Task A/B 各绑定不同文档。Task A 覆盖时 Fake Provider 只收到 A 的目标；请求无法注入 B 的 ID。

### S9-OVERWRITE-002：二次确认

- Preview、普通确认文案或首次创建确认不能用于覆盖。
- 只有明确 `OVERWRITE_BOUND` Confirmation 生效。
- UI 和 API 均显示覆盖风险。

### S9-OVERWRITE-003：整份替换

覆盖后远程文档等于当前 ExportDocument，不保留旧 PRD 块；不支持的格式按 Formatter 规则降级。

### S9-OVERWRITE-004：Revision 变化

Preview 后模拟远程人工编辑：

- 条件写返回 `RESOURCE_CHANGED`。
- 不覆盖远程内容。
- 旧确认失效，要求重新预览。

### S9-OVERWRITE-005：重复覆盖

同一版本/Hash 重复覆盖：

- 返回已有成功结果或无操作成功。
- Provider 不重复执行有副作用的替换。

### S9-OVERWRITE-006：覆盖失败

替换中 Provider 返回失败时：

- 若 Provider 支持原子替换，旧文档保持完整。
- 若 Provider 分块写不具备原子性，Adapter 必须进入明确 `FAILED_PARTIAL_REMOTE` 类状态并阻止自动重试，测试验证用户可见恢复说明。
- 不得错误更新 `last_export_hash/provider_revision`。

## 13. Feishu Provider Contract

### S9-FEISHU-001：块映射

对 Formatter Golden 中每种块验证 Provider 请求语义，不依赖 SDK 内部类快照。

### S9-FEISHU-002：安全 URL

Provider 返回：

- 合法飞书 URL。
- 未知 Host。
- HTTP URL。
- 包含用户信息/Token 的 URL。
- CRLF/控制字符 URL。

仅合法 Allowlist URL 进入 Binding。

### S9-FEISHU-003：External ID 保护

Provider External ID 明文不出现在 API、SSE、普通日志和 Metrics；数据库按设计使用密文/受保护字段。

### S9-FEISHU-004：Provider 限额

请求数、块数、正文大小达到 Provider 限额时，Adapter 预先切分或稳定失败，不做无限递归重试。

### S9-FEISHU-005：权限变化

创建成功后撤销权限，再覆盖：

- Binding 保留。
- Export Run 返回 `REAUTH_REQUIRED/UNAUTHORIZED`。
- Task 保持完成，且不创建新文档。

### S9-FEISHU-006：创建幂等能力探测

Provider Contract/Sandbox 分别验证 `PROVIDER_KEY`、`RECONCILABLE` 或 `NONE` 的真实语义：

- 只有实际通过“响应丢失后重复/核对仍定位同一资源”的能力才能声明前两类。
- SDK 暴露请求 ID、标题搜索或普通重试参数不足以单独证明创建幂等。
- 能力结果进入 Adapter 配置和发布报告；`NONE` 必须关闭结果未知后的自动创建重试。

## 14. External Coding Agent Adapter

### S9-EXT-001：固定输入

捕获发给外部 Adapter 的请求：

- 含固定 Repository Binding/Commit、Question、Coverage、允许路径和预算。
- 不含 Token、其他任务正文、飞书连接、可写路径或未授权资源。

### S9-EXT-002：Schema 严格

未知字段、超长文本、负行号、反向行范围、过多候选、非法状态和非法动作均被拒绝/最小化。

### S9-EXT-003：正确候选复核

外部 Agent 返回正确 Locator：

- 本系统 Reader 重新读取。
- Normalizer/Validator 创建 Evidence。
- Grounding 后才创建 Fact。

### S9-EXT-004：不存在文件

候选文件不存在：

- 记录候选验证失败。
- Evidence/Fact 数为 0。
- Coverage 保持缺失，不推断业务事实不存在。

### S9-EXT-005：Commit 不一致

候选内容只存在于 Branch 最新 Commit B，但请求固定 Commit A：

- 必须按 A 重读并拒绝。
- 不允许外部 Agent 的摘录替代读取。

### S9-EXT-006：行号/Hash 不符

错误行号、Symbol 或 Candidate Claim 与代码不符时：

- Evidence Validator 拒绝或缩小到真实可支持范围。
- 不得把 Candidate Claim 直接作为 Verified Fact。

### S9-EXT-007：越权路径

候选指向允许前缀外、敏感路径或其他仓库：

- 在 Reader 调用前拒绝。
- 安全计数增加。
- 公开结果不泄露目标内容。

### S9-EXT-008：重复与预算

- 重复候选去重。
- 超候选数、调用数、总字节或超时后停止。
- 已验证 Evidence 保留，Coverage 标记 `PARTIAL`。

### S9-EXT-009：Prompt Injection

Repository 内容要求读取 Token、执行 Shell、写文件、访问网络或忽略 Commit：

- Adapter 实际动作仍限于 List/Read/Search。
- 写调用、Shell、额外网络和凭证输出为 0。

### S9-EXT-010：外部 Agent 故障

Timeout、429、5xx、Invalid Schema 和 Empty：

- Eval Profile 不静默回退。
- 产品配置允许时显式回退 Native。
- 回退使用原 Commit 和剩余预算，事件记录原因。

### S9-EXT-011：私有推理

外部 Provider 返回 Chain-of-Thought/私有 Trace 字段时，不保存、不展示；只保留公开动作摘要和 Usage。

## 15. Profile Router 与 Ablation 公平性

### S9-PROFILE-001：默认 Native

未配置或非法 Profile 时使用 Native；不能因外部连接存在自动切换。

### S9-PROFILE-002：单变量

对一个 Eval Case 检查两个 Profile 的：

- Commit
- Information Need
- Required Coverage
- Tool/字节/时间预算
- Validator
- Grounding
- Generator

除 Locator 产生器外完全相同。

### S9-PROFILE-003：Eval 禁止回退

External Profile 失败必须记录失败，不以 Native 结果填充 External 指标。

### S9-PROFILE-004：产品显式回退

配置允许时：

- `fallback_from/to/reason` 出现在公开状态。
- 未验证候选不进入 Native 上下文。
- 已验证 Evidence 不重复读取。

## 16. PostgreSQL Migration 与 Repository Contract

### S9-DB-001：Migration Forward

在 Step 8 最新 Schema 上执行 Migration：

- 所有表、索引、外键、Check 和 Unique Constraint 存在。
- 既有数据不丢失。
- Readiness 识别目标版本。

### S9-DB-002：敏感列

- 无 Token/Authorization/Cookie 明文列。
- External ID 使用受保护字段。
- Audit/Attempt 表无正文列。

### S9-DB-003：唯一约束

真实 PostgreSQL 并发验证：

- task + provider 单 Binding。
- Export 业务幂等键唯一。
- 单 Export Run 单活动 Attempt。
- Repository Binding 不跨 Owner 重用。

### S9-DB-004：乐观锁

旧 `expected_task_version` 的预览确认、创建、覆盖和重试均返回 409；不调用 Provider。

### S9-DB-005：Memory/PostgreSQL 等价

相同 Command/Fixture 在 Memory 和 PostgreSQL Repository 上得到相同 Domain 结果、错误与公开状态。

### S9-DB-006：事务恢复

在 Provider 调用前后注入数据库连接失败和提交失败，恢复后满足 Run、Binding、Event 和 Audit 的最终一致性。

## 17. API Contract

### S9-API-001：OpenAPI

- 新 Endpoint、Request、Response、枚举和错误进入生成 Schema。
- Web 不存在重复手写类型。
- Schema 重新生成后 `git diff --exit-code` 为 0。

### S9-API-002：未知字段拒绝

所有写 Request 使用严格 Schema；`external_id`、Token、URL、正文和未知模式被 422 拒绝。

### S9-API-003：Owner 与版本

Repository Binding、Preview、Export History、Retry 对 Owner 和 `expected_task_version` 强制校验。

### S9-API-004：Idempotency Header

- 缺失 Key 的写命令按合同拒绝。
- 相同 Key/相同输入重放同一结果。
- 相同 Key/不同输入冲突。

### S9-API-005：Read Model

Export Read Model 返回：

- Mode、Document Version、Status、Retryable、Safe Error、Safe URL、时间。

不返回 External ID、Credential Ref、原始 Provider 响应、正文块或确认 Token。

### S9-API-006：SSE 白名单

每个新增事件参数化加入未知内部字段，断言公开 SSE 只保留白名单；断线重连不重复副作用。

### S9-API-007：错误稳定性

Provider 错误不改变 HTTP/API 语义；响应包含稳定 Code、Retryable、Correlation ID，无堆栈。

## 18. Web Component Test

### S9-WEB-001：远程来源展示

- 显示 Provider、仓库显示名、短 Commit、只读状态。
- Evidence 链接使用完整固定 Commit。
- 不显示 Credential、External Repository ID 或未授权路径。

### S9-WEB-002：导出入口

只有 `COMPLETED` Task 显示可用导出入口；其他状态隐藏或禁用且说明原因。

### S9-WEB-003：预览

展示标题、位置、版本、模式、未解决事项和安全摘要；Preview 不直接触发写 API。

### S9-WEB-004：创建确认

创建按钮明确，重复点击被禁用；刷新后恢复执行状态，不发起新 Export Run。

### S9-WEB-005：覆盖二次确认

已有 Binding 时展示目标标题和覆盖警告；取消不调用写 API；确认只发送 Intent/Mode/Version。

### S9-WEB-006：失败与重试

可重试/不可重试错误分别展示正确操作；重试使用原 Export Run，不重新创建 Preview 内容。

### S9-WEB-007：安全链接

只渲染 Allowlist HTTPS URL，并带 `noopener noreferrer`；非法 URL 显示纯文本。

### S9-WEB-008：可访问性

Dialog Focus Trap、键盘确认/取消、状态 Live Region、错误关联、禁用状态和对比度达到现有 Web 门禁。

## 19. Browser E2E

### S9-E2E-001：GitHub 调查主路径

选择已授权 Binding → 固定 Commit → 发起需要代码调查的 PRD → 查看 Tool/Evidence/Grounding → 刷新恢复，Commit 与证据不变。

### S9-E2E-002：GitLab 等价路径

在相同逻辑 Fixture 上重复主路径，用户可见行为等价。

### S9-E2E-003：飞书首次导出

完成 PRD → 打开 Preview → 明确确认 → Fake Feishu 创建 → 页面显示成功和安全链接 → 刷新保持。

### S9-E2E-004：飞书覆盖

修改并重新完成 PRD → Preview 显示 `OVERWRITE_BOUND` → 二次确认 → 原文档 Revision 更新，无第二份文档。

### S9-E2E-005：版本竞争

Tab A 打开 Preview，Tab B 修改任务；Tab A 确认时收到冲突并刷新，不调用 Provider。

### S9-E2E-006：响应丢失恢复

创建成功后 Fake Provider 丢弃响应 → 页面显示可重试 → 刷新/重试 → 最终同一文档成功。

### S9-E2E-007：非法请求

浏览器拦截并篡改请求加入其他 External ID/URL；服务端拒绝，目标文档不变。

### S9-E2E-008：删除任务

导出成功后删除任务；任务不可见，Fake Feishu 文档仍存在，Delete 调用次数为 0。

## 20. 故障注入与韧性

### S9-RES-001：网络故障矩阵

对 Repository、Feishu 和 External Agent 分别注入：

- DNS/连接失败
- Connect Timeout
- Read Timeout
- 连接中断
- 429
- 500/502/503
- Invalid JSON
- Truncated Body
- Oversized Body

断言有界重试、标准状态、无凭证泄露和无无限后台任务。

### S9-RES-002：数据库故障矩阵

在预校验、Run 状态写入、Provider 成功回写、Binding/Event/Audit 提交处注入失败，验证恢复和唯一性。

### S9-RES-003：取消

- Repository/External Agent 调查取消后不开始下一调用。
- 飞书已确认写入进入 Provider 后，只能标记取消请求/结果未知，不能声称已撤销远程副作用。
- 恢复时先核对 Provider 结果。

### S9-RES-004：服务重启

对每个持久化状态重启：

- Snapshot 固定。
- Export Intent 过期规则正确。
- Running/Unknown Export 可恢复。
- 成功结果不重复。

## 21. 性能与容量

性能门禁使用固定本地 Fake，避免公网抖动；真实 Sandbox 只做趋势观测。

### S9-PERF-001：远程读取预算

在固定 `repo-large`：

- 验证 Tree/Blob 调用、总字节和内存峰值不超过配置。
- 不把完整仓库加载到内存。

### S9-PERF-002：Formatter

对产品允许上限的 PRD：

- 格式化和 Hash 在线性可接受范围内完成。
- 峰值内存有界。
- Block 数在 Provider 限制内或稳定失败。

### S9-PERF-003：并发 Export

对不同任务的 20 个并发 Fake Export：

- 数据库连接池无死锁。
- 单任务唯一性正确。
- SSE/普通读取仍可响应。

### S9-PERF-004：External Profile

Eval 报告记录 P50/P95 时延、Tool Calls、Bytes、Tokens 和估算费用；不得只报告最终质量。

具体数值阈值在首轮基线跑完后冻结到 Eval Manifest，变更必须更新设计决策记录，不能在代码中随意放宽。

## 22. Native/External Eval

### 22.1 Case 分层

至少包含：

- 单文件明确事实。
- 跨 API/Validation/Storage 的多文件 Coverage。
- 测试与实现冲突。
- 大仓库路径定位。
- Empty/Unknown。
- 敏感内容。
- Prompt Injection。
- Provider 限流/部分结果。

### 22.2 运行矩阵

| Profile | Locator | Validator/Grounding/Generator | 回退 |
| --- | --- | --- | --- |
| Native | Existing Tool Loop | 固定 | 禁止 |
| External | External Coding Agent | 与 Native 相同 | 禁止 |

每个 Case 至少使用固定 Seed 重复运行，输出原始 Run Artifact 和聚合报告。

### 22.3 指标断言

硬门禁：

- Unauthorized Resource Leakage = 0。
- Credential Leakage = 0。
- Unvalidated Candidate Accepted = 0。
- Commit Drift = 0。
- Unsupported Critical Claim 不得高于 Native 已冻结上限。

比较指标：

- Evidence Precision/Recall。
- Coverage Completion。
- Grounded Fact Precision。
- Unknown Preservation。
- PRD Requirement Coverage。
- P50/P95 Latency。
- Tool/Provider Calls、Bytes、Tokens、Cost。

### 22.4 结论模板

报告必须明确：

- 外部 Profile 提升了哪些 Case。
- 退化了哪些 Case。
- 成本和时延代价。
- 安全失败数。
- 推荐默认关闭、按 Case 启用或放弃实现。

不得以单个成功 Demo 宣称外部 Coding Agent 优于 Native。

## 23. Provider Sandbox

### 23.1 运行条件

- 使用专用测试组织、仓库、飞书 Folder 和测试账号。
- 权限最小化，资源可回收。
- 凭证仅由 CI Secret 注入。
- 测试命名带 Run ID，清理只作用于明确测试资源。

### 23.2 测试范围

- GitHub/GitLab：解析 Commit、Tree 分页、Blob、Unicode Path、404/权限和限流 Header。
- 飞书：创建、结构化块、覆盖、Revision、权限撤销和安全 URL。
- 不在 Sandbox 执行任意第三方仓库或用户文档写入。

### 23.3 发布策略

- PR CI：Fake + Contract，全阻断。
- Nightly：真实 Sandbox，可因 Provider 故障标记基础设施失败，但必须保留报告。
- 发布前：目标 Provider Sandbox 必须在有效凭证下通过；若某 Provider 未通过，则对应 Feature Flag 保持关闭。

## 24. 需求追踪矩阵

| 不变量 | 主要测试 |
| --- | --- |
| 授权先于解析 | S9-COMMON-002、006，S9-REPO-009 |
| 凭证不透明 | S9-COMMON-001、005，S9-DB-002 |
| 远程只读、固定 Commit | S9-REPO-001～008，S9-READER-002、006 |
| Evidence 必须可核查 | S9-READER-004、005，S9-EXT-003～006 |
| Completed + 当前版本才能导出 | S9-POLICY-001、003、004 |
| 创建/覆盖幂等 | S9-EXPORT-002～006，S9-OVERWRITE-005 |
| 只能覆盖绑定文档 | S9-POLICY-005、006，S9-OVERWRITE-001 |
| 导出失败不改完成态 | S9-EXPORT-007 |
| 删除任务不外删 | S9-POLICY-007，S9-E2E-008 |
| 外部候选必须重读验证 | S9-EXT-003～009 |
| Ablation 决定保留 | S9-PROFILE-001～004，第 22 节 |

## 25. 建议执行命令

命令应在实现时写入项目脚本，避免 CI 与本地参数漂移：

```bash
pytest tests/integrations tests/repository/remote tests/export tests/external_investigator
pytest tests/integration/test_step9_postgres.py tests/integration/test_step9_recovery.py
pytest tests/api/test_repository_bindings.py tests/api/test_feishu_exports.py

cd web
npm run test
npm run typecheck
npm run build
npm run test:e2e -- --grep @step9

cd ..
python -m prd_agent.eval --profile native --manifest eval/cases/external-investigator/manifest.json
python -m prd_agent.eval --profile external-coding-agent --manifest eval/cases/external-investigator/manifest.json
```

实现后还必须运行仓库现有全量 Python、PostgreSQL、Web、Eval 和 Migration 门禁，而不是只运行 Step 9 目录。

## 26. 发布门禁

### P0 合并门禁

- [ ] Unit、Provider Contract、PostgreSQL Integration、API 和 Web Component 全通过。
- [ ] GitHub/GitLab Provider Contract 语义等价。
- [ ] Credential、未授权资源、External ID 和敏感正文泄露为 0。
- [ ] 分支移动后 Commit Drift 为 0。
- [ ] 飞书创建/覆盖只能基于有效 Preview 和 Confirmation。
- [ ] 重复、并发、超时结果未知和崩溃恢复不产生重复文档。
- [ ] 任意 External ID/URL 注入被拒绝。
- [ ] 导出失败不改变 PRD 完成态；任务删除不外删。
- [ ] 外部候选未经复核进入 Evidence/Fact 数为 0。
- [ ] OpenAPI 漂移、SSE 白名单、P0 Browser E2E 通过。
- [ ] Step 1～8 全量回归无 P0 失败。

### 发布前门禁

- [ ] Native/External Ablation 使用固定 Case、Commit 和预算完成。
- [ ] 报告包含失败 Case、质量、成本、时延和安全指标。
- [ ] 已作出并记录外部 Coding Agent 的启用/关闭结论。
- [ ] 目标 GitHub/GitLab/飞书 Sandbox 通过；未通过 Provider 的 Feature Flag 关闭。
- [ ] Migration Forward、Readiness 和回滚/恢复演练完成。
- [ ] P0 测试零 Skip/XFail；仅凭证缺失的 Sandbox 测试可按规则跳过。

## 27. 完成定义

Step 9 测试完成必须同时满足：

1. 每条核心不变量至少有一个失败测试和一个成功测试。
2. Fake Provider 能复现所有关键故障，不依赖公网运气。
3. 真实 PostgreSQL 覆盖唯一约束、乐观锁、崩溃恢复和并发。
4. 浏览器验证用户确认、覆盖警告、刷新恢复和安全链接。
5. Eval 公平比较 Native/External，并保留可复查 Artifact。
6. 安全硬门禁为 0 违规。
7. 全量历史步骤无回归。
8. 文档、OpenAPI、Migration、代码与测试使用同一状态和错误词汇。

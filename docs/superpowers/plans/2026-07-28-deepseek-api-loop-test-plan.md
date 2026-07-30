# PRD Agent DeepSeek LLM API Loop 自测方案

> 状态：L0/L1/L2/L4 与同步 API/PostgreSQL 链路已执行通过；异步 Worker 专项与 2核2GB 长稳待执行
> 日期：2026-07-28
> 对应设计：`2026-07-28-deepseek-api-loop-design.md`
> 原则：默认测试不需要 API Key、不访问公网、不产生模型费用

## 0.1 本次执行结果（2026-07-28）

| 验证项 | 结果 |
| --- | --- |
| Python 全量（含 PostgreSQL） | `265 passed, 4 skipped` |
| DeepSeek Live Smoke | `4 passed` |
| 真实 FastAPI→Workflow→DeepSeek→Investigation→Tool→Grounding→PostgreSQL | `1 passed` |
| Web 单元测试 | `4 files / 6 tests passed` |
| Web TypeScript | `tsc --noEmit` 通过 |
| Python bytecode 编译 | 通过 |
| Python 依赖完整性 | `No broken requirements found` |
| Docker Compose 展开校验 | 通过 |
| Git diff whitespace | 通过 |
| Secret 路径忽略与权限 | 已忽略，`0600` |
| 仓库疑似 `sk-` Key 扫描（排除 Secret/文档/依赖） | 无命中 |

未执行/未完成：

- L3 中依赖独立 Agent Worker 和持久化 Model Attempt 的恢复、并发 claim、fencing 测试。
- L5 的 2核2GB 容器资源限制、并发压测和 24 小时长稳。
- Starlette TestClient 仍有一条上游弃用警告，不影响当前功能，但升级依赖前需处理。

## 0. 测试分层

| 层级 | 是否需要Key | 是否访问DeepSeek | 用途 | 默认CI |
| --- | --- | --- | --- | --- |
| L0 静态与配置 | 否 | 否 | Secret、依赖、类型和生产装配边界 | 是 |
| L1 Client Mock合同 | 否 | 否 | HTTP请求、响应、错误和重试 | 是 |
| L2 Loop集成 | 否 | 否 | 两轮模型—工具—模型、预算和恢复 | 是 |
| L3 PostgreSQL/Worker | 否 | 否 | Attempt、Outbox、fencing和崩溃恢复 | 是 |
| L4 Live Smoke | 是 | 是 | Key、模型权限和真实JSON兼容性 | 否，手工启用 |
| L5 2核2GB Staging | 是 | 是 | 资源、排队、长稳和发布门禁 | 发布前 |

## 1. 测试安全规则

- `pytest` 默认不得读取真实 Key，不得访问 `api.deepseek.com`。
- 使用 `httpx.MockTransport` 或本地 Fake Server；测试中出现公网连接即失败。
- Live 测试必须同时满足：
  - `PRD_AGENT_LIVE_LLM_TEST=1`；
  - `PRD_AGENT_LLM_API_KEY_FILE` 指向存在的文件；
  - 测试带 `@pytest.mark.live_llm`。
- Live 测试只提交固定无敏感内容，不提交真实仓库代码、PRD或用户数据。
- 任何断言失败、HTTP异常和日志捕获都不得打印 Authorization Header。
- CI不设置 Live 开关，也不注入真实 Key。

## 2. L0：配置和静态测试

目标文件：`tests/production/test_model_api_config.py`

| ID | 场景 | 预期 |
| --- | --- | --- |
| CFG-01 | production 缺少 provider | 启动失败 |
| CFG-02 | production 缺少 model | 启动失败 |
| CFG-03 | production 缺少 Key 文件 | 启动失败 |
| CFG-04 | Key 文件为空 | 启动失败 |
| CFG-05 | `BASE_URL` 使用 HTTP | 启动失败 |
| CFG-06 | host 不在 allowlist | 启动失败 |
| CFG-07 | URL 含 username/password/query/fragment | 启动失败 |
| CFG-08 | `deepseek-v4-pro` 配置有效 | 构造成功 |
| CFG-09 | timeout、token、iteration 超出上下限 | 启动失败 |
| CFG-10 | test 环境试图使用真实网络 Client | 构造失败 |
| CFG-11 | 配置对象 `repr` | 不包含 Key |
| CFG-12 | local 未配置 LLM | 保持规则模型 |
| CFG-13 | staging/production 未配置 LLM | 不得回退规则模型 |
| CFG-14 | 只有 Agent Worker | 能读取 Key |
| CFG-15 | Web/API/Integration/Maintenance | 不挂载 Key |

静态检查：

```bash
rg -n '(^|[^A-Za-z0-9_-])sk-[A-Za-z0-9]{20,}([^A-Za-z0-9_-]|$)' . \
  -g '!infra/production/secrets/**' \
  -g '!docs/**'

git check-ignore infra/production/secrets/deepseek_api_key
```

通过标准：

- 搜索无疑似 Key。
- Secret 路径被 Git 忽略。
- `git diff` 中没有 Key 或 Bearer 值。

## 3. L1：DeepSeek Client Mock合同

目标文件：

- `tests/model_api/test_deepseek_client.py`
- `tests/model_api/test_deepseek_contract.py`

### 3.1 正常响应

| ID | 场景 | 预期 |
| --- | --- | --- |
| HTTP-01 | 200 + JSON content | 返回 ModelResponse |
| HTTP-02 | usage字段完整 | prompt/completion/total正确映射 |
| HTTP-03 | provider返回实际model | 写入response_model_id |
| HTTP-04 | provider request id存在 | 记录但不暴露给普通用户 |
| HTTP-05 |响应前有空白行 | 能正确解析最终JSON body |
| HTTP-06 | Unicode中文JSON | 不损坏编码 |
| HTTP-07 | `finish_reason=stop` | 成功 |

请求合同断言：

- URL 为 allowlist 内 HTTPS。
- `Authorization` 只在 Header。
- model 为配置值。
- `response_format.type=json_object`。
- `stream=false`。
- `thinking.type=disabled`。
- Prompt 中明确包含“JSON”和输出示例。
- `user_id` 符合 `[a-zA-Z0-9\-_]+`，且不包含邮箱、owner ID 原文。
- max_tokens 不超过配置上限。

### 3.2 输出异常

| ID | 场景 | 预期 |
| --- | --- | --- |
| OUT-01 | content为空 | ModelOutputError |
| OUT-02 | content为null | ModelOutputError |
| OUT-03 | content不是JSON | 同输入修复一次 |
| OUT-04 | JSON顶层不是object | 同输入修复一次 |
| OUT-05 | 缺必填字段 | Pydantic失败后修复一次 |
| OUT-06 | 多余字段 | `extra=forbid`拒绝 |
| OUT-07 | `finish_reason=length` | 不当作成功，进入修复/失败 |
| OUT-08 | content filter | 稳定不可重试错误 |
| OUT-09 | repair仍非法 | 结束，不进行第三次调用 |
| OUT-10 | JSON前后Markdown fence | 拒绝，不做宽松截取 |

### 3.3 HTTP错误和重试

| ID | 输入 | 预期调用次数 | 预期分类 |
| --- | --- | ---: | --- |
| ERR-01 | 400 | 1 | permanent_request |
| ERR-02 | 401 | 1 | authentication |
| ERR-03 | 402 | 1 | insufficient_balance |
| ERR-04 | 403 | 1 | permission |
| ERR-05 | 404 | 1 | model_or_endpoint |
| ERR-06 | 422 | 1 | permanent_request |
| ERR-07 | 429→200 | 2 | success_after_retry |
| ERR-08 | 429×3 | 3 | retry_exhausted |
| ERR-09 | 500→200 | 2 | success_after_retry |
| ERR-10 | 503×3 | 3 | retry_exhausted |
| ERR-11 | connect timeout→200 | 2 | success_after_retry |
| ERR-12 | read timeout×3 | 3 | result_unknown |
| ERR-13 | 非JSON HTTP body | 1或受状态码控制 | provider_protocol |
| ERR-14 | 超长 Retry-After | 等待被截断到30秒以下 |

测试使用注入的 fake clock/sleeper，不做真实等待。

### 3.4 日志脱敏

构造包含以下内容的异常：

```text
Authorization: Bearer sk-test-secret
api_key=sk-test-secret
/run/secrets/deepseek_api_key
```

断言：

- `caplog.text` 不包含 `sk-test-secret`。
- Exception `str/repr` 不包含 Secret。
- Model Attempt error只保存标准错误码。
- HTTP响应正文不原样进入公开Event。

## 4. L2：两轮 Agent Loop

目标文件：`tests/investigation/test_llm_api_loop.py`

### LOOP-01：成功的两轮调查

Fake Model 第一次返回：

```json
{
  "tool_id": "repo_tree",
  "tool_schema_version": "1",
  "arguments": {"prefix": "", "max_depth": 2},
  "purpose": "定位实现目录",
  "target_coverage": ["repository_structure"]
}
```

Fake Tool 返回目录摘要，但保持第二个 Coverage gap 未完成。

Fake Model 第二次返回：

```json
{
  "tool_id": "repo_search",
  "tool_schema_version": "1",
  "arguments": {"query": "WorkflowService"},
  "purpose": "定位工作流实现",
  "target_coverage": ["implementation_entry"]
}
```

断言：

- 模型调用2次。
- 工具调用2次。
- 第二次模型输入包含第一次 Tool 的公开摘要和 Evidence ID。
- 第二次输入不包含 Key、DSN或完整敏感日志。
- 每轮 Model Attempt、Tool Attempt、Step和Checkpoint顺序正确。
- Coverage完成后不进行第三次模型调用。

### LOOP-02～LOOP-12

| ID | 场景 | 预期 |
| --- | --- | --- |
| LOOP-02 | 未知tool_id | 不执行工具，记录验证失败 |
| LOOP-03 | 参数越过Tool Schema | 不执行工具 |
| LOOP-04 | 模型试图覆盖repository_id/commit | 字段被拒绝或忽略，服务端值不变 |
| LOOP-05 | 重复action signature | 第二次动作BLOCKED |
| LOOP-06 | 连续2轮无进展 | `NO_PROGRESS` |
| LOOP-07 | 达到8轮 | `MAX_ITERATIONS_REACHED` |
| LOOP-08 | Token预算耗尽 | `TOKEN_BUDGET_EXHAUSTED` |
| LOOP-09 | Tool预算耗尽 | `TOOL_BUDGET_EXHAUSTED` |
| LOOP-10 | 用户取消 | 不再发起下一次模型调用 |
| LOOP-11 | Worker失去lease | 不再发起模型/工具调用，旧结果不能提交 |
| LOOP-12 | Prompt injection要求shell/curl/secret | Tool Registry拒绝 |

## 5. L3：PostgreSQL、Outbox和恢复

目标文件：

- `tests/storage/test_postgres_model_attempts.py`
- `tests/production/test_llm_worker_recovery.py`

| ID | 场景 | 预期 |
| --- | --- | --- |
| DB-01 | 同一Attempt并发claim 10次 | 只有一个owner |
| DB-02 | 相同input hash已成功 | 复用，不调用Provider |
| DB-03 | 不同input hash | 新Attempt |
| DB-04 | 旧fencing token提交 | 更新0行并拒绝 |
| DB-05 | 模型成功后Worker kill | 恢复不重复调用模型 |
| DB-06 | 工具成功后Worker kill | 恢复不重复工具副作用 |
| DB-07 | API与Worker同时更新Investigation | 乐观锁保护，无覆盖 |
| DB-08 | Redis消息重复投递 | 同一逻辑轮次只执行一次 |
| DB-09 | Redis清空 | PostgreSQL Outbox可重新派发 |
| DB-10 | 调用模型期间检查pg_stat_activity | 无idle in transaction |
| DB-11 | Model Attempt只存hash和元数据 | 无完整Prompt、响应和Key |
| DB-12 | owner/tenant交叉查询 | 返回NotFound/Forbidden |

故障点：

```text
claim后、HTTP前
HTTP发送后、响应前
响应后、Attempt提交前
Attempt提交后、Tool执行前
Tool返回后、Evidence提交前
Checkpoint提交后、消息ack前
```

每个故障点至少执行一次 kill/restart 恢复测试。

## 6. L4：真实 DeepSeek Live Smoke

该层只有在部署人员手工写入 Key 后运行。

### 6.1 前置条件

```text
PRD_AGENT_ENVIRONMENT=local
PRD_AGENT_LLM_PROVIDER=deepseek
PRD_AGENT_LLM_BASE_URL=https://api.deepseek.com
PRD_AGENT_LLM_MODEL=deepseek-v4-pro
PRD_AGENT_LLM_API_KEY_FILE=<absolute secret file path>
PRD_AGENT_LIVE_LLM_TEST=1
```

测试代码读取 Key，但不得打印文件内容。若缺少开关或文件，Live 测试必须 `skip`，不能失败，也不能回退到其他Key来源。

### 6.2 LIVE-01：模型可用性

调用：

```text
GET /models
```

断言：

- HTTP 200。
- 列表包含配置的 `deepseek-v4-pro`。
- 401明确报告“认证失败”，但不打印Key。
- 402明确报告“余额不足”。

### 6.3 LIVE-02：最小JSON Output

固定输入：

```text
System: 只返回JSON对象。
User: 输出 {"status":"ok","value":2}，其中value为1+1。
```

限制：

```text
max_tokens <= 64
stream=false
thinking=disabled
timeout <= 30s
```

断言：

- HTTP 200。
- content可解析为JSON object。
- `status == "ok"`。
- `value == 2`。
- usage.total_tokens存在且大于0。
- response model和request id可记录。

### 6.4 LIVE-03：真实ProposedAction

向真实模型提供两个只读 Fake Tool Schema 和一个固定Coverage gap，不提供任何仓库内容。

断言：

- 输出通过 `ProposedAction`。
- tool_id来自白名单。
- arguments通过对应Schema。
- target_coverage只指向活动gap。

该测试最多允许一次repair，总模型调用不超过2次。

### 6.5 Live执行命令

实现后使用：

```bash
pytest -q -m live_llm tests/live/test_deepseek_smoke.py
```

测试结束只输出：

```text
model_id
finish_reason
token counts
latency_ms
PASS/FAIL
```

不得输出 Prompt、响应正文、Authorization或Key。

## 7. L5：2核2GB Staging自测

### 7.1 资源限制

部署：

```text
FastAPI x1
Agent Worker x1, concurrency=1
Integration Worker x1, concurrency=1
Maintenance x1
PostgreSQL x1
Redis x1
```

测试：

- 连续提交10个生成任务。
- 确认只有1个Agent Run执行，其余排队。
- 记录每个进程RSS、系统可用内存、Swap、CPU和数据库连接。
- 模型慢响应90秒时API普通查询仍可用。
- Agent Worker重启后队列继续，已提交轮次不重复。

通过标准：

- 无OOM。
- 无持续Swap。
- API查询p95小于800ms。
- 命令接收p95小于1s。
- Agent Worker RSS不超过设计预算420MB。
- 总数据库连接不超过24。
- Queue depth、最老等待时间和Provider错误可观测。

### 7.2 24小时长稳

负载：

- 每15分钟一个小型固定Mock任务。
- 每小时一次真实最小Live请求，或在成本控制要求下全部使用Mock。
- 周期注入429、503、timeout和Worker restart。

通过标准：

- 无内存持续增长。
- 无卡在RUNNING超过恢复阈值的Run。
- 无重复工具副作用。
- 无超过预算的模型调用。
- 日志扫描无Key和Bearer Token。

## 8. 执行顺序与门禁

开发阶段：

```bash
pytest -q tests/production/test_model_api_config.py
pytest -q tests/model_api
pytest -q tests/investigation/test_llm_api_loop.py
pytest -q tests/storage/test_postgres_model_attempts.py
pytest -q
```

Key写入后：

```bash
pytest -q -m live_llm tests/live/test_deepseek_smoke.py
```

发布前：

```text
L0-L3全部通过
→ Live Smoke通过
→ 2核2GB故障测试通过
→ 24小时长稳通过
→ 才允许启用真实用户流量
```

## 9. 最终验收清单

- [ ] 默认CI不需要Key且不访问公网。
- [ ] Secret路径被Git忽略，diff和日志无Key。
- [ ] production缺少Key或模型配置时fail closed。
- [ ] `deepseek-v4-pro`出现在账号 `/models`。
- [ ] JSON Output真实调用通过。
- [ ] 第二轮模型能读取第一轮工具结果。
- [ ] 非法输出不会执行工具。
- [ ] 重试、repair、迭代、Token和Tool调用全部有界。
- [ ] Model Attempt支持并发claim、恢复和fencing。
- [ ] Provider调用期间无数据库长事务。
- [ ] 多用户上下文和Provider user_id隔离。
- [ ] 2核2GB无OOM、持续Swap和API阻塞。
- [ ] 24小时长稳无重复动作和卡死Run。

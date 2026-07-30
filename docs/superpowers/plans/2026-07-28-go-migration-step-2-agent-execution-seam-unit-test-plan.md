# Go 迁移 Step 2：Agent Execution Seam 单测方案

> 状态：IMPLEMENTED（核心单测与 gRPC contract test 已完成）
> 日期：2026-07-28
> 对应设计：`2026-07-28-go-migration-step-2-agent-execution-seam-design.md`
> 方法：TDD，按垂直切片执行 RED → GREEN → REFACTOR

## 1. 测试目标

测试重点不是 gRPC 生成代码的字段数量，而是跨语言边界上的可观察行为：

1. 只有合法 Lease 才能修改 Agent Run。
2. stale Worker 不能覆盖新 Worker 的结果。
3. Checkpoint、Model Attempt、Evidence、Draft Patch 可恢复且幂等。
4. Python Agent 不需要数据库连接即可运行。
5. Task 版本冲突和协议错误能被稳定识别。
6. 失败、超时、Worker 重启不会造成重复终态或静默丢失。

测试通过公开接口验证行为，不测试私有字段、SQL 拼接细节或具体实现类名。

## 2. 测试分层

| 层级 | 目标 | 允许的依赖 | 不覆盖 |
| --- | --- | --- | --- |
| Go Domain Unit | Lease、fencing、状态和幂等行为 | Memory Store、Fake Clock | PostgreSQL 方言 |
| Go Application Unit | Agent Execution Service 行为 | Store interface、Fake Store | gRPC 编解码 |
| Go gRPC Contract Test | status code、metadata、payload mapping | generated bindings、Fake Service | 真实数据库 |
| Python Runtime Unit | Worker loop、heartbeat、checkpoint 恢复 | Fake Agent RPC、Fake LLM | Go SQL |
| Cross-language Contract | Go/Python 双向消息兼容 | 生成 bindings、fixtures | Provider API |
| PostgreSQL Integration | 事务、锁、唯一约束、并发 | Test PostgreSQL | 模型推理 |

本文件以单测和 contract test 为主；PostgreSQL Integration 是 Step 2 的验收门禁，但不应替代快速单测。

## 3. 测试夹具

### 3.1 Go 夹具

建议目录：

```text
backend-go/internal/agentexec/
  service_test.go
  grpc_server_test.go
  fixtures_test.go
backend-go/internal/runcontrol/
  lease_test.go
  checkpoint_test.go
  idempotency_test.go
```

公共夹具：

- `MemoryStore`：验证行为，不依赖数据库；
- `FakeClock`：精确控制 Lease 过期时间；
- `FakeWorkerIdentity`：模拟合法/非法 Worker；
- `RecordingStore`：只记录公共接口调用，用于 adapter 行为断言；
- `testLease(runID, workerID, token)`：生成合法 Lease；
- 固定 `tenant_id`、`owner_id`、`task_id`、`run_id`，避免随机 ID 让断言不稳定。

### 3.2 Python 夹具

```text
agent-python/tests/
  test_runtime_acquire.py
  test_runtime_checkpoint.py
  test_runtime_failure.py
  test_grpc_contract.py
```

公共夹具：

- `FakeAgentExecutionClient`：记录 RPC 顺序和请求；
- `FakeLLM`：可返回成功、结构化错误、timeout；
- `FakeStream`：模拟重复、丢失和延迟 wake-up；
- `FakeClock`：控制 heartbeat 和 timeout；
- `RunContextFixture`：包含 task message、workflow version、checkpoint。

### 3.3 Contract fixtures

建议新增：

```text
contracts/fixtures/agent_execution/
  acquire_run_request.json
  lease_context.json
  run_context_empty_checkpoint.json
  run_context_with_checkpoint.json
  model_attempt.json
  evidence_batch.json
  draft_patch.json
  task_version_conflict.json
  lease_lost.json
```

Fixture 只包含脱敏数据，不允许出现真实 API Key、JWT、OAuth Token、真实用户消息或 Provider 正文。

## 4. TDD 垂直切片测试清单

### Slice 2.1：Acquire / Heartbeat / Complete

#### Test 2.1.1：合法 Worker 可以获取运行租约

- Given：Run 为 `QUEUED`，无活跃 Lease。
- When：Worker A 调用 `AcquireRun`。
- Then：返回 `RUNNING`、唯一 `lease_id`、`fencing_token=1` 和过期时间。
- RED：先写行为测试，确认当前实现缺少 RPC adapter。
- GREEN：实现最小 application service 和 adapter。

#### Test 2.1.2：活跃 Lease 阻止第二个 Worker 获取

- Given：Worker A 持有未过期 Lease。
- When：Worker B 调用 `AcquireRun`。
- Then：返回 `LEASE_HELD`，Run 和 fencing token 不变化。

#### Test 2.1.3：过期 Lease 可被接管

- Given：Worker A Lease 已过期。
- When：Worker B 获取同一个 Run。
- Then：Worker B 获得新 `lease_id`，fencing token 严格递增。

#### Test 2.1.4：Heartbeat 只允许当前 Lease

- Given：Worker A 持有 Lease。
- When：使用错误 lease_id、worker_id 或 fencing token 发送 Heartbeat。
- Then：返回 `LEASE_LOST`，过期时间不变。

#### Test 2.1.5：合法 Lease 可以完成 Run

- Given：Run 为 `RUNNING`，Lease 合法且未过期。
- When：调用 `CompleteRun`。
- Then：Run 变为 `SUCCEEDED`，Lease 清除，Queue Slot 释放。

#### Test 2.1.6：旧 Worker 不能完成已被接管的 Run

- Given：Worker A 过期，Worker B 已接管。
- When：Worker A 使用旧 token 完成 Run。
- Then：返回 `LEASE_LOST`，Run 保持 Worker B 的状态。

### Slice 2.2：Context / Checkpoint

#### Test 2.2.1：首次运行返回空 checkpoint

- Given：Run 没有 checkpoint。
- When：合法 Lease 调用 `GetRunContext`。
- Then：返回 task message、workflow version 和空 checkpoint。

#### Test 2.2.2：Checkpoint 可按 sequence 恢复

- Given：已保存 sequence=3 checkpoint。
- When：Worker 重启后读取 context。
- Then：只返回最新合法 checkpoint。

#### Test 2.2.3：旧 sequence 不得覆盖新 checkpoint

- Given：当前 sequence=3。
- When：提交 sequence=2。
- Then：返回 `CHECKPOINT_SEQUENCE_CONFLICT`，blob 不变化。

#### Test 2.2.4：同一 sequence 的相同内容可重放

- Given：sequence=3 和相同 content hash 已存在。
- When：重复提交。
- Then：返回原 receipt，不产生第二条事实记录。

#### Test 2.2.5：Checkpoint payload 超限被拒绝

- Given：超过配置大小的 blob。
- When：调用 `SaveCheckpoint`。
- Then：返回 `PAYLOAD_TOO_LARGE`，不写数据库。

### Slice 2.3：Model Attempt / Evidence

#### Test 2.3.1：Model Attempt 使用 attempt_key 幂等

- Given：第一次 attempt 已记录。
- When：同一 `run_id + attempt_key` 重复提交相同 hash。
- Then：返回原 `attempt_id`，记录数量不增加。

#### Test 2.3.2：Attempt key 复用不同请求返回冲突

- Given：相同 attempt_key 已绑定 request_hash A。
- When：提交 request_hash B。
- Then：返回 `IDEMPOTENCY_CONFLICT`。

#### Test 2.3.3：敏感字段不会被保存

- Given：metadata 中错误地包含 Authorization、API key 或 JWT。
- When：调用 `RecordModelAttempt`。
- Then：请求被拒绝或敏感字段被明确剔除；持久化内容不含 secret。

#### Test 2.3.4：Evidence 重复提交不会重复写入

- Given：相同 source、locator 和 excerpt hash 已存在。
- When：重复 AppendEvidence。
- Then：返回已接受数量为 0 或原 receipt，不产生重复 Evidence。

#### Test 2.3.5：Evidence 缺少定位信息被拒绝

- Given：source_id 或 locator 为空。
- When：AppendEvidence。
- Then：返回明确 validation error，不改变 Run。

### Slice 2.4：Draft / Task Version

#### Test 2.4.1：正确 Task version 可以提交 Draft Patch

- Given：Task version=4，Lease 合法。
- When：提交 expected version=4。
- Then：产生新的 Working Draft version，Task version 递增。

#### Test 2.4.2：旧 Task version 返回冲突

- Given：Task version 已为 5。
- When：提交 expected version=4。
- Then：返回 `TASK_VERSION_CONFLICT`，Draft 和 Task 均不变化。

#### Test 2.4.3：正确版本但 stale Lease 仍然失败

- Given：expected version 正确，但 fencing token 已过期。
- When：SubmitDraft。
- Then：优先返回 `LEASE_LOST`，不写 Draft。

### Slice 2.5：Python Worker Loop

#### Test 2.5.1：Worker 按正确顺序执行最小 Run

- Given：wake-up、合法 Acquire、LLM 返回结构化结果。
- When：Worker 运行一次。
- Then：RPC 顺序为 `Acquire → Context → Attempt → Checkpoint → Draft → Complete`。

#### Test 2.5.2：Heartbeat 失败会停止后续写入

- Given：模型执行期间 Heartbeat 返回 `LEASE_LOST`。
- When：Agent 生成结果。
- Then：Worker 不再调用 Draft/Complete，并记录可恢复失败。

#### Test 2.5.3：模型 timeout 进入可重试失败

- Given：LLM timeout。
- When：Worker 处理异常。
- Then：调用 `FailRun(retryable=true)`，不伪造 Draft。

#### Test 2.5.4：结构化输出错误不绕过 Go 边界

- Given：LLM 返回无法解析的结构化结果。
- When：Worker 尝试修复仍失败。
- Then：只上报失败 metadata，不直接写数据库或修改 Run 状态。

#### Test 2.5.5：重复 wake-up 不重复执行同一个 Run

- Given：Redis Stream 发送相同 run_id 两次。
- When：Worker 消费两条消息。
- Then：第二次因 Run 已终态或 Lease 已被持有而安全退出，不产生第二个成功结果。

## 5. gRPC Contract Test

每个消息至少覆盖：

- Go encode → Python decode；
- Python encode → Go decode；
- unknown field 保留/忽略符合 proto3 兼容规则；
- 缺少 mandatory contract version 被拒绝；
- unsupported contract version 进入 `CONTRACT_VERSION_UNSUPPORTED`；
- 时间戳、fencing token、sequence 不发生精度或符号转换；
- error code 可双向映射到客户端异常类型。

Contract Test 不应断言生成代码的字段排列或具体类名，只断言语义字段和兼容行为。

## 6. PostgreSQL Integration Gate

单测通过后再运行以下数据库行为测试：

1. 两个 Worker 并发 Acquire 不会同时得到有效 Lease。
2. 两个事务并发 SubmitDraft 只有一个成功。
3. `attempt_key` 唯一约束保证跨进程幂等。
4. checkpoint sequence 在并发提交下单调递增。
5. stale fencing update 的 affected rows 为 0。
6. 事务回滚不会留下半条 Evidence 或 Draft。
7. Outbox 发布成功后只标记一次 published。

这些测试使用临时 PostgreSQL，不使用 SQLite 替代，因为锁、`FOR UPDATE`、唯一约束和事务隔离是本 Step 的核心行为。

## 7. 测试执行顺序

每个 Slice 严格执行：

```text
RED       写一个行为测试，确认它因能力缺失而失败
GREEN     写最小实现，让该测试通过
REFACTOR  清理接口和重复逻辑，重新运行当前 Slice 测试
CONTRACT  增加 Go/Python 边界 fixture
REGRESSION 运行前序 Slice 和 race test
```

禁止先批量编写所有测试再一次性实现；每个测试都必须对应刚刚验证过的一个真实行为。

## 8. 测试命令

### Go

```bash
cd backend-go
go test ./...
go test -race ./internal/...
go vet ./...
```

### Contract

```bash
cd contracts
buf lint
buf generate
```

### Python Agent

```bash
pytest agent-python/tests -q
pytest agent-python/tests/test_grpc_contract.py -q
```

### Integration

```bash
docker compose -f infra/test/docker-compose.yml up -d postgres redis
go test ./... -tags=integration
pytest -m integration -q
```

## 9. 通过标准

- Slice 2.1–2.5 的行为测试全部通过；
- Go `-race` 无数据竞争；
- 所有 stale lease/fencing、版本冲突、幂等和 payload 限制均有回归测试；
- Go/Python contract fixture 双向解析通过；
- PostgreSQL Integration Gate 通过；
- 测试夹具不包含任何隐私或生产凭证；
- 不以覆盖率数字代替行为验收，但新增核心模块建议保持 85% 以上有效分支覆盖。

# Agent Loop 最终实现与本地部署验证报告

> 日期：2026-08-02
> 结论：R1–R3 本地实现完成，完整测试与 Compose 部署检查通过；Phase 10/11 保持环境硬门禁。

## 1. 已实现范围

### R1：本地契约闭环

- 抽取 Memory/PostgreSQL 共用的 Review Workflow contract，覆盖 v4 assignment、output replay、并发确认单赢、Outline 锁定和依赖 Unit 顺序。
- 增加隔离 schema 的 legacy migration fixture：从 `0001..0015` 写入 v1 历史数据，再升级至 `0021`，验证历史语义、迁移幂等性和新约束。
- PostgreSQL 集成测试全部使用独立 schema，消除共享数据库状态污染。

### R2：Repair 与 Shadow 权威链

- 增加真实 Worker `REVISE_UNIT` E2E，验证 patch 后重新 claim、Grounding、Quality，并检查 generation 推进与 `requires_regrounding=false`。
- Shadow Artifact 不再信任 Worker 自带结果；Memory/PostgreSQL 都从已持久化 `RunOutput` 或 `WorkingDraft` 重建 authoritative trace，再校验 trace、assignment、policy 与 effect ledger。
- 缺少持久化 authoritative output、identity 漂移或 Shadow 产生额外 effect 时 fail-closed。

### R3：持久化 rollout lifecycle

- 新增 migration `0021_rollout_lifecycle_commands.sql`，扩展 `RECORD_GATE`、`TRANSITION_STAGE`、`RECORD_READINESS`，并增加 readiness record 表。
- Gate、Stage、Readiness 使用统一 command identity、request hash、expected version、dry-run/apply 和幂等 replay 语义。
- `rolloutctl` 支持 `record-gate`、`transition`、`readiness render|record|verify`。
- `LEGACY_RETIRED` 必须绑定 completed zero-count drain、removal inventory hash 和持久化 zero-hit reader observation；任一缺失均拒绝。

## 2. 完整测试结果

| 验证线 | 结果 |
| --- | --- |
| Python 主工程（含本地 PostgreSQL integration） | `327 passed, 4 skipped` |
| Python Agent Worker | `159 passed` |
| Go 全量（含 9 个隔离 PostgreSQL integration cases） | `go test ./...` 通过 |
| Go 静态检查 | `go vet ./...` 通过 |
| Web 单测 | `4 files / 6 tests` 通过 |
| Web 类型检查 | `tsc --noEmit` 通过 |
| Web production build | Next.js production build 通过，4 个路由生成成功 |

Python 的 4 个 skip 均为需要显式设置 `PRD_AGENT_LIVE_LLM_TEST=1` 的 DeepSeek live LLM smoke，
不属于本地确定性验证失败。唯一告警是 Starlette `TestClient` 的依赖弃用提醒，不影响本次结果。

## 3. 本地部署检查

使用 `infra/local/docker-compose.go.yml` 构建并启动完整栈。检查结果：

| 组件 | 证据 |
| --- | --- |
| Go API | 容器 healthy；`GET /api/v1/health/ready` 返回 `{"status":"ready"}` |
| Python Agent | 容器 healthy；9100 RPC socket 返回 `worker-rpc-ok` |
| PostgreSQL | 容器 healthy；migration head 为 `0021_rollout_lifecycle_commands.sql`；readiness 表存在 |
| Redis | 容器 healthy；`PING` 返回 `PONG` |
| Web | `/tasks/new` 返回 HTTP 200；production build 通过 |
| Maintenance | 持续运行并完成 Worker dispatch |
| Compose | `docker compose config --quiet` 通过；`go-migrate` 成功退出 |
| 临时构建设施 | 临时 Go module proxy 容器已清理 |

最近十分钟 API、Maintenance、Worker 日志没有错误输出。

## 4. 部署后端到端工作流

本地真实 HTTP + PostgreSQL + Maintenance + gRPC Worker 流程已完成：

```text
create v4 task
  -> OUTLINE_REVIEW
  -> confirm outline
  -> UNIT_REVIEW
  -> confirm unit
  -> FULL_REVIEW
  -> REVIEWABLE / PASSED
```

- Task：`task-63c6fca5ce0d0f1bf24f1e79e1c84fab`
- 最终 Task 状态：`REVIEWABLE`，version `6`
- Full Review：`PASSED`
- 审查输入绑定：Outline version、Unit hash set、source Run 和 content hash 均已持久化。
- Feishu publish 未执行：本地没有受控 staging target，发布仍受显式 preview/confirm 与环境 Gate 约束。

## 5. Rollout 安全验证

部署中的 rollout authority 为：

```json
{"stage":"LOCAL_ONLY","policy_version":"local-v4.1","paused":false,"version":1}
```

使用已构建镜像执行 readiness dry-run，结果为 `BLOCKED` 且 `applied=false`，阻塞码为：

- `GATE_DECISION_NOT_BOUND`
- `MISSING_GATE_DECISION`
- `STAGE_NOT_LOCALLY_VERIFIED`

这证明本地测试通过不会伪造 Gate、不会自动晋级 staging/production，也不会触发 v1 删除。后续必须按
`infra/production/agent-runtime-rollout-runbook.md` 收集真实环境证据并逐阶段执行 Phase 10；只有完成
zero-drain、producer removal、完整 reader observation window 和最终 Gate 后，Phase 11 才允许进入
`LEGACY_RETIRED`。

## 6. 复核入口

- 最终闭环设计：`docs/superpowers/plans/2026-08-02-agent-loop-final-closure-and-retirement-design.md`
- Rollout runbook：`infra/production/agent-runtime-rollout-runbook.md`
- Migration：`backend-go/db/migrations/0021_rollout_lifecycle_commands.sql`
- Rollout lifecycle：`backend-go/internal/runcontrol/rollout_lifecycle.go`
- PostgreSQL lifecycle：`backend-go/internal/storage/rollout_lifecycle.go`
- Operator CLI：`backend-go/cmd/rolloutctl/main.go`

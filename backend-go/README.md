# Go Control Plane

This module is the target Go backend for PRD Agent. It deliberately does not
use GORM. PostgreSQL access uses `pgxpool` and explicit SQL; `sqlc` is prepared
as the code-generation seam for the next repository slice. Redis is used only
as an outbox wake-up transport; PostgreSQL remains the source of truth.

## Run locally

完整的 PostgreSQL → Go API → Go Dispatcher → Python Agent Worker 验证：

```bash
scripts/validate-go-agent-local.sh
```

该脚本使用 `infra/local/docker-compose.go.yml` 的独立 Compose project、
端口和 PostgreSQL volume，不会修改旧 Python 本地环境的数据。

仅启动 API：

```bash
go run ./cmd/api
```

Without `PRD_AGENT_DATABASE_DSN`, the server uses the in-memory Store so the
HTTP contract can be exercised locally. With a DSN, run the SQL files under
`db/migrations` through the Go migration runner before starting the server:

```bash
go run ./cmd/migrate
```

## Current vertical slice

- Gin health and principal endpoints;
- tenant/owner-scoped Task and Agent Run commands;
- stable Idempotency-Key replay;
- bounded Queue Slot admission;
- PostgreSQL Store using `pgxpool` and versioned SQL migrations;
- lease, heartbeat, fencing-token validation and stale-worker rejection;
- waiting-run promotion and Redis Stream outbox publisher;
- versioned Go-to-Python `AgentWorkerService` protobuf contract and generated bindings;
- bounded direct Go-to-Python Agent Worker RPC pool and durable Dispatcher;
- Go-owned checkpoints, model-attempt metadata, evidence and draft patches.

The Python Agent remains the model/workflow runtime. It is not imported by the
Go binary and it does not receive public HTTP traffic.

## Verification profile

The opt-in Compose profile starts the Go API and maintenance process beside the
legacy Python deployment on port `8001`:

```bash
docker compose -f infra/production/docker-compose.yml \
  -f infra/production/docker-compose.go.yml \
  --profile go-control-plane up --build go-migrate go-api python-agent go-maintenance
```

This is intentionally a shadow/cutover profile until API parity, OIDC, data
projection, and the Python Agent gRPC client gates are complete. Do not run two
writers against the same production-owned tables.

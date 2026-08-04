# PRD Agent

M0 Portfolio Core includes the Direct Prompt evaluation baseline, resumable PRD workflow,
commit-pinned read-only Repository Tools, deterministic Evidence/Facts and a bounded
Investigation Loop. Step 5 adds Fact/Claim Grounding, one targeted supplement, Grounding
metrics and deterministic Evidence Appendix rendering. Step 6 adds multi-level outlines,
dynamic confirmation units, confirmed-only context, immutable section/document versions,
Document Quality gates, user-approved revision and explicit finalization. Step 7 adds an
owner-scoped FastAPI, resumable SSE stream and a responsive Next.js PRD workbench.

The deployment target is a bounded multi-user service on 2 vCPU / 2GB RAM /
40GB SSD. Feishu owns published PRD bodies, GitHub owns repository content,
PostgreSQL owns orchestration and retrieval metadata, and production calls a
remote LLM only. The migration and acceptance contract is documented in
`docs/superpowers/plans/2026-07-28-feishu-github-rag-queue-architecture-design.md`.
The active P0 implementation scope for a small number of stable users and a
single recoverable Agent is
`docs/superpowers/plans/2026-07-29-agent-landing-p0-solution-design.md`.
The active master design is
`docs/superpowers/specs/2026-07-21-prd-agent-v1.1-design.md`; the V1.0 design is
inactive and retained only as history.

## Local setup

```bash
python3 -m venv venv
venv/bin/python -m pip install -e '.[api,postgres,workflow,production,dev]'
npm --prefix web install
docker compose -f infra/local/docker-compose.yml up -d
export PRD_AGENT_DATABASE_DSN='postgresql://prd_agent:prd_agent_local_only@localhost:5432/prd_agent'
```

To enable the real LLM workflow locally, create the ignored file
`infra/production/secrets/deepseek_api_key` with only the DeepSeek API key,
then copy the `PRD_AGENT_LLM_*` settings from `.env.example`. The deterministic
offline model remains available only to local development, tests and evaluation;
staging and production fail readiness without the remote model configuration.

### Context compression

The LangGraph runtime has a Ledger-backed context preparation seam for every
remote model call, including information-need planning and scoped outline/unit
generation. It builds an immutable `CONTEXT_PACK` artifact, applies deterministic
operation-specific projection first, and can optionally use a separately budgeted
model call for semantic compression. The original payload remains authoritative;
semantic output may omit existing values but cannot change mandatory fields or
introduce new identifiers.

Compression is disabled by default. Configure the token limits in `.env`, then
roll out with `PRD_AGENT_CONTEXT_POLICY_MODE=shadow` before switching to
`enforce`. Semantic compression is independently gated by
`PRD_AGENT_CONTEXT_SEMANTIC_COMPACTION=true`. Non-off modes require
`PRD_AGENT_AGENT_LOOP_MODE=langgraph`; startup fails instead of silently using
the legacy model path. Existing PostgreSQL deployments must apply migration
`0022_context_pack_ledger_kind.sql` through the normal migration runner.

The detailed contracts, failure semantics, rollout gates, and compression
rationale are documented in
[`docs/superpowers/plans/2026-08-02-agent-context-compaction-design.md`](docs/superpowers/plans/2026-08-02-agent-context-compaction-design.md).

### Project memory

Project memory is owned by the Go/PostgreSQL control plane. Agents and API
clients create review candidates; only an explicit user confirmation promotes a
candidate into an immutable memory version. Every run captures a fixed memory
space, epoch watermark, access-scope hash and assignment hash. Capability
Gateway recall is therefore owner-scoped, permission-checked and reproducible:
memory confirmed after a run starts is invisible to that run.

The LangGraph model seam records recall as a `CAPABILITY` Ledger entry and the
bounded `PROJECT_MEMORY_BUNDLE` as a `LOCAL_DERIVATION`. In `shadow` mode these
artifacts are evaluated without changing model input. In `enforce` mode the
bundle becomes a named Context Pack source; open conflicts are returned
together and never silently resolved. Configure rollout with
`PRD_AGENT_PROJECT_MEMORY_MODE`, starting with `shadow`. Existing PostgreSQL
deployments must apply `0023_project_memory.sql`.

The owner-scoped API is under `/api/v1/memory-spaces`: it supports space and
candidate creation, candidate confirmation/rejection, record listing/search,
and version-checked revocation. All mutations require `Idempotency-Key`.
Detailed authority, lifecycle, failure and security contracts are in
[`docs/superpowers/plans/2026-08-03-project-memory-tool-design.md`](docs/superpowers/plans/2026-08-03-project-memory-tool-design.md).

For a PostgreSQL volume created before Step 2, apply `infra/local/schema.sql` once; Docker's
initialization directory only runs for a new volume.
For a database created before Step 4, also apply
`infra/local/migrations/20260723_step4_investigation.sql` once.
For a database created before Step 5/6, then apply:

```bash
psql "$PRD_AGENT_DATABASE_DSN" \
  -f infra/local/migrations/20260723_step5_step6_grounding_workflow.sql
```

For a database created before Step 7, apply the owner/query migration:

```bash
psql "$PRD_AGENT_DATABASE_DSN" \
  -f infra/local/migrations/20260726_step7_web_owner_queries.sql
```

For an existing database, apply the Step 8–10 expand migrations in filename
order. The production migration runner does this under a PostgreSQL advisory
lock:

```bash
PRD_AGENT_DATABASE_DSN="$PRD_AGENT_DATABASE_DSN" \
  prd-agent-migrate --root infra/local
```

Step 10 adds the production profile: OIDC principal mapping, per-request
PostgreSQL pool connections, transactional Outbox/Inbox, worker leases with
heartbeat and fencing, Celery JSON queue contracts, persisted cancellation,
recovery reconciliation, OAuth/Secret/Webhook security boundaries, and
production deployment manifests. PostgreSQL remains the source of truth for the
PRD Agent control plane; Feishu is the source of truth for published PRD bodies,
GitHub is the source of truth for code, and Redis is used only for broker,
wake-up, cancellation notification and bounded caches.

## Web workbench

Start the API and Web app in separate terminals:

```bash
venv/bin/uvicorn --env-file .env prd_agent.api:create_app --factory \
  --host 127.0.0.1 --port 8000

npm --prefix web run dev
```

Open `http://127.0.0.1:3000`. The Portfolio profile uses the fixed `local-user`
principal, but every list, snapshot, command and event query still enforces `owner_id`.
The UI supports task creation, clarification, outline/unit confirmation, quality revision,
finalization, reopen, safe Markdown and event-cursor recovery.

## Local Feishu Wiki integration

Copy `.env.example` to `.env` and configure one existing Wiki Docx node. The
application exchanges `PRD_AGENT_FEISHU_APP_ID` and
`PRD_AGENT_FEISHU_APP_SECRET` for a cached tenant access token, resolves
`PRD_AGENT_FEISHU_WIKI_NODE_TOKEN` to the underlying Docx identifier, and
always reads or replaces that same node in place. The user-facing link remains
the configured Wiki URL.

The configured Feishu application must be published, have Docx/Wiki API
permissions, and have edit access to the concrete Wiki space. Generate the two
local application secrets independently:

```bash
openssl rand -hex 32
openssl rand -hex 32
```

For an existing database, apply the integration tables before starting the
API:

```bash
set -a
source .env
set +a
venv/bin/python -m prd_agent.production.migrate --root infra/local
```

## Production profile

The small-user P0 profile does **not** require an identity platform. TLS ingress
uses one Basic Auth credential per allowed user, strips caller-supplied identity
headers, and passes an authenticated username plus a 32-byte proxy secret to
the Go API. The API enforces an explicit user allowlist, exact Origin/Host, and
owner isolation.

The production topology now includes PostgreSQL, Redis wake-up transport, Go
API/migrations/maintenance, one Python Agent Worker, one fixed-Wiki Feishu
Integration Worker, Next.js Web, TLS ingress, and encrypted 15-minute database
snapshots. All model calls and the DeepSeek key stay in the Agent container.

Use the complete deployment and recovery procedure in
[`infra/production/README.md`](infra/production/README.md). Production still
fails closed until the operator supplies the domain/TLS files, generated
service secrets, DeepSeek key, fixed Feishu Wiki target, Basic Auth file and an
external backup directory.

## Minimal workflow

```bash
prd-agent workflow start --message '订单列表增加创建时间筛选' --idempotency-key start-001
prd-agent workflow show --task-id TASK_ID
prd-agent workflow confirm-outline --task-id TASK_ID --outline-version 1 \
  --expected-task-version TASK_VERSION --idempotency-key outline-001
prd-agent workflow confirm-unit --task-id TASK_ID --unit-id UNIT_ID \
  --expected-task-version TASK_VERSION --idempotency-key unit-001
prd-agent workflow finalize --task-id TASK_ID --document-id DOCUMENT_ID \
  --content-hash DOCUMENT_CONTENT_HASH --expected-task-version TASK_VERSION \
  --idempotency-key finalize-001
prd-agent workflow reopen --task-id TASK_ID --unit-id UNIT_ID \
  --reason '业务规则发生变化' --expected-task-version TASK_VERSION \
  --idempotency-key reopen-001
```

The CLI uses a deterministic offline model so the state machine can be demonstrated without
external credentials. A task becomes `COMPLETED` only after every confirmation unit is
confirmed, document checks pass and `finalize` identifies the current document hash.

## Verification

```bash
venv/bin/python -m pytest -q
# Runs every PostgreSQL integration test instead of skipping the database gate:
PRD_AGENT_TEST_DATABASE_DSN="$PRD_AGENT_DATABASE_DSN" \
  venv/bin/python -m pytest -q
npm --prefix web run test
npm --prefix web run typecheck
npm --prefix web run build

PYTHONPATH=src python3 -m prd_agent.eval run-baseline \
  --manifest eval/cases/manifest.json \
  --config eval/configs/minimal_workflow.json

PYTHONPATH=src python3 -m prd_agent.eval run-baseline \
  --manifest eval/cases/manifest.json \
  --config eval/configs/single_retrieval.json

PYTHONPATH=src python3 -m prd_agent.eval run-baseline \
  --manifest eval/cases/manifest.json \
  --config eval/configs/bounded_investigation.json

PYTHONPATH=src:agent-python venv/bin/python -m prd_agent.eval run-baseline \
  --manifest eval/cases/manifest.json \
  --config eval/configs/langgraph_v1_characterization.json \
  --output-dir eval/reports
```

The LangGraph characterization configuration runs the production Agent loop with fixed
offline adapters. It emits versioned, content-safe traces and sanitized reports marked
`deterministic_only`; it does not measure remote-model quality.

## Repository evidence

`RepositoryEvidenceService` accepts only registered, schema-versioned Tool Actions. The
included registry exposes `repo_tree`, `search_text`, `read_file`, `find_symbol`,
`find_references`, `parse_openapi`, `parse_database_schema`, and `find_related_tests`.
Repository IDs map to server-configured
roots and prefixes; all reads come from Git objects at a resolved 40-character commit SHA.

Text/tree/test results remain Evidence only. OpenAPI and PostgreSQL DDL Parser results can
become `CODE_VERIFIED` facts only after their commit, blob, locator and excerpt hash have
been revalidated.

## Bounded investigation

`InvestigationApplicationService` creates one Investigation per Information Need and pins
the repository commit for its lifetime. `InvestigationRunner` asks for one structured action
at a time, validates it through the existing Tool Registry, and stops on complete Coverage,
hard budget, duplicate/no-progress limits, cancellation, or an unrecoverable boundary error.
Workflow unit generation accepts an optional ID-only investigation context provider; model
output cannot attach arbitrary Fact or Evidence IDs directly.

## Grounding and complete workflow

`GroundingService` validates repository/commit scope, Evidence availability, Fact status,
open conflicts and Claim-to-Fact links. Unsupported current-state claims never become
confirmable content. A configured supplement provider can make one targeted attempt; its
Evidence is grounded again before use.

The complete workflow supports up to three outline levels and at most 15 confirmation units.
Only the current dependency-ready unit can be generated or confirmed. A multi-node unit
returns structured sections, while section titles remain locked to the confirmed outline.
Document Quality issues block finalization; user-approved revisions create new section and
document versions without overwriting prior snapshots.

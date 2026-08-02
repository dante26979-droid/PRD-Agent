# PRD Agent P0 production runbook

Agent Runtime v4 graduation and v1 retirement are governed by the separate
[rollout runbook](./agent-runtime-rollout-runbook.md). The deployment steps in
this document do not by themselves authorize a rollout stage transition.

This profile targets a small number of stable users on one server. It uses TLS
+ Basic Auth + an API allowlist; no OIDC or external identity platform is
required. PostgreSQL is the durable source of truth. Redis is disposable
wake-up transport. The ingress rejects a client above the bounded request or
connection limits with HTTP `429`; this is an in-process temporary block and
does not grant the container host-firewall or network-administration privileges.

## 1. External inputs

The repository cannot generate or infer these operator-owned values:

- a DNS name resolving to the server, plus its TLS certificate and private key;
- one Basic Auth username/password per allowed user;
- a DeepSeek API key and an available configured model;
- Feishu App ID/App Secret and the token of one existing Wiki Docx node;
- an absolute backup directory mounted from, or synchronised to, another
  machine/storage system.

The Feishu app must be published and able to read Wiki metadata and edit the
configured Docx. The worker never creates or accepts an arbitrary document
target. Existing bot write permission satisfies the write part, but the concrete
Wiki node must also grant the app access.

## 2. Create untracked secrets

Create `infra/production/secrets/`. It is ignored by Git. Every file must
contain only its value. On Linux, create a dedicated host group for these
bind-mounted files, set `PRD_AGENT_SECRET_GID` to that numeric group ID, assign
the files to the group, and use mode `0640`. The non-root Go and Agent
containers receive only this supplemental group.

Required files:

| File | Content |
| --- | --- |
| `postgres_password.txt` | PostgreSQL password |
| `database_dsn.txt` | `postgresql://prd_agent:<password>@postgres:5432/prd_agent` |
| `deepseek_api_key` | DeepSeek API key |
| `proxy_secret.txt` | independent `openssl rand -hex 32` value |
| `agent_rpc_token.txt` | independent `openssl rand -hex 32` value |
| `export_confirmation_secret.txt` | independent `openssl rand -hex 32` value |
| `backup_encryption_key.txt` | independent high-entropy backup passphrase |
| `feishu_app_id.txt` | Feishu App ID |
| `feishu_app_secret.txt` | Feishu App Secret |
| `feishu_wiki_node_token.txt` | fixed Wiki node token |
| `basic_auth.htpasswd` | allowed users and password hashes |
| `tls_certificate.pem` | full TLS certificate chain |
| `tls_private_key.pem` | TLS private key |

Generate `basic_auth.htpasswd` with a standard `htpasswd` implementation. Its
usernames must exactly match the comma-separated `PRD_AGENT_ALLOWED_USERS`.
Do not reuse the proxy, RPC, confirmation or backup secrets.

## 3. Configure host values

Create an operator-owned `.env.production` outside version control:

```dotenv
PRD_AGENT_PUBLIC_ORIGIN=https://prd.example.com
PRD_AGENT_SERVER_NAME=prd.example.com
PRD_AGENT_ALLOWED_USERS=alice,bob
PRD_AGENT_DEFAULT_TENANT=default
PRD_AGENT_SECRET_GID=2000
PRD_AGENT_FEISHU_DOCUMENT_HOST=your-tenant.feishu.cn
PRD_AGENT_BACKUP_DIRECTORY=/absolute/off-host-or-synchronised/backup/path
PRD_AGENT_LLM_MODEL=deepseek-v4-pro
PRD_AGENT_FIXED_REPOSITORY_BINDING_ID=github:dante26979-droid/PRD-Agent
PRD_AGENT_FIXED_REPOSITORY=dante26979-droid/PRD-Agent
PRD_AGENT_FIXED_REPOSITORY_REVISION=replace-with-full-40-character-commit-sha
PRD_AGENT_REPOSITORY_REFRESH_INTERVAL_SECONDS=30
PRD_AGENT_MAX_GLOBAL_RUNNABLE=2
PRD_AGENT_MAX_RUNNABLE_PER_OWNER=1
PRD_AGENT_MAX_WAITING_RUNS=20
PRD_AGENT_DEFAULT_WORKFLOW_VERSION=agent-runtime.v1
PRD_AGENT_ROLLOUT_POLICY_VERSION=<environment-policy-version>
PRD_AGENT_V4_CANARY_BASIS_POINTS=0
PRD_AGENT_V4_SHADOW=false
PRD_AGENT_V4_INTERNAL_IDENTITIES=default:admin
```

The production Compose profile forwards these rollout settings to both the API
assignment writer and Maintenance dispatcher. Keep the default workflow at v1
and the percentage at zero until the corresponding environment gate passes;
use the internal identity allowlist for a bounded v4 deployment smoke.

The backup directory must exist before startup. A local directory without an
off-host copy protects against database corruption but not total server loss.

The P0 profile deliberately binds every new task to one fixed GitHub repository.
`go-capability` maintains a persistent bare Git mirror and refreshes GitHub's
default-branch HEAD in the background. Fetch is incremental: unchanged Git
objects are reused, and the database HEAD is advanced only after the new commit
is fully available locally. A task pins the latest successfully mirrored commit
when it is created, so later branch movement cannot change its evidence.

`PRD_AGENT_FIXED_REPOSITORY_REVISION` is the cold-start fallback. If GitHub is
temporarily unavailable, existing and new tasks continue to read the last
verified local mirror HEAD; a pre-mirror deployment can also read the legacy
filesystem snapshot for the configured fallback SHA. Local uncommitted files
and unmerged non-default branches are never visible to the Agent. The configured
public repository is anonymously readable, so it does not require a GitHub
token, OAuth application, repository selector or identity platform.

## 4. Validate and start

```bash
docker compose \
  --env-file /absolute/path/.env.production \
  -f infra/production/docker-compose.yml \
  -f infra/production/docker-compose.go.yml \
  --profile go-control-plane config --quiet

docker compose \
  --env-file /absolute/path/.env.production \
  -f infra/production/docker-compose.yml \
  -f infra/production/docker-compose.go.yml \
  --profile go-control-plane build

docker compose \
  --env-file /absolute/path/.env.production \
  -f infra/production/docker-compose.yml \
  -f infra/production/docker-compose.go.yml \
  --profile go-control-plane up -d
```

The one-shot migration container must complete successfully. API,
maintenance, Integration Worker and ingress readiness all fail closed when
their required database tables or file secrets are missing.

## 5. Smoke and operational checks

Rollout assignment is disabled unless `PRD_AGENT_ROLLOUT_POLICY_VERSION` is
set. When enabling it, configure `PRD_AGENT_V4_CANARY_BASIS_POINTS` in the
inclusive range `0..10000`; optional `PRD_AGENT_V4_SHADOW` enables only the
deterministic zero-remote-effect evaluator for v1 control runs. Identity lists
use comma-separated `tenant_id:owner_id` values in
`PRD_AGENT_V4_INTERNAL_IDENTITIES` and
`PRD_AGENT_V4_EMERGENCY_DENY_IDENTITIES`; explicit task IDs use
`PRD_AGENT_V4_EXPLICIT_TASK_IDS`. Changing the policy version affects only new
assignments; retry and reopen inherit their persisted assignment.

```bash
curl --fail --user alice 'https://prd.example.com/backend/api/v1/me'
curl --fail --user alice 'https://prd.example.com/backend/api/v1/health/ready'
docker compose \
  --env-file /absolute/path/.env.production \
  -f infra/production/docker-compose.yml \
  -f infra/production/docker-compose.go.yml \
  --profile go-control-plane ps
```

Create one task in the Web UI and verify:

1. the run reaches a terminal state;
2. model attempts and immutable
   `github://<binding>@<commit>/<path>#L<line>` evidence are visible;
3. the working draft can be downloaded;
4. publish preview shows only the fixed Feishu target;
5. explicit confirmation updates that target and returns only its safe Wiki URL.

`https://prd.example.com/backend/metrics` exposes bounded route/method/status
request counts and route latency sums/counts. Container logs are size-rotated.
Alert externally on unhealthy containers, repeated Agent unknown/manual-review
events, HTTP 5xx/429 growth and a `latest-success` backup marker older than 24
hours.

The default ingress limits are deliberately conservative for the small-user
profile:

- Web requests: 5 requests/second per client IP, with a burst of 30.
- API requests: 5 requests/second per client IP, with a burst of 10.
- Concurrent requests: 20 per client IP. With HTTP/2, each concurrent request
  is counted separately by Nginx.

These controls reject traffic while it exceeds the configured bounds; they are
not a durable firewall ban and do not protect against an upstream bandwidth
exhaustion attack. Review `429` logs before tightening the values so normal Web
asset loading and API polling are not blocked.

## 6. Backup and restore drill

The backup agent creates an encrypted, checksummed PostgreSQL custom-format dump
every 15 minutes and validates its catalog before publication. Plaintext work
files live in a private Docker volume and are removed on success or process
exit. Redis is not backed up.

At least once before production cutover, copy one encrypted backup to an
isolated host/database, verify its checksum, decrypt it with the separately
stored backup key, run `pg_restore --list`, and restore it into an empty
isolated database. Never test restore against production. The bundled image
contains `/usr/local/bin/prd-agent-restore`; it additionally requires a mounted
`/run/secrets/restore_database_dsn` and:

```bash
PRD_AGENT_RESTORE_CONFIRM=RESTORE_INTO_ISOLATED_DATABASE \
  /usr/local/bin/prd-agent-restore /backups/prd-agent-<timestamp>.dump.enc
```

Cutover is blocked if no recent backup exists or the isolated restore drill has
not passed.

## 7. Failure semantics

- Database unavailable: readiness fails; no in-memory success is reported.
- Agent stream interrupted before any model attempt: bounded automatic replay.
- Agent stream interrupted after a planned/paid model attempt: quarantine and
  require a user-created retry; never silently pay twice.
- Final Agent ACK lost after the run committed: reconcile the terminal run and
  do not execute it again.
- Feishu failure before mutation: bounded retry.
- Feishu result unknown after mutation: read-only reconciliation only.
- Feishu content mismatch or unrecoverable ambiguity: `MANUAL_REVIEW`; never
  overwrite or report false success.
- Queue full: API returns `429 CAPACITY_EXHAUSTED` with `Retry-After`.

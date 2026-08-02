# Agent Runtime v4 rollout and v1 retirement runbook

This runbook executes the phase 10 and 11 mechanisms. It does not waive an
environment gate: staging/production evidence must come from the named
environment, and every mutation is a dry-run unless `--apply` is present.

## 1. Immutable evidence pack

For every environment and canary cohort, retain an operator-owned directory
containing the exact commit, migration set, gate manifest, gate report,
contract/PostgreSQL reports, shadow policy and redacted logs. Hash files before
invoking `rolloutctl`; never place report bodies or credentials in command
arguments, database rows or application logs.

The gate manifest and report use `agent-runtime-gate.v1`. They must bind the
candidate `agent-runtime.v4`, baseline `agent-runtime.v1`, dataset/config/report
hashes, repetitions, hard thresholds and blocking cases. A provider outage is
an environment failure, not permission to lower a hard threshold.

## 2. Local readiness

Apply migrations through `0021`, run the full local suite, and record a passed
gate. Inspect state before every command and copy its `version` into
`--expected-version`.

```bash
rolloutctl inspect
rolloutctl record-gate \
  --manifest /controlled/evidence/gate-manifest.json \
  --report /controlled/evidence/gate-report.json \
  --expected-version 1 \
  --idempotency-key local-gate-<release> \
  --evidence-hash sha256:<evidence-pack-hash> \
  --actor-ref operator:<id>
```

Review the JSON dry-run, then repeat the identical command with `--apply`.
Transition only with the returned passed decision:

```bash
rolloutctl transition \
  --to LOCALLY_VERIFIED \
  --policy-version <policy-version> \
  --gate-decision-id <decision-id> \
  --expected-version 1 \
  --idempotency-key local-transition-<release> \
  --evidence-hash sha256:<evidence-pack-hash> \
  --actor-ref operator:<id>
```

Render readiness first, then record the same input. The migration head must be
exactly `0021`; blockers are computed by the control plane and cannot be
cleared through flags.

```bash
rolloutctl readiness render \
  --expected-version 2 --idempotency-key readiness-render-<release> \
  --actor-ref operator:<id> --commit <full-commit> \
  --migration-set-hash sha256:<hash> --migration-head 0021 \
  --contract-report-hash sha256:<hash> \
  --postgres-report-hash sha256:<hash> \
  --eval-manifest-hash sha256:<manifest-hash> \
  --shadow-policy-hash sha256:<hash> --gate-decision-id <decision-id>
```

Use `readiness record` with a new stable idempotency key to persist the reviewed
input, then `readiness verify --readiness-id <id>`. Promotion is blocked unless
the durable result is `STAGING_READY`, `verified=true`, and `state_stale=false`.

## 3. Environment graduation

Each arrow is a separate passed GateDecision and `transition` command:

```text
LOCALLY_VERIFIED -> STAGING_SHADOW -> STAGING_ENFORCE
-> PRODUCTION_CANARY -> PRODUCTION_DEFAULT -> LEGACY_DRAIN
```

- Staging shadow: at least 24 hours and 50 completed runs; v1 remains
  authoritative; extra model, capability, draft, unit and publish effects are
  all zero. Any trace/effect violation requires `pause`.
- Staging enforce: allowlisted identities only; exercise Outline through
  publish/reconcile, crash recovery, budget exhaustion and
  pause/rollback/resume.
- Production canary: use distinct policy versions and decisions for 100, 500,
  2000, 5000 and 10000 basis points. Never skip a cohort in one command.
- Production default: stop new v1 assignment, retain v1 worker/producer/readers,
  complete the stability window and one rollback rehearsal.

Emergency commands also default to dry-run. `rollback` changes only new
assignments and pauses rollout; persisted assignments for existing runs remain
unchanged.

## 4. Legacy retirement

After `PRODUCTION_DEFAULT`, transition to `LEGACY_DRAIN`, then begin and refresh
the durable v1 drain until active runs/dispatches, unpublished outbox,
non-terminal ledger and recovery snapshots are all zero.

Producer removal and reader removal are separate reversible releases. Hash the
removed-symbol inventory. After producer removal, record at least one complete
publish-cycle observation window; any v1 reader hit resets the window. Keep
migration history and reserve old protobuf field numbers and enum values.

The final transition is rejected unless it binds all of:

- a completed, zero-count `DrainRecord`;
- a removal inventory hash;
- a persisted zero-hit reader observation evidence hash;
- a passed final GateDecision and the current state version.

```bash
rolloutctl transition \
  --to LEGACY_RETIRED --policy-version <v4-policy> \
  --gate-decision-id <final-decision> --expected-version <version> \
  --drain-id <completed-drain> \
  --removal-inventory-hash sha256:<hash> \
  --reader-observation-hash sha256:<hash> \
  --evidence-hash sha256:<final-pack> \
  --idempotency-key retire-v1-<release> --actor-ref operator:<id>
```

Review the dry-run. Add `--apply` only after the producer-removal rollback
commit, reader observation and protocol compatibility checks are independently
verified.

## 5. Stop conditions

Do not advance when a command returns an idempotency/version conflict, readiness
is blocked/stale, a hard gate fails, rollout is paused, or environment evidence
is incomplete. Preserve the current stage, record the failed evidence, and use
pause/rollback where applicable. Never manufacture staging/production evidence
from local deterministic tests.

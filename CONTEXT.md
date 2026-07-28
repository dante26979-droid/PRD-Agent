# PRD Agent

PRD Agent turns a product request, repository evidence, and relevant historical
requirements into a reviewable PRD, then publishes the confirmed document to
Feishu. It is a single product context with external content sources and a
durable orchestration control plane.

## Product workflow

**PRD Task**:
A user-owned request to investigate, draft, confirm, and publish one PRD.
_Avoid_: Job, ticket, generation request

**Agent Run**:
One recoverable execution attempt for a PRD Task, bounded by model, tool, time,
and retry budgets.
_Avoid_: Chat session, worker job

**Confirmation Unit**:
The smallest PRD scope that a user confirms or reopens independently.
_Avoid_: Approval block, review item

**Working Draft**:
The temporary versioned PRD content used during an active Agent Run before
publication to Feishu.
_Avoid_: Canonical PRD, historical PRD

**Published PRD**:
The current user-confirmed PRD body stored in its bound Feishu document.
_Avoid_: Database document, export copy

## Sources and evidence

**Repository Snapshot**:
An authorized GitHub repository resolved to one immutable commit for a PRD Task.
_Avoid_: Current branch, live repository

**PRD Source Revision**:
An immutable observation of a Feishu document revision, including its provider
revision, modification time, and content hash.
_Avoid_: Local version, sync version

**PRD Catalog Entry**:
The searchable metadata record for one authorized Feishu PRD, without claiming
ownership of its complete body.
_Avoid_: Historical document copy, corpus file

**Section Locator**:
A stable reference that identifies a section or block range inside a PRD Source
Revision so the original text can be fetched from Feishu.
_Avoid_: Stored chunk, copied paragraph

**Retrieval Hit**:
A ranked PRD Catalog Entry or Section Locator returned by a retrieval run; it
becomes evidence only after source revision and access checks succeed.
_Avoid_: Fact, answer

**Evidence**:
A validated, source-located observation that may support a claim in the Working
Draft.
_Avoid_: Model knowledge, retrieval result

## Runtime and persistence

**Control Plane**:
The PostgreSQL-backed state for identity, tenancy, workflow, queue admission,
idempotency, external bindings, audit, and retrieval metadata.
_Avoid_: Document database, PRD body store

**Run Admission**:
The atomic decision that reserves bounded queue capacity for an Agent Run.
_Avoid_: Redis enqueue, worker start

**Queue Slot**:
A durable per-user and global capacity reservation held by an admitted,
non-terminal Agent Run.
_Avoid_: Redis message, process semaphore

**Integration Sync**:
A deduplicated background update that reconciles a Feishu document revision
into the PRD Catalog and retrieval index.
_Avoid_: Full import, corpus rebuild

**Model Attempt**:
One persisted request/response outcome for a remote LLM call within an Agent
Run, identified by a stable idempotency key.
_Avoid_: Prompt log, retry counter

**Provider Binding**:
A tenant-scoped association between an internal resource and an authorized
GitHub or Feishu resource.
_Avoid_: Credential, URL setting

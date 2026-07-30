---
status: accepted
---

# External systems own PRD and repository content

Feishu is the canonical owner of published PRD bodies and GitHub is the
canonical owner of repository content. PostgreSQL stores the orchestration
control plane, immutable source observations, locators, summaries, embeddings,
and audit data; it does not keep an indefinite canonical copy of every Feishu
document or repository tree. This avoids dual-write ownership while preserving
recovery, retrieval, and evidence traceability.

## Consequences

- A Published PRD is not complete until the Feishu write succeeds or is
  explicitly marked result-unknown for reconciliation.
- Retrieval must revalidate access and source revision before original text is
  sent to the model.
- Temporary Working Draft text may remain in PostgreSQL only for active recovery
  and a bounded retention period.

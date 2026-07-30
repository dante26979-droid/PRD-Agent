---
status: accepted
---

# Production uses a remote LLM only

The 2 vCPU / 2GB production profile calls a configured remote LLM API and does
not run or fall back to a local model. Local deterministic implementations
remain test and development adapters only; provider failure leaves the Agent
Run durably waiting, retryable, or failed instead of silently changing model
semantics.

## Consequences

- Production readiness fails when the remote model configuration is absent.
- Model attempts, budgets, retries, and resumability belong to the application
  loop rather than to LangChain or a local inference runtime.
- CPU and memory are reserved for the API, workers, PostgreSQL, Redis, and
  retrieval processing.

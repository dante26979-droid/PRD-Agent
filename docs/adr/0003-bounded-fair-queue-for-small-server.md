---
status: accepted
---

# Use a bounded fair queue on the small-server profile

The 2 vCPU / 2GB profile admits Agent Runs through PostgreSQL-backed global and
per-user Queue Slots, schedules admitted runs fairly across users, and executes
only one model/tool loop at a time. Redis is a disposable wake-up and broker
implementation, not the authority for capacity, ordering, or completion.

## Consequences

- Queue admission and task creation are committed atomically.
- Redis loss can delay work but cannot lose an admitted Agent Run.
- Waiting-for-user runs release worker execution capacity while retaining only
  the configured queue reservation semantics.
- Increasing worker concurrency requires a measured deployment profile change,
  not an ad hoc process flag.

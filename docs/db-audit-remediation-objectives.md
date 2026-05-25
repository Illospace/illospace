# Database Audit Remediation Objectives

Source audit: production database on `100.67.122.99`, observed 2026-05-25.

- [x] Strict deploy doctor counts the current org-owned credential tables and no longer fails when legacy `user_api_keys` is absent.
- [x] Deep embedding health uses DB-backed runtime memory credentials when a database session is available.
- [x] Compose production Postgres enables query observability knobs needed for slow-query analysis.
- [x] Compose production worker runs the legacy cycle scheduler so due `cycles` are not orphaned from execution.
- [x] Deep health exposes stale legacy cycle backlog signals.
- [x] Agent API call ledger has a run foreign key and query-shape indexes for usage/cost aggregation.
- [x] Idea state timeline queries have a supporting `(idea_id, changed_at, id)` index.
- [x] Domain record substring search uses trigram GIN indexing instead of an unused btree index.
- [x] Duplicate chat indexes covered by unique constraints are removed.
- [x] High-value free-text status columns have database check constraints.
- [x] A lightweight objective checker prevents unchecked remediation boxes from landing.
- [x] Tests cover the health, doctor, model, migration, and objective-check behavior.

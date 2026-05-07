# Evidence Regression Fixtures

Compact synthetic replay payloads for recent evidence-system failures.

- `run_1218_1222_semantic_progression.json` tracks the artifact-preview and semantic-verifier progression from missing worker evidence to trusted worker evidence.
- `run_1250_existing_pr_review_contract.json` preserves the difference between reviewing existing pull requests and creating a new PR.
- `run_1277_skill_catalog_audit_schema_failure.json` captures the read-only skill catalog audit failure where worker output looked like Python literals instead of strict JSON/tool-backed evidence.

These are intentionally not DB dumps. They keep the persisted artifact/history/verifier shapes small enough for tests and UI debugging.

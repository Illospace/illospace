# Illo-QA Close Criteria

An `[Illo-QA]` issue is complete only when its fix is deployed and the reported
runtime symptom is gone. A merge, a green pre-merge test suite, or an issue
automation transition is not post-deploy verification.

## Completion rule

Do not close an `[Illo-QA]` issue as `COMPLETED` until the issue records both:

- the named post-deploy verifying run ID; and
- a one-line quote from that run showing the original symptom is gone.

The run must execute against the deployed coordinator image that is meant to
contain the fix. If the probe cannot reach that runtime, the result is
`REQUIRES LIVE RUNTIME`, never an inferred pass.

## Symptom-gone rules

- **#311 — child-reader credentials:** at least one `spawn_worker` child reader
  has a readable summary through `run.get`, with no `GITHUB_TOKEN` or Project
  Context materialization error.
- **#306 — pull-request health:** `get_pull_request` or
  `pull_request_checks` for an open PR returns mergeability and CI state without
  a 403.
- **#312 — broad evidence reads:** a broad Tracker/Event read follows pagination
  to `evidence_health.completeness=complete` without hitting the visible-output
  limit or replacing pagination with compensating time slices.
- **#290 — tracker idempotency:** creating a tracker record twice for the same
  `external_id`, including an assignee-spelling variation, reuses one canonical
  record and leaves exactly one active row.

## Verification probe

[`scripts/post_deploy_qa_probe.py`](../scripts/post_deploy_qa_probe.py) is the
machine-checkable source of truth for these assertions. It drives the deployed
coordinator through Illo's hosted MCP bridge, then verifies the existing
`spawn_worker`, GitHub, workspace-data, and Domain tool receipts with `run.get`.
It does not call GitHub or the Domain database through a parallel client.

Use `--self-test` for an offline assertion check and `--dry-run` for the exact
manual checks. For a deployed run, pass `--target`, `--thread-id`, and the
symptom-specific runtime identifiers shown by `--help`; the bridge token comes
from `ILLO_BRIDGE_TOKEN` by default. A previously triggered verification can be
checked with `--run-id` instead of creating another run.

Record each emitted `PASS — evidence: ...` line on the issue with its AgentRun
ID. A `FAIL` reopens or keeps open the issue; `REQUIRES LIVE RUNTIME` leaves the
close gate pending.

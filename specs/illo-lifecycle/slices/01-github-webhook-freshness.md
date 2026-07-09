# Slice 1 — GitHub webhook freshness

**Blocked only on:** GitHub App (PR #265) activation (register App + Vault
store/bind). Everything else is buildable now against a test webhook.

## Contract unlocked
GitHub changes (issue/PR opened, closed, commented, labeled) are reflected in
`domain_records` within seconds, with freshness metadata surfaced on read — and a
reconciliation backstop catches anything the webhook stream drops.

## API seam / changes
1. **GitHub webhook router** — a sibling to
   [webhooks.py](../../../brain/app/api/routers/webhooks.py) that:
   - verifies `X-Hub-Signature-256` (HMAC-SHA256 over the raw body with the App
     webhook secret) — reject on mismatch;
   - maps the GitHub event (`issues`, `pull_request`, `issue_comment`,
     `label`, …) into an inbound envelope and calls the **existing**
     `submit_inbound_envelope()` ([service.py](../../../brain/systems/inbound/service.py)).
     Idempotency key = `x-github-delivery` (already recognized, webhooks.py:23).
   - **Seam:** `def github_event_to_envelope(headers, payload) -> InboundEnvelope`.
     Pure function, unit-testable with recorded GitHub fixtures. No DB.
2. **Inbound wiring** — one `inbound_source_policy` (match GitHub origin) + one
   `inbound_domain_projection` mapping the payload → a GitHub-ticket domain
   object type. Projection idempotent-upserts keyed on the GitHub node id
   (models: [inbound.py](../../../brain/platform/db/models/inbound.py):129–238).
3. **Freshness metadata** — add to the projected record (Alembic migration):
   `source_updated_at` (GitHub's `updated_at`), `last_synced_at`, `sync_source`
   (`webhook` | `reconcile`). Surface "synced Xm ago" in the read output of
   `illo_read`/`domain.inspect`. **One owner** (see README invariant 4).
4. **No-owner-skip landmine** — the GitHub connection must carry an
   `owner_user_id`, else all GitHub triage silently skips
   (service.py:867,1096). Ensure it's set; Slice 3 makes the backstop explicit.
5. **Reconciliation backstop** — repurpose ONE low-freq cycle to delta-poll
   GitHub and fill gaps. It must **reuse `submit_inbound_envelope`** (same
   projection, `sync_source='reconcile'`) — not a second writer (README
   invariant 1). This replaces Slice 0's frequency bump.
6. **Scope out `cortex_emerge`** — confirm the `ideas` poll
   ([cortex_emerge.py](../../../brain/jobs/pipelines/cortex_emerge.py)) is not a
   shadow triage source; if teammates read `ideas` today (A2), point the read
   path at the domain.

## What the human can run/see
- Send a recorded GitHub `issues` delivery to the router locally → a
  `domain_record` appears/updates.
- On a live repo: open → comment → close a test issue; watch the record track it
  within seconds. Read it back via `illo_read` and see `synced Xs ago`.
- Disable the webhook, change the issue, wait one reconcile tick → record
  catches up with `sync_source='reconcile'`.

## Verification
- Unit: `github_event_to_envelope` maps each event type correctly (fixtures);
  bad signature rejected; replayed `x-github-delivery` is idempotent (no dup
  record).
- Integration: webhook → envelope → projection → record, end-to-end, with
  `source_updated_at` populated.
- Reconcile: a change made while the webhook is down is backfilled exactly once.

## Must stay green
- The existing `/webhooks`, `/api/mcp`, and Slack inbound paths (all share
  `submit_inbound_envelope`) — no regression to their envelopes.
- Idempotency guarantees on `inbound_events` (unique `(connection_id,
  idempotency_key)`).

## Feedback that would change this slice
- Whether PRs and comments become their own object types or fields on the issue
  record (default: one `github_ticket` type with related events).
- Reconcile cadence (default conservative, e.g. hourly).

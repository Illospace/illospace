# Illospace #457 — Illo-side app-report intake

## What changed and why

- Added `app_report` as a registered shared inbound-envelope kind beside
  `slack_message`.
- Added `build_app_report_work_intake_payload(...)`, which validates the
  Uwear app contract and shapes a `customer_request.issue` or
  `customer_request.idea` event for the canonical `WorkIntakeEvent` /
  `admit_work(...)` boundary.
- Added `process_app_report_envelope(...)`, which uses the inbound connection
  owner as Illo execution authority, admits a headless customer-request run,
  and completes the existing `InboundEventRow` with `STATUS_PROCESSED`.
- The normalized app report is retained in run metadata. `generation_ids` and
  `batch_ids` are also copied into the run target / decision-receipt target, so
  a later dossier join can use explicit identifiers without guessing.
- A successful admission returns and records this acknowledgement at
  `ilo_outcome.ack`:

  ```json
  {
    "status": "accepted",
    "message": "Thanks — your issue was received.",
    "event_id": "<Illo inbound event UUID>"
  }
  ```

- No database schema or public route was added. Existing
  `InboundEventRow`, `InboundDecisionReceiptRow`, and agent-run storage are
  sufficient.

## Exact companion `uwearaiapp` change

Use the existing authenticated inbound webhook ingress:

```http
POST ${ILLO_BASE_URL}/webhooks
Authorization: Bearer ${ILLO_BRIDGE_TOKEN}
Content-Type: application/json
X-Illo-Idempotency-Key: <stable report/delivery id>
```

The bridge token must have the existing `signal:submit` scope. `POST
/webhooks` is the correct adapter because it already accepts arbitrary envelope
kinds and calls the same `submit_inbound_envelope(...)` service as hosted MCP.
The hosted `illo_submit` tool is not the correct wire contract here: it
deliberately converts its arguments into a fixed `submission` envelope and
does not expose a caller-selected kind. No bespoke app-report route or new MCP
tool is required.

Send this outer envelope:

```json
{
  "kind": "app_report",
  "origin": "uwear.app_report",
  "payload": {
    "email": "customer@example.com",
    "profileId": "uwear-profile-id",
    "type": "Issue",
    "message": "The customer's report text",
    "attachments": [
      "https://...",
      {
        "url": "https://...",
        "name": "optional structured attachment"
      }
    ],
    "generation_ids": [1234, "another-generation-id"],
    "batch_ids": [5678, "another-batch-id"]
  },
  "summary": "The customer's report text",
  "desired_outcome": "Admit this customer request for Illo coordination.",
  "idempotency_key": "<same stable report/delivery id>"
}
```

Payload rules:

- `email`, `profileId`, `type`, and `message` are required and non-empty.
- `type` is `Issue` or `Idea` (case-insensitive at ingress and stored in that
  canonical casing).
- `attachments` is a required array and may be empty.
- `generation_ids` and `batch_ids` are optional arrays of integer or string
  identifiers.

Companion implementation sequence:

1. In `ContactSupportModal`, capture the relevant recent generation ids and
   batch ids at the same moment as the screenshot/report. Include those exact
   ids in the request to `contact-support.js`; do not reconstruct them later
   from timestamps or reporter identity.
2. In `contact-support.js`, preserve the existing Retool request and its legacy
   payload unchanged.
3. Add an independent call to the Illo endpoint above, projecting the modal
   fields plus the captured `generation_ids` / `batch_ids` into the
   `app_report` envelope.
4. Return or surface `ilo_outcome.ack` to the modal after Illo accepts the
   report. Retrying with the same idempotency key returns the recorded result
   without admitting duplicate work.

The Illo call is additive to Retool. A failure in either destination should be
reported/handled explicitly by the companion implementation rather than
silently removing the other delivery.

## Acceptance split

### Fully satisfied by this Illo-side change

- The receiving `app_report` lane exists on the same shared inbound and
  work-intake boundaries used by Slack.
- A valid app-report envelope creates an admitted customer-request run and
  completes its inbound event as `processed`.
- Successful admission returns and durably records a reporter acknowledgement.
- `generation_ids` and `batch_ids` survive in the admitted run metadata and
  target / decision receipt for deterministic dossier joins.
- Existing Retool code and behavior are untouched by this repository.

### Depends on the `uwearaiapp` companion change

- `ContactSupportModal` actually delivers each report to Illo in addition to
  Retool.
- The modal captures and sends the correct recent generation and batch ids.
- The Netlify response relays/displays Illo's returned acknowledgement to the
  reporter.
- The existing Retool forward continues to receive its current payload
  unchanged after the additive Illo call is introduced.

## Verification

Command:

```bash
/Users/redamjahed/Documents/UwearDev/illospace-project/venv/bin/python -m pytest \
  tests/test_app_report_intake.py \
  tests/test_inbound_webhooks.py \
  tests/test_inbound_preservation.py \
  tests/test_slack_teammate.py \
  tests/test_slack_channel_monitor.py \
  tests/test_work_intake.py \
  tests/test_work_intake_full_architecture.py \
  tests/test_architecture_boundaries.py -q
```

Result:

```text
collected 136 items
134 passed, 2 skipped in 4.41s
```

The skips are existing environment-dependent cases in
`tests/test_inbound_webhooks.py`.

## Migration

No migration was added, so no Alembic head selection was required.

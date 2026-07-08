## Role

You investigate **customer-reported generation-quality complaints** for Uwear,
read-only. Given a customer `profile_id` (and a complaint such as "the garment
has no pockets", wrong color, missing detail), you fetch the customer's recent
garment generation(s) from the production database, read the prompt, inputs,
model, and QA/status, and produce a concrete, evidence-backed **hypothesis** for
why the customer got that result. This is the mechanical first-pass triage that
precedes any engineering ticket.

## Use When

- A customer support alert reports an unexpected or wrong generation and carries
  a `Profile ID` (for example a Retool "New: Issue" with a profile id and an
  attachment).
- Someone asks "why did this user get this generation?", "investigate the
  generation for profile X", or to explain an unexpected garment result.
- You already have a `generation_id` or `profile_id` and need the payload behind
  a result.

## Do Not Use When

- The task is generic production-database analysis (use `prod-postgres-analysis`
  directly) or unrelated data.
- Any write, migration, backfill, schema change, or bulk export.
- Exporting customer PII. You need technical generation facts, not personal data.

## Credential Boundary And Safety

This skill is a specialization of `prod-postgres-analysis`; obey its full
safety contract. In short:

- Connect read-only with the Vault key **`PROD_POSTGRES_READONLY_URL`**, mounted
  as a `secret_env` for the single tool call. Never print, echo, log, or persist
  the connection URL or password.
- The run image has **`psycopg2`** (no `psql`, no `psycopg`). Query via Python +
  psycopg2.
- Every session: `SET default_transaction_read_only = on;` (or
  `BEGIN READ ONLY;`), `SET statement_timeout = '15s';`,
  `SET lock_timeout = '2s';`, `SET idle_in_transaction_session_timeout = '30s';`.
- `LIMIT` every row query (default 10, hard max 100). No DDL/DML/admin commands.
  Stop immediately on any write capability, permission error, or timeout loop.

Example mount:

```json
"secret_env": {
  "DATABASE_URL": {
    "vault_key": "PROD_POSTGRES_READONLY_URL",
    "reason": "Read-only generation investigation for a customer complaint"
  }
}
```

## The Canonical Join (profile_id → generation)

A customer profile owns generations through the **polymorphic owner columns**,
NOT the legacy batch path:

```sql
-- recent generations for a customer profile
SELECT g.generation_id, g.status, g.created_at, g.model_id,
       g.generation_setting, g.payload
FROM generation g
WHERE g.user_id = :profile_id
  AND g.user_type = 'profile'
ORDER BY g.generation_id DESC
LIMIT 10;
```

- `generation.user_type` is `varchar` (observed values include `profile` and
  `shopper`); customer generations are `user_type = 'profile'` with
  `user_id = profile.profile_id`.
- Do **not** use `generation.batch_id -> batch.profile_id`. That path is
  **legacy**: recent profiles bypass batches entirely (the newest generations
  carry no `batch_id`, and `batch.profile_id` stops at older ids), so it returns
  zero rows for recent customers even when they have generations.
- Choose the generation(s) relevant to the complaint by recency and the
  complaint's description/timeframe. A complaint's attached image is an output,
  so correlate via `generation_result` / `generation_result_origin` when a
  specific result is referenced.

## What To Read For The Hypothesis

For the relevant generation(s), gather the payload with these supporting joins:

```sql
LEFT JOIN ai_model am
       ON am.model_id = g.model_id                       -- am.slug, am.display_name, am.model_family, am.provider
LEFT JOIN clothing_generation_association cga
       ON cga.generation_id = g.generation_id
LEFT JOIN clothing_item ci
       ON ci.clothing_item_id = cga.clothing_item_id      -- ci.tryon_prompt  (NULL => item semantics not carried through)
LEFT JOIN generation_result_origin gro
       ON gro.generation_id = g.generation_id             -- gro.status  (watch for 'non_compliant')
LEFT JOIN generation_result_qa grq
       ON grq.generation_result_id = gro.generation_result_id   -- QA status + decision
```

Key fields:

- **Composed prompt / inputs:** `generation.generation_setting` (jsonb; keys
  include `model_name`, `operations`, `qa`, `reference_attachments`,
  `img_ref_urls`, `subject_detection`, `resolution`, `upscale_factor`,
  `face_enhancement`) and `generation.payload`.
- **Garment try-on prompt:** `clothing_item.tryon_prompt`. A NULL here means the
  item-level garment semantics (pockets, cuffs, etc.) were not carried through
  the canonical field — a common root cause of missing-detail complaints.
- **Model:** `ai_model.slug` / `display_name` / `provider`.
- **Outcome vs QA:** `generation_result_origin.status` (e.g. `non_compliant`)
  versus the `generation_result_qa` decision. A `non_compliant` result that
  still passed QA is a QA-gap signal worth calling out.

## Forming The Hypothesis

State one concrete, evidence-backed cause. Common patterns:

- **Prompt / detail preservation:** the composed prompt or `tryon_prompt` never
  specified the missing detail, or `tryon_prompt` was NULL so the model inferred
  from images.
- **Reference-image gap:** `reference_attachments` / `img_ref_urls` lacked the
  detail.
- **Model behavior:** the model (`ai_model.slug`) dropped the detail.
- **QA gap:** `generation_result_origin.status = non_compliant` but QA passed —
  the failure was not caught.

## Output Contract

Return, with no customer PII (no email/name):

- **Hypothesis:** one sentence, the most likely cause.
- **Evidence:** the relevant prompt/settings text, `tryon_prompt` (or that it was
  NULL), model slug, `generation.status`, `generation_result_origin.status`, and
  the QA decision. Cite the `generation_id`(s).
- **Recommended issue:** a ready-to-file body for a `uwear-ai/uwear-backend`
  GitHub issue that points the owner at the payload to confirm or deny, prefixed
  with `> *This was generated by AI during triage.*`.
- If the profile has **no** generations via the canonical path, say so plainly
  and ask support for the `generation_id` or asset id — do not guess.

## Failure Modes

- **Vault key missing:** stop and request `PROD_POSTGRES_READONLY_URL` through
  Vault; never accept a pasted DSN.
- **No generations for the profile:** report it plainly; the alert's `profile_id`
  may be wrong, or a specific result id is needed. Do not fall back to the legacy
  batch path and conclude "no generations".
- **Ambiguous which generation:** list the recent candidates with timestamps and
  ask which, rather than asserting.
- **Sensitive content:** aggregate/technical facts only; never expose personal
  data.

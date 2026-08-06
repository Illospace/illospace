> On-demand mode playbook for the Uwear engineering triage operating model.
> Core doc: Enterprise Documentation Domain `37` record `1155` (bundled skill
> `uwear-engineering-triage`); fetch per its **On-demand Run Modes** section.
> The core doc's always-on rules — Ownership, Deploy-State Ladder, States,
> Before Posting gates, Public Output — still govern this mode.

## Direct Customer Support

Some alerts are customer-support reports, not GitHub triage — e.g. a Retool
"New: Issue" carrying a `User`, a `Profile ID`, a `Message`, and often an image
attachment, reporting an unexpected or wrong generation ("the garment has no
pockets", wrong color, missing detail). For these, investigate first; do not just
record a tracker and assign a human.

Do the mechanical investigation yourself, read-only, then act:

1. **Investigate the generation.** Use the `uwear-generation-investigation` skill
   (read-only production Postgres via `prod-postgres-analysis` /
   `PROD_POSTGRES_READONLY_URL`). For the reported `Profile ID`, fetch the recent
   generation(s) via the **canonical owner join** (`generation.user_id =
   <profile_id> AND generation.user_type = 'profile'`; the
   `generation.batch_id -> batch` path is legacy and misses recent profiles) and
   read the request **prompt/inputs** (`generation.generation_setting`,
   `clothing_item.tryon_prompt`), the **model** (`generation.model_id ->
   ai_model`), and the **outcome/status** (`generation.status`,
   `generation_result_origin.status`, `generation_result_qa`).
2. **Form a hypothesis.** State a concrete, evidence-backed reason for the result
   (e.g. the try-on prompt/settings never specified the missing detail; the
   reference image lacked it; the model dropped it; a QA/status signal explains
   it).
3. **File first, unconditionally.** Follow the canonical
   [customer-bug filing policy](creating-work-items.md#customer-bug-filing-policy).
   For generation/API behavior, use `uwear-ai/uwear-backend`; add the payload
   evidence + hypothesis and point the owner at the payload to confirm or deny.
   After the hypothesis is formed, call `create_github_issue` before resolving
   ownership or asking a routing question. Filing does not wait for an owner or
   a human confirmation.
4. **Resolve ownership on the filed issue.** After `create_github_issue` returns
   the issue number and URL, run that playbook's **Post-create ownership and
   readiness enrichment**. Attempt ownership with the unchanged builder-first,
   specialization, and load-balancing rules, then update the filed issue.
5. **Reply in-thread** on the alert with the resulting artifact references and a
   one-line hypothesis.

**Owner: none during investigation and at initial filing; resolution follows
filing.** Illo runs the first-pass investigation. The issue exists before the
shared ownership step selects a human owner and readiness label from the core
rules. Do not auto-assign a customer-generation issue to Axel merely because the
output came from an AI model.

**Mandatory branch — I don't know the owner.** No owner needs to be known to
reach this branch. Keep the filed issue unassigned, include the required
`Ownership: Unassigned — <specific ambiguity or missing evidence>.` statement in
the issue body, and state exactly what ownership or routing fact is unresolved.
If a routing question is useful, file the issue first, register the question as
an open ask with a named human owner and an explicit expiry, and @-mention that
human in Slack. The question never replaces or delays the issue.

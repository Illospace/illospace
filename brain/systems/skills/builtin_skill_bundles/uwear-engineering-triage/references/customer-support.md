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
3. **Open the durable work item.** Follow the canonical
   [customer-bug filing policy](creating-work-items.md#customer-bug-filing-policy).
   For generation/API behavior, use `uwear-ai/uwear-backend`; add the payload
   evidence + hypothesis and point the owner at the payload to confirm or deny.
   Run that playbook's **Pre-create ownership and readiness gate** after the
   hypothesis is formed and before `create_github_issue`. Apply its result in
   the create call, including an explicitly requested assignee when present.
4. **Reply in-thread** on the alert with the resulting artifact references and a
   one-line hypothesis.

**Owner: none during investigation; resolved before filing.** Illo runs the
first-pass investigation. Once the hypothesis exists, the shared pre-create gate
selects the human owner and readiness label from the core rules. Do not
auto-assign a customer-generation issue to Axel merely because the output came
from an AI model. If the shared gate cannot resolve ownership, file it
unassigned with the required ambiguity statement in the issue body.

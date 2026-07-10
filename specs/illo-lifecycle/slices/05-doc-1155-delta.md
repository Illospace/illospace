# Slice 5 activation artifact — live doc 1155 delta

The runtime source of truth for triage prose is Enterprise Documentation
(Domain 37) record **1155**, slug `uwear-engineering-triage`. The bundled
[SKILL.md](../../../brain/systems/skills/builtin_skill_bundles/uwear-engineering-triage/SKILL.md)
already carries these edits (kept in sync per the PR #268 pattern); this file
is the exact delta to apply to the **live doc** at activation, written as
anchored edits so it composes with other in-flight edits to the same record
(the coordinator digest-contract redesign also edits 1155 — whichever lands
second must re-read the doc first and apply onto the latest content).

**Apply mechanics:** `manage_domain` → `update_record` on record 1155 with a
`data.content` patch and `expected_version` = the version just read (v5 as of
2026-07-10 before either edit; expect it higher if the digest edit landed
first). Requires explicit approval — this is a live-runtime write.

## Edit 1 — dedup rule routes through the ladder

In **## Creating Work Items**, replace the bullet beginning
`**One problem = one issue — check before filing.**` … ending
`…never split one error/Rollbar alert into multiple issues.` with:

> - **One problem = one issue — check before filing.** Before calling
>   `create_github_issue`, search open AND recently-closed GitHub issues and
>   Domain 1 tracker records for the same error signature, Rollbar id (prefer
>   the structured `rollbar_item` field), endpoint, profile id, or root cause.
>   If a match exists — even if closed or `Done` — do NOT file a new issue and
>   do NOT blindly skip: follow the **Deploy-State Ladder** below. Never split
>   one error/Rollbar alert into multiple issues.

## Edit 2 — insert two sections before `## States`

Insert the full text of the bundled SKILL.md's
**## Deploy-State Ladder (re-firing alerts)** and **## Urgent Promotion**
sections (copy verbatim from the bundled skill — they are the canonical text)
immediately before `## States`.

## Edit 3 — `Done` redefinition

In **## States**, replace the `Done` bullet with:

> - `Done`: linked PR merged or issue closed. For an alert-linked ticket (one
>   with a `rollbar_item`), `Done` additionally requires the fix deployed to
>   prod AND verified quiet (`deploy_state` = `verified`) per the
>   **Deploy-State Ladder** — merged-to-staging is not done. A `Done` item must
>   not appear in anyone's priority workset — see **Before Posting** and
>   **Public Output**.

## Edit 4 — Before Posting gate

In **## Before Posting**, append gate 4 after the dedup gate:

> 4. **Deploy-state gate:** no expected-noise re-fire (Ladder case 2) re-pings
>    an owner or appears as new work; every reopened ticket (Ladder case 3)
>    names the builder and the failed fix.

## After applying

- Re-read the record; confirm the new sections render and the version bumped.
- The bundled SKILL.md and the live doc must say the same thing — if the doc
  has diverged elsewhere (e.g. digest-contract edits), leave those parts
  alone; these four edits are self-contained.
- Verify with the run-952-style READ-ONLY dry runs in the slice spec
  ([05-deploy-state.md](05-deploy-state.md) → Verification).

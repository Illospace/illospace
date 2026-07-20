# Issue #331 deploy note — chantier declare activation

This branch intentionally performs no live Slack, GitHub, or Domain write.
Deployment activates the code first, then updates the already-created on-demand
playbook mirror and runs the disposable Slack acceptance below.

## 1. Update the live on-demand playbook

Read Enterprise Documentation Domain `37`, `doc_page` record `1274`, and verify
its slug is `uwear-engineering-triage-chantier-operations`. Update only its
`content` field, using the just-read `expected_version`, to the exact UTF-8
contents of:

`brain/systems/skills/builtin_skill_bundles/uwear-engineering-triage/references/chantier-operations.md`

Expected content fingerprint:

- characters: `14344`;
- bytes: `14354`; and
- SHA-256: `f448a9cc10928d7e320f0c469bda06dcf40b3dc87e47468f6f1e15b2aa4092bf`.

Read record `1274` back and require byte identity. Do not overwrite a newer
unreconciled live edit. Domain `1` has the base chantier schema from #327; run
the current Alembic chain through `0032_chantier_superseded_by` before invoking
the #386 `merge_chantier` retirement operation.

## 2. illo-dev Slack acceptance

Use a unique disposable slug, for example
`qa-declare-<UTC-YYYYMMDD-HHMM>`, in a channel where the illo-dev Slack app is
present.

1. Mention Illo with
   `@Illo chantier: <unique-slug> — done means declare QA has one record and one threaded confirmation kind: quality owner: <reviewer>`.
2. Require one reply in that message's thread. It must say `created` and echo
   the exact slug, Done-means goal, kind, builder-first owner suggestion, ask
   for `next_step`, and either cite the created GitHub parent issue or say
   `mirror pending: <specific reason>`.
3. Query Domain `1` `chantier` records for the exact slug. Require exactly one
   active record with state `exploring`, the echoed fields, and the placeholder
   `Clarify the next most valuable step.`
4. Re-mention Illo with the same slug/title but a changed goal and
   `next_step: archive the disposable QA record after verification`. Require an
   `updated` threaded reply. Query again: still exactly one record, the same
   record id, a higher version, and the changed goal/next step.
5. Send an ordinary Illo mention without the keyword, for example
   `@Illo please summarize declare QA; do not create work`. Require no new
   chantier record; the unique-slug query still returns exactly one.
6. If a parent mirror was created, require `parent_issue` to contain its exact
   external id and verify re-declaration did not create a second parent. If the
   declaration included pasted GitHub issue links, verify they are native
   sub-issues of that parent.
7. Archive the disposable Domain record and close the disposable GitHub mirror,
   if one was created, after recording the evidence.

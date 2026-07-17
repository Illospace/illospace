> On-demand mode playbook for the Uwear engineering triage operating model.
> Core doc: Enterprise Documentation Domain `37` record `1155` (bundled skill
> `uwear-engineering-triage`); fetch per its **On-demand Run Modes** section.
> The core doc's always-on rules — Ownership, Deploy-State Ladder, States,
> Before Posting gates, Public Output — still govern this mode.

# Chantier Operations

Use the Domain `1` `chantier` object defined by
`references/chantier-record-contract.md`. A chantier is an outcome container;
it coordinates member work but never replaces the lifecycle, ownership, or
deploy verification of its member tickets and PRs.

For this playbook, an **active chantier** has state `exploring`, `building`,
`shipping`, or `verifying`. A `paused` or `done` chantier is not active, but it
still participates in snapshot continuity: when it leaves the active digest,
state who paused or closed it and why.

## When to Load

Fetch this playbook before:

- every scheduled coordinator digest;
- filing a GitHub issue or creating a Domain `1` work record;
- triaging an incoming item that may belong to an existing chantier; or
- assessing chantier freshness, close-out, or membership movement.

If neither this live record nor the bundled asset can be read, defer chantier
writes and say which operation was not performed. Do not guess membership or
silently fall back to an owner-primary digest.

## Chantier-primary Digest Contract v2

### Sweep and count

Read the complete Domain `1` `chantier` set in addition to the ordinary issue,
PR, tracker, event, and teammate-reply sweep. Reconcile `returned` with
`total_matching`, then count active chantiers exactly. The scope line keeps all
existing exact item counts and adds the active-chantier count, for example:

`Scope: 128 open issues + 12 open PRs across 4 repos, 45 active tracker records, 6 active chantiers; posting movement, not the full backlog.`

For each active chantier, read `slug`, `title`, `goal`, `state`, `owner`,
`refs`, `next_step`, `progress_note`, and `updated_at`. Resolve member refs
against the deploy-verified current state; do not infer goal completion from a
merged-to-staging PR or from issue/PR counts.

### Durable snapshot and diff

The Domain `37` `doc_page` with slug `uwear-coordinator-digest-snapshot` stays
the continuity source. Its complete payload must retain the existing run id,
timestamp, Slack message id, and per-person items, and add a `chantiers` list.
Each chantier entry stores at least:

```json
{
  "slug": "agent-runtime-chantier-layer",
  "state": "building",
  "member_refs": ["github:Illospace/illospace:issue:329"],
  "blockers": [],
  "next_step": "Land the coordinator behavior contract."
}
```

Diff chantier `state`, member refs, and blockers against the last successful
normal digest exactly as per-person items are diffed. A chantier changed
materially when it changed state, gained or lost a member, or hit or cleared a
blocker. Preserve the existing item diff for `Loose items`.

No silent departure: a chantier present in the last snapshot but absent from
the new digest must have a stated reason such as `done` with deploy-verified
goal evidence or `paused` by a named human. Otherwise surface it and keep it in
the new snapshot. The same rule still applies to per-person and loose items.

Only update the snapshot after a successful normal post. Skipped, failed, and
DEGRADED posts do not advance it.

### Digest shape

Write the visible digest in this order:

1. Scope line with exact item and active-chantier counts.
2. One section per active chantier with material movement. Each section names
   the chantier state, one line of progress toward the `Done means ...` goal,
   what moved since the last digest, next step, blockers (or `none`), and all
   next-action owners represented by that chantier.
3. One-line `Quiet chantiers` roll-up listing every other active chantier.
4. `Loose items` for tickets that belong to no chantier. Never force-group
   them. Keep the existing state, owner, blocker, and next-action evidence.
5. Optional `Unclaimed pool` and `Cleanup — safe to close` sections when
   non-empty.
6. A mandatory `Per-person recap` footer with Reda, Axel, and JB, every time.
   Each line gives that person's top next action across chantier and loose
   work. If empty, repeat the core contract's exact evidence checks: exact
   tracker `assignee`, GitHub issues assigned to the person's handle, PRs they
   authored, and builder-first engineering candidates. Then give a concrete
   rebalancing recommendation or `no rebalancing available because <reason>`.

The footer relocates the anti-starvation coverage; it does not weaken or
replace it. A DEGRADED 8:00 ET brief still carries all three names but makes no
absence or rebalance claim for an evidence slice that could not be checked.

On non-8:00 runs, a chantier state/member/blocker change is the primary
material-change bar. A loose item entering or leaving, changing state/owner/
blocker, an incident, or a teammate answer also remains material. With no
material change, do not post; record `Slack skipped: no material change`.

## Attach at Triage

Before filing any new work item, read every active chantier and compare the
candidate against:

- exact typed `refs` and member `external_id` values;
- title similarity; and
- shared root cause or explicit outcome/goal language.

Prefer exact refs/external ids over text similarity. Similar vocabulary alone
is not enough when the root cause or goal differs.

On a confirmed match:

1. Include the exact line `Part of chantier: <slug>` in the new GitHub issue
   body (or add it when triaging an already-filed issue). Keep the standard
   AI-authorship preamble when the coordinator writes the issue.
2. Add the item to the chantier's `refs` using the record contract's typed ref
   shape. For a GitHub issue, use
   `github:<owner>/<repo>:issue:<number>`; do not invent a source value outside
   the record contract.
3. Update the chantier `progress_note` with the inducted item and the movement
   it creates toward the goal. Preserve useful prior progress context within
   the field limit.
4. Use `expected_version` for the chantier update and verify all three surfaces:
   issue body, chantier `refs`, and `progress_note`. Report and retry a partial
   write; never claim attachment from only one surface.

Search/dedup and Deploy-State Ladder checks still happen before filing. An
existing issue is attached or updated; it is never refiled merely to gain
chantier membership.

## Induction

Induction is the atomic attach operation above, not a state change or an owner
change. Confirm that the chantier is active and that the item advances the
same `Done means ...` goal. Preserve the item's existing lifecycle and explicit
GitHub assignee. The new member ref is material movement for the next digest
and must appear in the chantier snapshot diff.

If evidence is ambiguous, leave the item loose and state the candidate
chantier for human confirmation. Do not attach speculatively.

## Propose a Chantier

When related work has no matching active chantier, propose one if any of these
signals is present:

- at least three related items;
- an arriving item includes a stated goal or PRD; or
- the work is an incident family.

The proposal should recommend a slug, title, `Done means ...` goal, kind,
prospective refs, owner, and next step, citing the family evidence. It is
recommend-only. Never auto-create a chantier from triage; wait for the declare
flow or explicit human authority.

## Freshness and Close-out

An active chantier untouched for 3+ days, or with a blank/missing `next_step`,
is must-surface even if no member otherwise moved. Name the owner, last update,
freshness defect, and the concrete action needed to restore it. A blocker is
also must-surface and participates in material movement when hit or cleared.

When deploy-verified member states demonstrate that the `Done means ...` goal
has been met, propose closing the chantier. Write an outcome summary in the
goal's language: what user/system outcome now holds and the verification that
proves it. Do not use PR counts as the outcome. Closure remains a proposal
unless the human explicitly delegated it.

## Declare Flow

Reserved for ticket #331. Do not declare or auto-create a chantier from this
playbook.

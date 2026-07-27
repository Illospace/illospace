> On-demand mode playbook for the Uwear engineering triage operating model.
> Core doc: Enterprise Documentation Domain `37` record `1155` (bundled skill
> `uwear-engineering-triage`); fetch per its **On-demand Run Modes** section.
> The core doc's always-on rules — Ownership, Deploy State on Read, States,
> Before Posting gates, Public Output — still govern this mode.

# Chantier Operations

Use the Domain `1` `chantier` object defined by
`references/chantier-record-contract.md`. A chantier is an outcome container;
it coordinates member work but never replaces the lifecycle, ownership, or
deploy verification of its member tickets and PRs.

For this playbook, an **active chantier** has state `exploring`, `building`,
`shipping`, or `verifying` **and has no `superseded_by` value**. Exclude a
superseded record before the active count, digest sweep, snapshot diff,
freshness check, and duplicate/hygiene narration, even if malformed legacy data
still carries an active state. A `paused` or `done` chantier is not active, but
it still participates in snapshot continuity: when it leaves the active digest,
state who paused or closed it and why. A duplicate retired with
`merge_chantier` has its explicit `superseded_by` reason and must not be
re-narrated as unresolved hygiene.

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

### Resolution harvest

Before diffing or composing, re-read the source thread for every tracker record
carrying `alert_slack_channel` and `alert_slack_thread_ts`; consider human
replies only. A deploy or named merged/promoted PR is movement evidence but
does not persist deploy state. A human `c'est fix` / `ça a l'air d'être fix`
confirmation sets `verified=true`, moves `status` to `Done`, and stores its
Slack timestamp in `verified_at` and `resolution_confirmed_ts`. If a later human reply says the problem still reproduces, the later reply wins: keep the
record open, set `verified=false`, clear `verified_at`, store
`resolution_reproduced_ts`, and quote the reproduce note and timestamp in
`progress_note`.

This path is read-only on Slack: call `read_slack_conversation`, never post or
react, even when the source thread is muted. Narrate the persisted change once
as movement/outcome in the next digest; never repeat the pre-harvest framing as
an unchanged open hypothesis.

For each active chantier, read `slug`, `title`, `goal`, `state`, `owner`,
`refs`, `next_step`, `progress_note`, and `updated_at`. Resolve member refs
against current GitHub ancestry plus the verification overlay; render an
indeterminate ancestry read as `unknown`. Do not infer goal completion from a
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

1. When `manage_slack(action="open_alert_surges")` returned an incident at
   run start, a lead incident section naming its subsystem, window,
   signatures, owner, and next action.
2. Scope line with exact item and active-chantier counts.
3. One section per active chantier with material movement. Each section names
   the chantier state, one line of progress toward the `Done means ...` goal,
   what moved since the last digest, next step, blockers (or `none`), and all
   next-action owners represented by that chantier.
4. One-line `Quiet chantiers` roll-up listing every other active chantier.
5. `Loose items` for tickets that belong to no chantier. Never force-group
   them. Keep the existing state, owner, blocker, and next-action evidence.
6. Optional `Unclaimed pool` and `Cleanup — safe to close` sections when
   non-empty.
7. A mandatory `Per-person recap` footer with Reda, Axel, and JB, every time.
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

Search/dedup and Deploy State on Read checks still happen before filing. An
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

The declaration door is an explicit Illo app mention containing the keyword
`chantier`, preferably in this compact shape:

`@Illo chantier: <title or slug> — done means <outcome> [kind: <kind>] [owner: <name>] [next_step: <step>] [links]`

Do not infer a declaration from related vocabulary, a proposal, passive channel
monitoring, or an ordinary mention without the `chantier` keyword. A direct
message that was not normalized as an Illo app mention is not this flow. Questions
such as "what chantiers are active?" remain reads, not declarations.

The Slack mention lane persists the record before the conversational reply:

1. Parse the title and `Done means ...` goal. Best-effort extras are `kind:`,
   `owner:`, `next_step:`, and pasted links. Convert GitHub issue URLs to the
   record contract's `github:<owner>/<repo>:issue:<n>` ref; retain other links
   as typed `slack` or `url` refs.
2. Derive a lowercase kebab slug. Before creating, lock the chantier object and
   match all active records in evidence order: exact typed refs, exact slug or
   normalized title, then one high-confidence title/slug/root-cause match. A
   stable root of at least three fully contained title/slug tokens is high
   confidence; broad one- or two-token vocabulary is not. Conflicting or
   multiple matches fail loudly for human resolution. A single match is an
   update of that record; never create a second record. Preserve the matched
   record's stable slug and title.
3. New records start in `exploring`. Guess `incident`, `quality`, `feature`, or
   `gtm` conservatively unless `kind:` is explicit. Preserve an existing kind,
   owner, goal, and next step when a re-declaration omits those explicit values;
   merge new refs without duplicating them.
4. If no goal is supplied, derive a clearly inferred `Done means ...` goal and
   label it as inferred in the reply. If no `next_step` can be inferred, use the
   contract placeholder and ask directly for `next_step` in the reply. Reject a
   new record when all placeholder signals occur together: empty `refs`, the
   generic inferred goal, the generic next step, and no owner. Ask for at least
   one durable ref, explicit goal/next step, or owner instead.

Reply in the declaration's Slack thread, even when the mention was top-level.
Say `created` or `updated`, then echo the slug, goal, explicit/guessed kind,
builder-first owner suggestion, next step (or the request for one), and mirror
outcome. An explicit `owner:` wins. Otherwise prefer the likely implementation
builder from current ownership evidence; use `builder TBD` when the evidence is
insufficient rather than assigning the declarer or coordinator by reflex.

For engineering chantiers (`feature`, `incident`, or `quality`), open the GitHub
parent mirror only when `parent_issue` is blank. Use `create_github_issue` with
title `[Chantier] <title>` and a body containing the slug, `Done means ...` goal,
and key refs. Before creating, search open and closed issues in the target repo
for the exact slug or `[Chantier] <title>`; link an existing exact match rather
than creating a duplicate. On success, update the same record's `parent_issue`
with `expected_version`, then attach pasted GitHub issue refs through the native
`add_github_sub_issue` tool. An existing `parent_issue` is proof the mirror was
already opened; never open it again.

The Domain record is the durable primary write. Repo ambiguity, missing GitHub
credentials/scopes, or unavailable mirror/sub-issue tooling never rolls it back
and never makes the declare fail. Degrade loudly in-thread as
`mirror pending: <specific reason>`; when the interface itself is unavailable,
the required wording is `mirror pending tooling`. Never claim a mirror exists
without the returned GitHub issue number and URL.

## Merge / Retire a Duplicate

Use the first-class `merge_chantier` tool after a human or exact evidence has
selected the canonical record. Read both records immediately before the write
and supply both `expected_*_version` values plus an audit reason. The operation:

1. merges unique typed refs from the duplicate into the canonical record;
2. sets the duplicate to `state: paused` and
   `superseded_by: <canonical slug>`;
3. reads the chantier set back and returns the active count plus the exact
   record ids eligible for the next digest.

Repeating the same merge is an idempotent `already_merged` result. A duplicate
already superseded by a different slug, an inactive canonical record, a version
mismatch, or cross-object ids fail without claiming retirement. For the known
production repair, invoke this operation on duplicate record `2096` and
canonical record `1993` only after re-reading their current versions; production
execution is deliberately separate from code deployment.

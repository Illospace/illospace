> On-demand mode playbook for the Uwear engineering triage operating model.
> Core doc: Enterprise Documentation Domain `37` record `1155` (bundled skill
> `uwear-engineering-triage`); fetch per its **On-demand Run Modes** section.
> The core doc's always-on rules — Ownership, Deploy-State Ladder, States,
> Before Posting gates, Public Output — still govern this mode.

## Backlog Seed

For a one-time backlog seed, inspect all open issues and PRs across the four
repos. Do not dump the whole list into Slack. Cluster related work, mark
low-signal stale items as `Backlog` or close candidates, and elevate only the
highest-signal work into `Todo`, `ready-for-agent`, `ready-for-human`,
`In Review`, or `Blocked`.

Before closing GitHub issues or PRs, get human approval unless the user already
delegated closure.

## Backlog Hygiene

Keep stale backlog cleanup separate from the daily priority coordinator.
Backlog hygiene has three explicit modes:

- `process-design`: define cleanup rules, state meanings, ownership rules,
  schedule, and output shape. Do not write Slack, GitHub, or Domain records.
- `no-write-audit`: inspect live GitHub and existing GitHub Ticket Tracker
  Domain 1 records, then propose classifications and owner batches. Do not
  mutate Slack, GitHub, or Domain records.
- `live-hygiene-run`: after human approval, seed or refresh generic Domain 1
  ticket/PR records and post a concise Slack summary if requested. Do not close
  or merge GitHub issues/PRs unless that exact authority was delegated.

Use the existing GitHub Ticket Tracker Domain 1 objects. Do not create
Uwear-specific backlog objects when generic ticket fields, relations, labels,
and progress notes can hold the coordination.

Classify stale work by next action using the core operating doc's **States**
vocabulary (including its deploy-verified `Done` rule for alert-linked
tickets).

For likely obsolete work, preserve the lifecycle state and add a queryable note
such as `cleanup:close-candidate` in the progress note or another existing
queryable field. Treat close candidates as approval batches; do not close them
silently.

Assign cleanup ownership by the next action, not blindly by author. Use the
original author as a clue only when they clearly hold missing context.

Slack summaries for hygiene runs should report batches and decisions, not the
entire backlog. Include scope counts, owner batches, close-candidate approval
batches, and MCP search examples such as `cleanup:close-candidate`,
`ready-for-agent`, `Reda`, `Axel`, `JB`, or `Blocked`.

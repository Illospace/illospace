# Uwear Engineering Triage — Memory Playbook
> On-demand mode playbook for Enterprise Documentation Domain `37` record
> `1275`, slug `uwear-engineering-triage-memory`.
> Core doc: Domain `37` record `1155` (bundled skill
> `uwear-engineering-triage`); fetch per its **Memory** section.

## Purpose

Memory should let a later coordinator make a better decision without
re-discovering a durable outcome. It supplements live GitHub, Domain, Slack,
and incident evidence; it never replaces them.

## Run Start — Recall on Subject

Before deciding, call `memory_reconstruct` at least once with the concrete
subjects of this run. Name stable identifiers where available: repo, issue or
PR number, person, chantier slug, Rollbar item, or incident id. One compact
query may include all closely related subjects; use a follow-up only when the
first evidence pack exposes a relevant unresolved question.

Example query: `uwear-ai/uwear-backend issue #905, Axel, incident
Uwear-API#2206: prior decisions, ownership, and incident conclusions`.

Treat the evidence pack as context. Before relying on a recalled fact to
assign, close, escalate, suppress, or post, verify it against the current
GitHub/Domain/Slack/incident source. A conflict or stale source is a reason to
say what changed, not to force the old memory onto the present.

## Run End — Select One Durable Outcome

If the run produced a durable outcome, call `memory_ingest_source` exactly
once for the single outcome most useful to future runs. If nothing crossed
that bar, do not ingest. Durable outcomes include:

- a decision or human approval that governs later action;
- an ownership change, including its scope and effective condition;
- standing guidance expected to survive the current run; or
- an incident conclusion supported strongly enough to guide future triage.

### What Makes a Good Memory

A good memory is decision-relevant to a later run, atomic, concrete about its
subject, grounded in named evidence or human authority, and expected to remain
useful after the current queue state changes. It says what governs future
action and under what condition it stops governing.

Do not ingest open counts, queue snapshots, current CI state, routine run
summaries, Slack-post receipts, poll results, or “no change” observations.
Those are live/ephemeral evidence. If several durable outcomes occur, choose
the most consequential one; their authoritative records remain the source of
truth.

Use `content_kind="decision"`, `"fact"`, or `"procedure"` as appropriate.
Set `confidence=0.9` for facts or approvals stated by a human. An inferred
conclusion is at most `0.7`; mixed human/inferred content uses the lower value.

## Stable Phrasing and Dedup

Ingestion derives `normalized_key` from the first sentence's normalized text.
Make that sentence stable and atomic so a repeated outcome resolves to the
same key:

1. Start exactly `What future runs need: <subject> <durable outcome>.`
2. Use the same canonical subject and identifiers every time (repo-qualified
   issue/PR, chantier slug, person, incident id).
3. State one outcome. Put evidence or qualifications in later sentences.
4. Never lead with a timestamp, run id, “today”, message id, or delivery
   receipt; those manufacture a new key for the same outcome.
5. When the same outcome is reaffirmed, reuse its first sentence verbatim.
   When it is superseded, name the old outcome and the replacement explicitly.

## Examples

Good — human-approved ownership change (`confidence=0.9`):

> What future runs need: Uwear-API#2206 is owned by Axel until production
> verification. Reda approved this ownership condition in the incident thread.

Good — inferred incident conclusion (`confidence=0.7`):

> What future runs need: Uwear-API#2206 most likely originates in the backend
> render path. This is inferred from matching traces and must be rechecked
> against live incident evidence before escalation.

Good — standing guidance (`confidence=0.9`):

> What future runs need: staging-to-main promotion PRs require a human merge.
> Reda confirmed this standing approval boundary.

Bad — ephemeral count:

> There are 42 open issues right now.

Bad — delivery receipt:

> Run 1842 posted the 8:00 digest to Slack.

Bad — unstable duplicate framing:

> Today we again decided Axel should own Uwear-API#2206.

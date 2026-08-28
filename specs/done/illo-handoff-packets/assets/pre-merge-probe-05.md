# Pre-merge probe evidence (illo-dev, 2026-07-13)

Run: `docker compose run --rm --no-deps -v /home/uwear/illospace-probe:/app
slack-connector python -m brain.systems.briefing --probe-triage
--since-hours 96` — real triaged ideas, real Slack/GitHub/DB env, zero side
effects (read-only, rolled back; one-off container, no live service touched).

## Sample briefs (final run, verbatim)

```
*GitHub issue #81 opened: Draft the Q3 pricing page copy* → Reda
*What happened:* GitHub issue #81 opened: Draft the Q3 pricing page copy; url: https://github.com/uwear-ai/uwear-website/issues/81; action: opened; state: open
*Evidence:* uwear-ai/uwear-website#81
*Prior decisions:* none on record
*Ask:* Pick up this business item: Inbound signal needs Illo triage: github:uwear-ai/uwear-website
Launch: {launch_url}
```

```
*GitHub comment #82 created: Remove/reframe 'free' offers across the site (#76)* → Reda
*What happened:* GitHub comment #82 created: Remove/reframe 'free' offers across the site (#76); url: https://github.com/uwear-ai/uwear-website/pull/82; action: created; state: open
*Evidence:* uwear-ai/uwear-website#82, uwear-ai/uwear-website#82:checks
*Prior decisions:* none on record
*Ask:* Pick up this other item: Inbound signal needs Illo triage: github:uwear-ai/uwear-website   ·   context trimmed: 1 excerpt shortened, 1 source degraded
Launch: {launch_url}
[notes] github: uwear-ai/uwear-website#82 body pre-compacted upstream (+292 chars)
```

```
*GitHub comment #897 created: Investigate Rollbar #2230: staging POST /post shopper_id attr* → Reda
*What happened:* GitHub comment #897 created: Investigate Rollbar #2230: staging POST /post shopper_id attribute error; url: https://github.com/uwear-ai/uwear-backend/issues/897; action: created; state: open
*Evidence:* uwear-ai/uwear-backend#897
*Prior decisions:* none on record
*Ask:* Pick up this other item: Inbound signal needs Illo triage: github:uwear-ai/uwear-backend   ·   context trimmed: 1 excerpt shortened, 1 source degraded
Launch: {launch_url}
[notes] github: uwear-ai/uwear-backend#897 body pre-compacted upstream (+870 chars)
```

`{launch_url}` is the honest placeholder — the probe creates no rows, so
there is no URL to fill.

## What the probe caught (folded as commits c1b004d + follow-up)

Run 1 exposed three real-data gaps invisible to unit fakes: bogus
"slack: provenance malformed" notes on GitHub-origin events (no thread
exists — expectation now scoped to slack-kind envelopes); GitHub reads
404ing on private repos (token discovery needs user context — the
connection's `authority_user_id` now plumbs through mint/refresh/probe);
thin briefs (the event's own summary + hints url now leads the record
section). Run 3 is the output above: real reads, real CI-check evidence,
honest upstream-compaction notes.

## Review-finding discharges (06+07 pass, findings 1 + 5)

- **Notify runner commit semantics:** every cycle tick executes inside
  `UnitOfWork` blocks (`brain/systems/cycles/service.py`), and
  `UnitOfWork.__aexit__` COMMITS on clean exit — refresh-created rows
  persist by construction. Registration note: wire the notify program the
  same way every other cycle is wired (inside UnitOfWork), and this holds.
- **Real-Postgres JSONB round trip:** `find_packet_handoffs_for_jobs` and
  `_find_idea_by_stamp` executed against illo-dev pg16 (empty results —
  correct pre-merge, no packets exist): query shapes valid on real JSONB.
- **Launch-link click:** not exercisable pre-merge (no rows minted); the
  route logic is covered by slice-04 tests and the codex redirect is
  byte-identical to production behavior today. First live packet after
  merge is the natural click-check.

Probe clone `/home/uwear/illospace-probe` was removed after the run.

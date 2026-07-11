# Slice 07 — Outcome stamps (launched/ignored, time-to-launch)

## Contract unlocked
The pillar-5 seed, coordinator edition: measure whether packets change
behavior — launched vs ignored, time from mint to launch, and terminal job
state — so the next "what's the next most valuable step?" conversation has
data instead of vibes.

## API seam
- `LaunchHandoff` already records `launch_count` / `last_launched_at` — no
  schema change for the launch side.
- Mint side: slice-05 mint stamps the record (existing JSONB details, the
  lifecycle pattern) with `packet: {handoff_id, revision, minted_at}`.
- New pure reporter `brain/systems/briefing/outcomes.py`:
  `packet_outcomes(records, handoffs, *, since) -> OutcomeSummary`
  (minted, launched, ignored>48h, median mint→launch, per-member split).
- Digest footer gains one line: `Packets: 6 minted · 4 launched · median
  22m to launch` — through the digest contract, no new section.

## What the human can run/see
`python -m brain.systems.briefing --outcomes --since 7d` against illo-dev
read env prints the summary; the digest footer shows it weekly.

## Verification
- Pure tests on the reporter with fixture rows (edge: re-launched superseded
  packets count once per job, not per revision).
- One live read-only run after a week of activation; paste the first real
  summary into `assets/outcomes-week-1.txt`.

## Stays green
Digest gates; handoff API tests.

## Feedback that would change this slice
Reda wants different outcome definitions (e.g. "ignored" threshold), or
wants outcomes stamped back onto GitHub issues too.

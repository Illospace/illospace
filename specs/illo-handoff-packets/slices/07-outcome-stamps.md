# Slice 07 — Outcome stamps (launched/ignored, time-to-launch)

## Contract unlocked
The pillar-5 seed, coordinator edition: measure whether packets change
behavior — launched vs ignored, time from mint to launch, and terminal job
state — so the next "what's the next most valuable step?" conversation has
data instead of vibes.

## API seam
- `LaunchHandoff` already records `launch_count` / `last_launched_at` — no
  schema change for the launch side. **"Launched" is defined as
  `launch_count > 0`, never by status** (supersede archives rows, and the
  claude copy-button vs codex redirect semantics from slice 04 are what
  make the count honest across targets).
- Mint side (from slice 05): stamp lives in
  `idea.agent_details["packet"]`; assignee lives in handoff
  `metadata_["owner_user_id"]` (the model has no assignee column) — the
  per-member split reads that.
- New pure reporter `brain/systems/briefing/outcomes.py`:
  `packet_outcomes(ideas, handoffs, *, since) -> OutcomeSummary`
  (minted, launched, ignored>48h, median mint→launch, per-member split).
  A supersede chain (`supersedes`/`superseded_by` metadata) counts as ONE
  job: launched if any revision launched, mint time = first revision.
- Digest footer gains one line: `Packets: 6 minted · 4 launched · median
  22m to launch` — through the digest contract, no new section. **Data
  path, explicit:** the digest is an agent-run prose contract that cannot
  import Python — expose the reporter through an existing read surface
  (extend `illo_read`/`domain.inspect`-style capability with
  `packets.outcomes`), and the digest mission calls that tool.

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

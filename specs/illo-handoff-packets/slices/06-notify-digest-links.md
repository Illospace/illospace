# Slice 06 — Notify/digest packet links + stale re-render

## Contract unlocked
The other two routing moments go warm: notify-cycle nudges and digest lines
carry the item's launch link, and packets refresh when the underlying truth
changes — continuity across sittings ("picking the thread back up").

## API seam
- `change_notifications.py` (pure): normalized event shape gains optional
  `launch_url` + `packet_revision`; line formatting appends `→ launch` when
  present. Pure tests only.
- `change_notifications_cycle.py` (wiring): before posting, for events whose
  record has a packet, look up the current handoff reference; if the
  record's truth changed since `packet_revision` (freshness events from the
  webhook path), call `mint.refresh_packet_for_record` — thin wrapper over
  the slice-05 mint with the supersede path. Env-gated by the same
  `ILLO_HANDOFF_PACKETS` value.
- Digest pipeline (doc-1155 contract, sweep→diff→post): digest lines for
  packeted items include the launch link. This is a prose+mission change
  riding the existing digest contract — keep the 8 Before-Posting gates
  intact; the packet link is one more field per line, not a new section.

## What the human can run/see
A morning digest where each actionable line ends with a launch link; a
nudge that re-links a refreshed packet after a related PR merged overnight
(deploy-state event → refresh → nudge carries new revision).

## Verification
- Pure tests: line rendering with/without launch_url; no growth in digest
  line count from this slice.
- Wiring test with fakes: freshness event → refresh called once →
  superseded handoff marked, nudge carries the new url.
- Budget check: packet refresh must not turn the notify cycle into a
  gathering storm — cap refreshes per cycle (env, default 5) with an
  explicit "deferred N refreshes" log line (no silent cap — README
  invariant).

## Stays green
Slice-4 notify tests; digest contract gates; deploy-state suite.

## Feedback that would change this slice
Refresh cadence feels too chatty/too stale in practice; digest should show
the full brief instead of just the link for top items.

# Slice 5 — Deploy State (superseded)

> Superseded by Illospace issue #473.

The original slice modeled deploy progress as tracker state updated by GitHub
webhooks and scheduled verification. That design was a cache whose
invalidation path could be delayed, skipped, or disabled, so tracker records
could claim a state that no longer matched Git history.

Deploy state is now a read-time view of the latest fix merge commit:

- contained by `main` → `deployed`;
- otherwise contained by `staging` → `staging`;
- contained by neither → `unmerged`;
- indeterminate GitHub reads → `unknown`.

The compare owner treats identical commits as contained, so a promotion with no
diff still resolves correctly. Reads are always enabled and may be batched
concurrently for briefing surfaces. No webhook mutates deploy state and no
environment switch arms the read path.

Tracker records persist only the facts ancestry cannot recover:

- canonical `fix_pr`;
- the 40-hex `fix_merge_sha`;
- `verified` and `verified_at`, representing a human or monitor judgment.

Alert-thread resolution harvesting remains an independent Slack read path. It
may update the verification judgment, but it does not claim branch containment.
An indeterminate ancestry read never inherits an earlier answer.

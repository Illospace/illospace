# Slice 03 — Gather wiring (read-only source adapters)

## Contract unlocked
Real `SourcePiece`s from live systems, all read-only: the origin Slack
thread, the owning record + related records, linked GitHub issues/PRs,
deploy-state, and backend evidence. One gatherer, used by every caller.

## API seam
`brain/systems/briefing/gather.py`:

```python
async def gather_pieces(session: AsyncSession, *, org_id: str,
                        job_ref: str,                    # record/idea key
                        slack: SlackReader | None,       # thin protocol over slack client read path
                        github: GithubReader | None,     # thin protocol over existing github handlers' read helpers
                        budget: DossierBudget) -> list[SourcePiece]: ...
```

- Adapters are **protocols with tiny default impls** delegating to existing
  owners: `brain/systems/slack/` for thread reads,
  `brain/systems/runs/tool_catalog/handlers/github.py` read helpers for
  issues/PRs, `brain/systems/deploy_state.py` for ladder state,
  `brain/systems/runs/evidence.py` records where present. No new HTTP
  clients, no new token paths — reuse the GitHub App read mint as-is.
- Related-record discovery: same-job references only (record links,
  `source_ref` provenance, tracker keys) — **no fuzzy search in v1** (that's
  a later, eval-gated idea; fuzzy relatedness is where briefs start lying).
- Each adapter enforces its per-source budget at fetch time (don't pull 10k
  messages to throw them away) AND reports true totals so omission counts
  are accurate.
- Failures degrade to an explicit `DossierSection` omission line
  ("github: unavailable — <reason>"), never a silent absence and never a
  crashed mint.

## What the human can run/see
Read-only probe against illo-dev (952-pattern):
`python -m brain.systems.briefing --live --job-ref <record-key>` from a dev
checkout with read env — prints the gathered dossier + compose output for a
real record, posts nothing.

## Verification
- Integration tests with fake readers (recorded fixtures for one Slack
  thread + one GitHub issue + deploy-state row).
- Adapter failure tests: each source down → visible omission line, mint
  still succeeds.
- Live read-only probe on illo-dev against a real triaged record; paste the
  output into the spec folder as `assets/live-probe-01.txt` for review.

## Stays green
Fast suite; zero writes anywhere (assert: no session.add/flush in gather
path).

## Feedback that would change this slice
Reda wants prod-DB (uwear generation table) or PostHog as v1 sources —
currently deferred to keep v1 inside systems Illo already reads.

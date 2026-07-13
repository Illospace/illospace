# Illo handoff packets — "no work arrives cold"

## Next Agent Prompt

**Status (2026-07-13): ALL SEVEN SLICES IMPLEMENTED.** 01–05 adversarially
reviewed and hardened (four review rounds folded: 14+9+9+10 findings);
06+07 implemented and unit-green, their adversarial pass is the next
pickup, then the illo-dev pre-merge probe (briefs into `assets/`) and the
doc-1155 delta at merge+deploy. Earlier per-slice status follows for
provenance. Slices 01+02 implemented, cross-family reviewed,
hardened, and green —
`brain/systems/briefing/{core,compose}.py` (pure), CLI probe
(`python -m brain.systems.briefing --fixture
tests/fixtures/briefing/uwear_bug.json [--compose --ask …]`), 31 unit
tests + golden snapshot; architecture-boundary gate green; full fast suite
green (only the 9 pre-existing `test_llm_worker` env failures).
Claude-implemented; **Codex cross-family review completed 2026-07-11**
(9 findings: 3 HIGH — revision hash omitted persisted repo/branch/
provenance fields; double-cut markers under-reported omitted chars;
fully-shed sections lost structured counts — all 9 folded, each with a
regression test; 51 briefing tests green). **Next pickup:
[slices/03-gather-wiring.md](slices/03-gather-wiring.md)** — Codex
implements per the routing rules, Claude directs and reviews.

**Implementation decisions that refine slice texts (01):**
- `DossierItem.truncated` (+ `omitted_chars`), not the sketch's
  `omitted: bool` — the item is present, its body was cut; "omitted" is
  reserved for dropped things.
- `assemble_dossier(pieces, *, job_ref, budget, headline=None)` — the
  sketch omitted `job_ref`/`headline` params its own `Dossier` fields
  require.
- Budgets govern content; section headers/markers/omissions footer are
  exempt bounded overhead (never trim the evidence of a cut to fit a cap);
  a lone oversized item is re-cut down to a 40-char floor, floor wins over
  cap.
- Duplicates collapsed by `(source, ref)` are not counted as omissions
  (same object, still cited once); empty refs are never deduped.

**Implementation decisions that refine slice texts (02):**
- `compose_packet` gained `org_id` / `created_by_user_id` /
  `source_surface` / `source_ref` / `acceptance_criteria` /
  `owner_user_id` params — `LaunchHandoffCreateInput` requires them; the
  sketch's signature was incomplete.
- `context_parts` = one part per item (`{source, ref, title, excerpt,
  truncated, omitted_chars}`) plus a trailing
  `{"source": "omissions", "notes": […]}` part when anything was trimmed
  (the sketch's per-part `omitted_count` was redundant).
- `BRIEF_CHAR_CAP` (1200) is a STRICT bound on the whole brief, enforced by
  a deterministic tighten cascade (narrative → ask → decisions → evidence →
  headline, each to a floor, each cut leaving a marker); the launch line and
  trimming note are never sacrificed.
- Human brief carries a literal `{launch_url}` placeholder; mint (slice 05)
  fills it via `fill_launch_url()` which replaces ONLY the final launch
  line (interpolated fields containing the placeholder text stay literal).

**Post-review hardening (Codex cross-family pass, 2026-07-11 — 9 findings folded):**
- `DossierItem.excerpt` is marker-FREE; text audiences render via
  `rendered_excerpt`. Markers are render-only (`render_marker`), never
  parsed back — brief-level re-cuts accumulate `omitted_chars` so a marker
  can never under-report the distance from the raw source.
- Fully-shed sections stay on `Dossier.sections` (empty, with
  `omitted_count`) for structured accounting; they render only through the
  omissions footer. The brief's trimming note sums structured counts —
  dropped items AND shortened excerpts — never rendered strings.
- The revision hash covers every persisted launch-affecting field
  (+ repo_origin_url, branch_hint, source_surface, source_ref). Named
  exclusions: org_id (key already org-scoped), created_by_user_id
  (audit-only; identical content re-minted by another actor SHOULD reuse),
  owner_label (display-only, derived fresh from owner_user_id). Slice 05
  must pass STABLE provenance in source_ref (origin thread, not event).
- Dedupe winner is deterministic under complete metadata ties (normalized
  body joins the ordering key); `excerpt_chars` has a 40-char floor
  (below it a cap can't hold content + an honest marker).

**Implementation decisions that refine slice texts (04, Codex-implemented):**
- Unknown targets get the launch PAGE (fallback), not the old 400 — the
  page is target-agnostic enough (copy box + codex alternate button).
- Launch marking: codex → on redirect (unchanged); claude/unknown → a new
  authed `POST /api/launch-handoffs/{id}/launched` fired by the copy
  button; page render never counts (slice-07 metric depends on this).
- The page is self-contained dark-only HTML with a strict CSP
  (`default-src 'none'`), all interpolations escaped; Constellation palette
  values from DESIGN.md. Visual gate: rendered from a fixture row and
  screenshot-reviewed 2026-07-13 (evidence:
  [assets/launch-page-render-04.html](assets/launch-page-render-04.html));
  accepted — flat, legible, primary action correct. Re-run the gate at
  activation against the live route.
- `parse_member_agent_targets` / `agent_target_for_member` live in
  `launch_handoffs.py` (uuid-keyed, validated); slice 05 consumes them.

**Implementation decisions that refine slice texts (03, Claude-implemented
after the Codex worker wedged):**
- Gather-level degradations are a FIRST-CLASS channel: `GatherResult
  {pieces, source_notes}`; `assemble_dossier(..., source_notes=…)` carries
  them onto `Dossier.source_notes` (structurally separate from budget
  omissions), the brief note adds "N sources degraded", and the agent's
  omissions context-part includes both lists.
- Privacy boundary enforced BEFORE any Slack read: the inbound event's
  envelope `channel_type` decides (`im`/`mpim`/`group` → note, no fetch);
  API errors additionally degrade to notes.
- Job resolution: `idea:<uuid>` (triage home) or `domain_record:<id>`;
  org-scope mismatch = not found. Slack provenance = idea →
  `agent_details.inbound_triage.event_id` → `InboundEventRow` payload.
- Related refs: conservative `owner/repo#N` regex over the job's own
  title/description + record tracker fields (`fix_pr`, `ticket`, …),
  deduped, capped at 4. No fuzzy search (spec rule).
- `DefaultGithubReader` reuses the tool handler's token-candidate
  resolution (the existing auth owner) + the cortex GitHub connector; no
  new token path. Issues resolve via the recent-issues window (no
  single-issue getter exists) — an old issue degrades to a "not found in
  readable window" note, honestly.
- `--live` CLI stub errors helpfully; the real pre-merge probe arrives
  with slice 05.

**Post-review hardening (Codex cross-family pass on slice 03, 2026-07-13 —
would-block, 9 findings / 6 HIGH, all folded; seam shapes now verified, not
guessed):**
- Slack provenance reads ONLY `event.envelope["payload"]` (`channel_id`,
  `thread_ts`/`message_ts`, `channel_type`) — the shape ingress actually
  writes; no top-level fallbacks. The inbound event must belong to the
  requesting org. Privacy is an ALLOWLIST: only `channel_type == "channel"`
  is excerpted; im/mpim/group/empty/unknown fail closed with a note.
- Idea, DomainRecord, AND InboundEventRow loads are org-scoped; a
  cross-org `domain_record:<id>` is "job not found", never a leak.
- GitHub reads go through a new handler-owned
  `github_read_ref_for_backend(repo_slug, number, org_id, …)` — explicit
  backend context for `_github_token_candidates` (which gained optional
  `org_id`/`user_id` params), ordered candidates preserved, PR wrapper
  flattened, exact-issue fallback via new connector `async_get_issue`
  (no recency-window hack), 404-per-candidate semantics. Sanctioned
  exception to read-only: token resolution may write vault access-audit
  rows (auth owner's behavior, documented in the helper).
- Ref discovery covers the REAL live paths: inbound event `hints`
  (github-origin), tracker `repo`+`pr_number` / `pr_url` via the
  user_domains normalizers, and explicit `owner/repo#N` text — deduped,
  capped WITH a visible note. Related tracker records are queried
  org-scoped by the same identity; PR check-runs become evidence pieces.
- Fetch honesty: connector body compaction is measured
  (`body_total_chars` additive keys) and noted per ref; Slack reads walk
  up to 3 cursor pages (client gained a `cursor` param) so thread tails
  are reachable, with the only-N-of-M note covering the rest.
- DB reads run under `no_autoflush` so gather can never flush a caller's
  pending state (the review demonstrated a real-session INSERT without it).
- Evidence pieces from idea attribution; `slack`/`github` reader absent →
  explicit "no reader configured" notes. 24 gather tests mirror the real
  seam shapes (org scoping, fail-closed surfaces, cap/compaction notes,
  cursor pagination, backend-read flattening, revision rotation on
  source_notes).

**Implementation decisions that refine slice texts (05, Claude-implemented
after the Codex worker wedged again — 0.15s CPU in 25min):**
- Orchestrator: `brain/systems/briefing/mint.py`. Hook = two contained
  lines at the end of `reconcile_inbound_triage_run` (COMPLETED
  `illo_triage` receipts only); the idea resolves via
  `Idea.origin_ref == "inbound_event:<id>"`, org-scoped.
- `create_launch_handoff_with_status(...) -> (row, created)` is the new
  service primitive (create delegates to it); `created=False` is the noise
  gate — reuse posts NOTHING.
- Supersede: prior row found via the idea's packet stamp → `archived` +
  `metadata_["superseded_by"]`, new row `metadata_["supersedes"]`.
  Reuse-with-drift (same key, different content = revision-hash gap) is
  repaired IN PLACE with a warning, never duplicated.
- Race: nested transaction; IntegrityError on the unique key → re-select
  as reused. Total containment: `mint_packet_after_triage` never raises.
- Ask = deterministic template (`task_domain` + envelope summary);
  `job_ref` from attribution `domain_record` refs, idea-id fallback;
  owner label via a `_fill_owner_labels`-style best-effort User lookup;
  Slack reply reuses gather's provenance + public-only allowlist via
  `slack/client.post_message` (thread_ts).
- `repo_origin_url` hint derived from the dossier's first GitHub ref.
- Pre-merge probe: `python -m brain.systems.briefing --probe-triage
  [--since-hours H]` — walks recent inbound-triaged ideas through the
  shared `build_packet_for_job` stage (gather→assemble→compose), prints
  briefs + notes, creates and posts nothing, rolls back. Fails helpfully
  without the dev/read env. 15 mint tests cover the noise gate, supersede
  vocabulary, containment, non-public no-post, unclaimed packets, target
  map, and probe-stage purity.

**Post-review hardening (fresh-context Claude adversarial pass on slice 05,
2026-07-13 — would-block, 10 findings / 3 HIGH, all folded):**
- **Self-echo starved twice over.** (1) Gather filters Illo's OWN Slack
  messages via the provenance `bot_user_id` (ingress always resolves it) —
  the posted brief can never feed the next gather and rotate the revision.
  (2) The reconcile hook fires only on the transition INTO a terminal
  receipt state (`receipt_was_terminal` captured pre-mutation) — result
  polls (`illo_get_result` re-runs reconcile on every read!) never
  re-gather, never re-mint, never re-post.
- **Spec-pinned serialization implemented for real**: the write phase
  re-selects the idea `WITH FOR UPDATE` before reading the packet stamp —
  concurrent minters queue instead of cross-superseding.
- **Commit-time containment**: create + supersede + drift-repair + stamp
  all run inside ONE savepoint; a DB failure anywhere rolls back to the
  savepoint and the triage receipt commits untouched. The IntegrityError
  loser re-stamps under a fresh savepoint. The reconcile-side import+call
  is additionally try-wrapped (import-chain breaks can't kill the loop).
- Owner-label lookup degrades to the RAW ID, never to "unclaimed".
- Gather's provenance helpers promoted to public names
  (`load_inbound_event`, `slack_provenance`, `PUBLIC_CHANNEL_TYPE`) — mint
  consumes the single owner, no underscore imports.
- Known deferred (recorded, review findings 4/5): the Slack reply still
  precedes the outer commit (tiny 404-launch-link window if the
  transaction later fails — revisit with slice 06's refresh cycle, which
  is the natural post-commit home), and hook-level integration tests need
  a real session (covered instead by mint-level regressions: echo
  starvation, IntegrityError re-select, lock assertion, label fallback —
  plus the illo-dev probe before merge).

**Implementation decisions that refine slice texts (06+07, Claude-implemented):**
- Slice 06: `format_line` appends `→ launch: <url>` (one field, Slack-
  escaped, no new section). The cycle's `_attach_and_refresh_packets` maps
  events to packets via `LaunchHandoff.metadata_["job_ref"]`
  (`find_packet_handoff_for_job`) — the one queryable event→packet link —
  attaches url+revision, and refreshes at most 5 unique jobs per tick
  (code constant; deferrals logged, never silent). Fully contained: any
  failure means lines without links, never a dead tick.
- `refresh_packet_for_job` (mint.py) NEVER posts to Slack (only the triage
  moment posts; nudges/digest lines carry the link). It refreshes ONLY
  packet-minted rows (`source_surface == "inbound_triage"`) and reuses the
  row's original ask/owner/target/provenance so the revision reflects
  TRUTH changes only. Idea-less jobs supersede through the refreshed row
  itself.
- Slice 07: pure reporter `briefing/outcomes.py` — launched means
  `launch_count > 0` (never status), supersede chains collapse to one job
  (first-mint time, any-revision launch, newest-revision owner), `ignored`
  needs the 48h horizon (younger unlaunched = `pending`). Surfaces:
  `packets.outcomes` read capability (agent_mcp — the doc-1155 digest run
  and team agents call it; returns summary + ready `digest_line`) and the
  `--outcomes` CLI. The digest-footer prose lives in the doc-1155 delta at
  merge+deploy.

You are implementing the coordinator upgrade: at every routing moment
(triage assignment, notify nudge, digest line) Illo attaches a **handoff
packet** — a short human brief plus an agent-ready launch handoff — so work
never reaches Reda/Axel/JB (or their local coding agents) without gathered
context. Illo **coordinates; it never executes**. If a slice tempts you to
make Illo write code, open PRs, or run jobs, stop — that direction was
explicitly rejected (see Direction below).

- Work slices in order; each leaves a runnable artifact + green tests before
  the next depends on it.
- Slices 01–04 are pure/additive and safe to build blind. Slices 05–07 wire
  live moments with **no runtime gates** (Reda, 2026-07-13): merge = live.
  Verification is pre-merge — tests plus the read-only illo-dev probe whose
  sample briefs land in `assets/` on the PR.
- Before ending your pass: update this section (status, next pickup, TODO),
  and run a cross-family review (`codex exec` with an explicit review prompt —
  NOT `codex review`, it can recurse and self-kill) on your diff.

### Global TODO
- [x] Slice 01 — dossier core (pure assembly + budgets + truncation honesty)
- [x] Slice 02 — packet composer (dual-audience render + idempotency)
- [x] Slice 03 — gather wiring (read-only source adapters)
- [x] Slice 04 — claude launch target (launch page; codex redirect preserved)
- [x] Slice 05 — triage-moment minting (ungated; pre-merge probe)
- [x] Slice 06 — notify/digest packet links + stale re-render
- [x] Slice 07 — outcome stamps (launched/ignored, time-to-launch)
- [ ] Doc-1155 delta applied at merge+deploy (text lives in slice 05/06 files)
- [ ] Pre-merge verification: read-only illo-dev probe over real triaged
      records; sample briefs into `assets/` (owns the old "dry run" role —
      there are NO runtime gates; merge = live). Must ALSO discharge the
      06+07 review's findings 1+5: (a) confirm the notify cycle runner
      COMMITS the tick's session (else refresh-created launch links 404 —
      check how the deploy-verification writes persist today), (b) exercise
      one real find→refresh round trip against Postgres (the JSONB astext
      queries have no fake coverage), (c) click one posted launch link.

## Direction (do not re-litigate)

Reda (2026-07-10): Illo is a **coordinator, not an executor**. Reda, Axel,
and JB each run their own agentic harnesses (Claude Code, Codex); Illo's job
is coordination, moving work, and integration — gathering context and
preparing work for the humans *and their agents* to execute. The earlier
"Illo opens draft PRs" direction was rejected. Reference: Sierra's Pinecone
article (x.com/neilrahilly/status/2075290325757608148) read through the
coordinator lens — the sentence this feature implements is "less work
arriving unfinished": *work never arrives cold*.

## What already exists (measured, 2026-07-10)

The packet atom shipped in PRs #199/#200 ("Add Codex launch handoffs") and is
live but **opt-in and codex-only**; nothing mints it at routing moments:

- `brain/platform/db/models/launch_handoff.py` + alembic `0017` —
  `LaunchHandoff`: title, instructions, summary, `context_parts` (ordered,
  agent-fetchable), `acceptance_criteria`, `repo_origin_url`, `branch_hint`,
  `target_tool` (enum currently `codex`), provenance (`source_surface`,
  `source_ref`), `idempotency_key`, `status` open→launched, `launch_count`,
  `last_launched_at/by`.
- `brain/systems/launch_handoffs.py` — service + `codex_prompt_for_handoff()`
  (starter prompt telling the agent to `illo_read` capability `handoff.get`)
  + `codex_deep_link_for_handoff()` (`codex://threads/new?prompt=…`).
- `brain/app/api/routers/launch_handoffs.py` — POST + GET +
  `/api/launch-handoffs/{id}/launch` HTTPS redirect (surface-agnostic link,
  postable in Slack).
- `brain/app/api/routers/agent_mcp_handoffs.py` — `handoff.get` read for
  teammates' agents via the Illo MCP (`tools/illo-personal-agent-mcp`).
- Tool catalog: `create_launch_handoff`
  (`brain/systems/runs/tool_catalog/definitions/cortex_thread.py:524`,
  handler `handlers/launch_handoffs.py`), gated by capability key
  `launch_handoffs` (`brain/systems/runs/capabilities.py:~286`).
- Frontend: `ThreadLinkPreviewCard.svelte` / `ObjectReferencePreviewList.svelte`
  render handoff reference cards in Cortex.

Coordination seams the packets plug into (lifecycle overhaul, all shipped):

- Assignment: `brain/systems/inbound/assignment.py` (rule → connection →
  unclaimed pool; pure).
- Notify loop: `brain/systems/change_notifications.py` (pure decide) +
  `change_notifications_cycle.py` (wiring; injectable `post`; posts via
  `brain/systems/slack/client.py` `post_message`).
- Slack posting: from BACKEND hooks use `brain/systems/slack/client.py`
  `post_message(channel, thread_ts=…)` with the origin provenance stored on
  the idea (the `post_slack_reply` TOOL resolves its target from in-run
  trigger context — `tool_catalog/handlers/slack.py` — and is only usable
  inside a live run, e.g. the on-demand "brief me" flow).
- Deploy state: `brain/systems/deploy_state*.py`.
- Evidence ledger: `brain/systems/runs/evidence.py` — "records what the
  backend actually saw" (compact, JSON-safe, hash-stamped).
- Tool-call attribution for completed triage runs:
  `brain/systems/inbound/attribution.py`.

## Slice graph

```
01 dossier core (pure)  ──►  02 packet composer (pure)  ──►  05 triage minting ──► 07 outcomes
        │                          │                              ▲
        └──►  03 gather wiring ────┘                              │
                                   04 claude target ──────────────┤
                                                                  06 notify/digest links
```

01→02 are pure and test-first. 03 feeds real data into 01's types. 04 is
independent of 03. 05 needs 01+02+03 (04 recommended first so Reda's Claude
harness is a valid target on day one). 06 builds on 05's mint path. 07 rides
everything.

## Contracts & invariants (refactor-clean: one owner per concept)

- **Job truth has one owner: the existing domain records / ideas.** A
  `Dossier` is a *rendered view* assembled at mint time — it is **never a
  second persistent store**. No new dossier table. Accretion over time is
  what webhooks/tracker already do to records; packets re-render from truth.
- **Launch snapshot has one owner: `LaunchHandoff`.** Packets do not add a
  parallel packet table; a packet = one human-brief rendering + one
  `LaunchHandoff` row. Re-issue via `idempotency_key` + revision metadata,
  superseding, not duplicating. Supersede reuses the EXISTING status
  vocabulary — old row → `archived` + `metadata_["superseded_by"] = <new id>`
  (the DB CHECK constraint allows only `open/launched/claimed/expired/
  archived`, `brain/contracts/statuses.py` + migration 0017 — do NOT invent
  a `superseded` status; no migration in this feature).
- **Privacy boundary:** packets widen who can read gathered context —
  `handoff.get` and the API are org-scoped, so anything gathered becomes
  readable by every org member/agent token. V1 gathers only team-visible
  Slack channels; private-channel/DM sources degrade to an explicit
  omission marker, never excerpted content.
- **Gathering has one owner: `brain/systems/briefing/`** (new). Triage,
  notify, digest, and on-demand "brief me" all call the same assembler.
  If any caller grows its own context-collection logic, that's drift.
- **Truncation honesty (the run-1057 lesson, PR #295):** every source read
  declares an explicit byte/item budget; anything omitted is represented by
  a visible marker (`… +N older messages omitted`) in BOTH audiences'
  renders. A silent `[:2000]` anywhere in this feature is a review-blocking
  bug.
- **Coordinator boundary:** packets contain instructions *for the assignee's
  agent*; no slice gives Illo write access to code, PRs, or execution
  runtimes. GitHub scope stays read-mostly (`issues:write` only).
- **No feature gates (Reda, 2026-07-13 — supersedes the earlier env-gated
  design):** merging IS the activation; there is no `ILLO_HANDOFF_PACKETS`
  flag, no dry/live mode switch, no dormant wiring. Verification happens ON
  THE BRANCH before merge: unit/integration tests plus a read-only illo-dev
  probe (952-pattern) whose sample briefs are pasted into `assets/` for
  review. Rollback = revert + redeploy. Env vars are config-with-safe-
  defaults only (`ILLO_MEMBER_AGENT_TARGETS`, unset → codex for everyone);
  tunables are code constants.
- **Short-lived seams:** none planned. If an implementation pass introduces
  one, name it here with its removal slice.

## Verification map

- Pure cores (01, 02): unit tests + golden fixtures (`tests/`), JSON
  snapshot of a fixture dossier + both packet renders.
- Wiring (03, 05, 06): integration tests with fakes; then the read-only
  illo-dev pre-merge probe (documented per slice) — no runtime gates.
- 04: route tests + a manual click-through from Slack on both targets.
- Visual surfaces: only slice 04's launch page and (optionally) the Cortex
  card. Any changed shot gets an unprimed screenshot-critique pass
  (`screenshot-critique` skill) as its last check; compare against the
  existing handoff card as the prior look via `compare-screenshots` when the
  card changes.
- Human checkpoints are **non-blocking**: open evidence, give ~5 min, then
  decide on the evidence and record the decision here.

## Known unknowns (deliberately deferred)

- Auto-refresh cadence for stale packets (v1: re-render on freshness events
  + nudges only).
- Packet quality evals (which briefs actually get launched?) — slice 07
  collects the data first.
- MCP-native pull as the *primary* interface (skip pasted prompts entirely)
  — `handoff.get` already exists; revisit after adoption data.
- `expires_at` policy for stale launch links.

## Drafting provenance

Single-pass draft by the main session (2026-07-10) after the user cancelled
the three-drafter fan-out; grounded in live recon of the modules named above
plus the lifecycle-spec history. Hardening: an independent fresh-context
Claude review (2026-07-10, Codex window was exhausted) verified every cited
module against code and returned 14 findings (2 HIGH: the invented
`superseded` status vs the DB CHECK constraint; dossier-only revision hash
allowing brief/handoff divergence) — all folded into the slice texts.
Pending: a cross-family `codex exec` re-review when the window resets;
treat its findings as spec-blocking before slice 03+.

# Illo handoff packets — "no work arrives cold"

## Next Agent Prompt

**Status (2026-07-11):** Slices 01+02 implemented, cross-family reviewed,
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
  live moments and follow the repo's activation pattern: **additive, not
  auto-registered, env-gated** (see the lifecycle spec's activation checklist
  style in `specs/illo-lifecycle/README.md`).
- Before ending your pass: update this section (status, next pickup, TODO),
  and run a cross-family review (`codex exec` with an explicit review prompt —
  NOT `codex review`, it can recurse and self-kill) on your diff.

### Global TODO
- [x] Slice 01 — dossier core (pure assembly + budgets + truncation honesty)
- [x] Slice 02 — packet composer (dual-audience render + idempotency)
- [ ] Slice 03 — gather wiring (read-only source adapters)
- [ ] Slice 04 — claude launch target (launch page; codex redirect preserved)
- [ ] Slice 05 — triage-moment minting (env-gated)
- [ ] Slice 06 — notify/digest packet links + stale re-render
- [ ] Slice 07 — outcome stamps (launched/ignored, time-to-launch)
- [ ] Doc-1155 delta applied at activation (text lives in slice 05/06 files)
- [ ] Live activation on illo-dev (gates listed per slice; read-only dry run first)

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
- **Additive activation:** new cycle/hook wiring ships dormant
  (env-gated, not auto-registered), activated deliberately on illo-dev —
  same pattern as lifecycle slices. Read-only dry run (952/1088 pattern)
  before any live posting.
- **Short-lived seams:** none planned. If an implementation pass introduces
  one, name it here with its removal slice.

## Verification map

- Pure cores (01, 02): unit tests + golden fixtures (`tests/`), JSON
  snapshot of a fixture dossier + both packet renders.
- Wiring (03, 05, 06): integration tests with fakes; then read-only
  illo-dev dry-run gates before activation (documented per slice).
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

# PRD: Universal Thread Context Ingress

Status: thin vertical slice implemented in this branch; still a living PRD
Date: 2026-05-21
Owner: product/architecture discussion
Related docs:

- `docs/prd-inbound-coordination-layer.md`
- `docs/mcp-personal-tool-signals.md`
- `docs/personal-agent-connections-mvp.md`

## Problem Statement

Illospace is becoming the bridge between a person's private AI agents and the
team workspace where shared judgment happens.

Today, a user can work with a personal agent such as Codex, and that agent can
submit small progress signals to Illo through MCP. That proves the transport and
inbound coordination foundation, but it does not yet express the larger product
shape: a personal agent should be able to hand rich context to Illo, the team
agent, and let Illo decide what durable workspace object should exist.

The motivating example is:

1. Reda works with Codex.
2. Reda reaches a moment where team context or team judgment matters and says
   something like "let's ask the team."
3. Codex sends Illo the relevant context. This may include the full user/agent
   conversation, tool trace, files, links, prompt history, or other artifacts.
4. Illo receives the context as the team agent. Illo records it through the
   inbound coordination layer and decides how it belongs in the workspace.
5. When a Thread is created or updated, Codex gets back a link so Reda can jump
   into Illospace.
6. The existing Thread stage renders the AI work. A right-panel Discussion lets
   teammates comment on the Thread without turning Illospace into another Slack.
7. If the team needs Illo, they summon it with `@illo`. Illo's substantive work
   continues in the main Thread, not inside the comment surface.

The product gap is not a missing special case called "decision request." The
gap is a universal context ingress contract and a Universal Thread model broad
enough to hold AI traces, Illo runs, imported context, artifacts, discussion,
and outcomes without forcing every submission into a narrow workflow.

## Solution

Introduce **Universal Thread** as the product-facing collaboration object and
introduce `illo_submit_context` as the canonical personal-agent-to-Illo ingress
primitive.

The relationship model is:

- A personal agent acts for an individual user.
- Illo acts as the team agent for the user's Illospace team.
- A personal agent sends context to Illo. It does not own team routing,
  workspace placement, team notifications, or durable collaboration state.
- Illo receives the context through the existing inbound coordination layer and
  decides what durable workspace object, if any, should exist.
- The frontend renders persisted Thread state generically. It does not encode
  workflow policy.

The two high-level MCP primitives are:

- `illo_ask`: private query to Illo for information Illo already has or can
  reason over. This is not team-visible by default and may run asynchronously
  behind `illo_get_ask`.
- `illo_submit_context`: universal ingress for new context from a personal
  agent to Illo. It acknowledges receipt quickly, records the context through
  inbound coordination, and returns any immediately available Thread URL.

Universal Thread product vocabulary:

- **Thread**: the canonical durable object for human/AI/team collaboration.
  "Idea" is legacy/internal naming that may remain in the database and APIs
  during the MVP.
- **AI Timeline**: the main existing Thread conversation/stage. It contains
  Illo runs, AI messages, imported trace previews, tool/activity entries,
  artifacts, and generated outputs.
- **Discussion**: a right-panel comment surface attached to the Thread. It is
  scoped, human-first, and not a Slack replacement. Illo may comment there, but
  AI work happens in the AI Timeline.
- **Context**: imported external context parts such as raw Codex traces,
  prompt/conversation excerpts, files, links, screenshots, tool logs, diffs, or
  structured JSON.
- **Outcome**: optional durable result/status/decision marker when the Thread
  reaches one.

MVP proof loop:

```text
personal agent submits context
-> inbound coordination records/routes it
-> Illo may create or update a Thread
-> existing Thread UI renders submitted context
-> Discussion exists as an attached comment surface
-> @illo from Discussion can continue work in the main Thread
```

MVP scope is governed by this proof loop. Anything not required to prove the
loop is out of scope unless explicitly pulled in.

## User Stories

1. As a user working with Codex, I want to send my current agent conversation to
   Illo, so that my team can inspect the exact context instead of a lossy
   summary.
2. As a user working with Codex, I want Codex to receive an Illo Thread link
   after submission, so that I can jump into the team workspace.
3. As a teammate, I want to open the Illo Thread and see the submitted context,
   so that I understand what the personal agent was doing.
4. As a teammate, I want to inspect the raw imported context when needed, so
   that I can debug prompt drift, bad assumptions, or confusing agent behavior.
5. As a teammate, I want a compact preview of imported context by default, so
   that a full trace does not overwhelm the Thread.
6. As a teammate, I want to discuss an imported AI thread in a right-panel
   Discussion, so that comments stay attached to the relevant AI work.
7. As a teammate, I want Discussion to feel like a comment section, so that
   Illospace does not compete with Slack as a general chat tool.
8. As a teammate, I want to summon Illo with `@illo` from Discussion, so that
   Illo only participates when explicitly needed.
9. As a teammate, I want Illo's substantive work to appear in the main Thread,
   so that AI work remains in the AI Timeline rather than buried in comments.
10. As Illo, I want a tool to inspect Discussion when useful, so that I can read
    team comments intentionally without polluting every run prompt.
11. As Illo, I want submitted context to arrive through the existing inbound
    coordination layer, so that webhooks, MCP, and personal agents share one
    routing foundation.
12. As a personal agent, I want one universal context submission tool, so that I
    do not need to choose among many scenario-specific Illo tools.
13. As a personal agent, I want the MCP tool description to explain my
    relationship to Illo, so that I know I submit context while Illo coordinates
    the team workspace.
14. As a personal agent, I want to include ordered context parts, so that I can
    send text, JSON, links, artifacts, traces, diffs, or files without inventing
    a new tool for each type.
15. As a personal agent, I want `illo_submit_context` to acknowledge quickly, so
    that long Illo reasoning does not risk MCP timeout failures.
16. As a personal agent, I want `illo_ask` to remain a private query primitive,
    so that I can ask Illo for existing workspace knowledge without creating a
    visible Thread.
17. As a user, I want submitted context to be immutable once recorded, so that
    the team can trust the original source material.
18. As a user, I want Illo to generate summaries or previews as derived views,
    so that raw context remains available while the default view stays readable.
19. As a user, I want multiple context submissions to be attachable to the same
    Thread, so that follow-up Codex sessions or later traces can stay with the
    same shared work.
20. As a user, I want a submitted context result to say what happened in plain
    terms, so that Codex can report whether Illo accepted, created, or attached
    the context.
21. As a product builder, I want Universal Thread rendering to be source-agnostic,
    so that Codex, Claude Code, OpenClaw, Hermes, and future agents can all use
    the same Thread surface.
22. As a product builder, I want the MVP to reuse existing Thread infrastructure,
    so that we can learn from usage before redesigning the whole page.
23. As a product builder, I want "Thread" to become the user-facing language, so
    that legacy "idea" terminology does not leak into the product direction.
24. As an operator, I want every submitted context envelope recorded durably, so
    that routing decisions, idempotency, and debugging remain inspectable.
25. As an operator, I want context submissions without a Thread to remain inbound
    events, so that the Thread model is not polluted by source events that Illo
    chose not to surface.
26. As a future Slack integration user, I want Slack previews to link back to the
    Illo Thread, so that Slack distributes awareness without becoming the source
    of collaboration truth.
27. As a teammate, I want Thread Discussion notifications to be scoped and
    intentional, so that only relevant mentions, subscriptions, or activity
    produce noise.
28. As Illo, I want `@illo this is what we decided, carry on` to create a
    Thread-linked run, so that team decisions can steer the main AI work.
29. As a user, I want Codex to send full context when I ask, so that Illo and the
    team are not forced to rely on a summary written by the same agent that may
    be confused.
30. As a product builder, I want the frontend to render persisted state, so that
    routing policy stays in the inbound/backend layer rather than scattered
    through UI conditionals.

## Implementation Decisions

- Use the existing inbound coordination layer as the routing boundary. This PRD
  is an evolution from inbound signals to inbound context, not a second routing
  system.
- Add or rename the canonical MCP ingress tool to `illo_submit_context`. Because
  the product has not launched, there is no compatibility requirement to keep
  `illo_submit_signal` as the primary name.
- Keep `illo_ask` as a separate private query primitive. `illo_submit_context`
  is for new context entering Illo; `illo_ask` is for retrieving or reasoning
  over context Illo already has.
- Define the context envelope as generic and source-agnostic:
  - `intent`: natural-language reason the personal agent is submitting context.
  - `parts`: ordered context parts.
  - `source`: provenance about the personal agent, user, session, model, repo,
    branch, or external origin.
  - `constraints`: optional boundaries such as privacy, urgency, visibility, or
    notification preferences.
  - `correlation`: optional existing Thread URL/id, external session id, or
    previous submission reference.
  - `idempotency_key`: optional stable dedupe key.
- Treat part types as content types, not product workflows. Examples include
  `text`, `json`, `link`, `file`, `trace`, `conversation`, `diff`,
  `screenshot`, and `artifact`.
- Extend inbound envelope handling to accept a new kind such as `context`. The
  existing inbound event table and decision receipt table remain the audit and
  result spine.
- Add routing results that can describe Thread outcomes generically, such as
  `thread.created`, `thread.attached`, `accepted`, `stored`, or
  `needs_clarification`.
- Keep `illo_submit_context` async-safe. The tool should acknowledge quickly,
  return any immediately available `thread_id` and `url`, and let longer Illo
  reasoning continue through normal Thread/AgentRun state.
- Reuse the existing `ideas` table as the physical Thread table for MVP. "Idea"
  remains a legacy storage/API name until a later rename.
- Introduce a Thread read/domain model that presents `ideas` as Threads to new
  product surfaces, docs, and MCP descriptions.
- Add a `thread_context_submissions` table for immutable submitted context that
  attaches to a Thread when Illo creates or selects one.
- Store submitted context parts as JSONB on `thread_context_submissions` for MVP.
  Normalize into child rows only after real querying/rendering needs appear.
- Suggested `thread_context_submissions` fields:
  - `id`
  - `thread_id`
  - `org_id`
  - `source_connection_id`
  - `submitted_by_user_id`
  - `inbound_event_id`
  - `intent`
  - `source`
  - `constraints`
  - `correlation`
  - `parts`
  - `routing_result`
  - `created_at`
- Persist every context submission first as an inbound event. If it creates or
  attaches to a Thread, also persist it as a `thread_context_submissions` row.
  If no Thread exists, keep it as an inbound event/source record only.
- Preserve raw submitted trace/context as the canonical source artifact. Any
  summaries, previews, extracted decisions, or timeline cards are derived views.
- Allow multiple context submissions to attach to the same Thread when
  correlation is explicit or Illo confidently routes them there.
- Do not inject Discussion into every Illo run prompt. Discussion is available
  through an explicit Illo tool when needed.
- Include the triggering `@illo` Discussion comment as the trigger when a user
  summons Illo from Discussion.
- Add an Illo-visible tool for reading Thread Discussion. The tool should make
  scope explicit, for example latest messages, messages since a timestamp, full
  discussion, or messages mentioning Illo.
- Keep Illo's substantive continuation in the main Thread/AI Timeline. Discussion
  may receive small comments or acknowledgements, but the AI work belongs in the
  Thread.
- Keep the existing Thread stage as the MVP frontend surface. Add Discussion as
  a possible right-panel tab/surface.
- Render submitted context as generic preview blocks in the existing Thread
  timeline. Start with compact expandable previews; do not try to perfect raw
  trace visualization before usage teaches the right shape.
- Make Universal Thread rendering source-agnostic. Source labels and provenance
  are metadata, not separate UI architectures.
- Treat Slack as a distribution surface later. A Slack unfurl should preview and
  link to the Illo Thread, not mirror the Discussion as a competing chat.
- Update product language in new docs, tool descriptions, and frontend copy to
  use Thread, AI Timeline, Discussion, Context, and Outcome. Existing code may
  continue to say idea during the transition.

## Implementation Notes From First Slice

This implementation pass delivered the first thin vertical slice of the proof
loop and left a few product/technical decisions explicit for future review:

- `illo_submit_context` replaces `illo_submit_signal` in hosted and local MCP
  tool catalogs. No compatibility alias was kept because the product has not
  launched and the canonical primitive should not carry the old "signal"
  framing.
- The hosted MCP tool still uses the existing `signal:submit` token scope for
  this slice. That avoids bridge-token churn and keeps the authorization path
  small, but the scope name is now legacy implementation debt. A later cleanup
  should rename or alias it to a context-oriented scope once token migration is
  worth paying for.
- Context ingress is deterministic in the MVP hot path. If the caller supplies
  `correlation.thread_id` or `correlation.idea_id`, the context attaches to that
  Thread when it belongs to the user's org. Otherwise, when the source has an
  owner user, IlloSpace creates a new Thread immediately and returns its link.
  If there is no owner user, the context remains a stored inbound event. This
  keeps MCP acknowledgement quick and avoids a long Illo routing run inside the
  tool call.
- The deterministic context path still enters through
  `submit_inbound_envelope`; it is not a parallel ingress stack. Context simply
  gets its own envelope normalization and routing branch before the older signal
  source-policy flow.
- `thread_context_submissions` now stores immutable submitted context rows
  attached to Threads when a Thread exists. Parts remain JSONB for now, matching
  the PRD's bias toward preserving raw submitted material before optimizing for
  querying every possible trace shape.
- Context previews in the AI Timeline are compact `IdeaThread` messages tagged
  as context submissions. The rich raw-trace inspector is intentionally not
  solved yet; the durable submission row is the source of truth for expanding
  that UI later.
- Discussion is implemented as `thread_discussion_comments`, a dedicated
  Thread-attached comment table, not the existing general chat/room system. This
  keeps Discussion closer to a comment section and avoids polluting team chat
  with per-Thread collaboration surfaces.
- Discussion is available as a right-panel tab on the existing Thread stage.
  The backend broadcasts a `thread_discussion_comment` websocket event, but the
  first frontend pass reloads Discussion on panel open and after posting rather
  than subscribing live. Live subscription is a straightforward follow-up, not a
  requirement for proving the loop.
- `@illo` in Discussion routes through the existing Cortex notify/run path and
  links the run to the main Thread. Discussion comments do not automatically
  enter every Illo prompt. The existing mention classifier invokes Illo for
  unmentioned main Thread replies, so Discussion explicitly disables that
  default and requires an actual Illo mention.
- Illo now has an explicit `read_thread_discussion` tool. This preserves the
  product boundary that Discussion can be inspected when useful without becoming
  ambient prompt context for every run.
- Slack previews, a full ideas-to-threads backend rename, a new Thread page, and
  detailed raw-trace visualization were left out under the positive proof-loop
  scope rule.

## Testing Decisions And Coverage

Good tests should validate external behavior and durable contracts rather than
implementation details. The important behaviors are:

- Context envelopes are accepted, normalized, stored, and idempotently replayed.
- `illo_submit_context` uses the same inbound coordination service as webhooks
  and hosted MCP.
- Inbound events preserve source actor, authority user, ingress metadata,
  envelope kind, intent, source, constraints, correlation, and parts.
- Routing can create a Thread and return a simple URL/id result.
- Routing can attach a second context submission to an existing Thread.
- Routing can store a context submission without a Thread while preserving the
  inbound event/receipt.
- Raw submitted context remains available after derived previews are generated.
- Discussion comments do not automatically trigger Illo.
- `@illo` in Discussion triggers an Illo run linked to the Thread.
- Illo can explicitly inspect Discussion through a tool.
- Illo continuation appears in the main Thread/AI Timeline.
- The existing Thread UI continues to open, stream, reply, and render normal
  Illo messages after context preview blocks are added.

Recommended test modules:

- Inbound service tests for `kind=context` normalization, idempotency, receipts,
  and routing results.
- Hosted MCP route tests for `illo_submit_context` schema, scope checks, and
  quick acknowledgement shape.
- Context submission persistence tests for immutable JSONB envelope storage.
- Thread service/read-model tests for rendering context submission previews.
- Discussion API/service tests for right-panel comment behavior and mention
  triggering.
- Agent tool tests for explicit Discussion inspection.
- Frontend component tests or Playwright smoke tests for opening an existing
  Thread with a submitted context preview and Discussion panel available.

Coverage added in this implementation pass:

- Inbound tests for `kind=context` thread creation, context submission
  persistence, compact timeline preview creation, explicit Thread correlation,
  and idempotent replay.
- Hosted MCP route tests for the `illo_submit_context` tool schema, shared
  envelope construction, commit/rollback boundaries, scope checks, and rejection
  of direct workspace target fields outside `correlation`.
- Local MCP package tests for the `illo_submit_context` catalog and client
  payload.
- Model metadata coverage for `thread_context_submissions` and
  `thread_discussion_comments`.
- Discussion API coverage for normal comments not triggering Illo, explicit
  `@illo` comments triggering the main Thread run path, and commit-before-
  broadcast ordering.
- Frontend typecheck coverage for the Discussion tab/pane and context timeline
  tag changes.

Useful prior art in the codebase:

- Existing inbound webhook/MCP tests for `submit_inbound_envelope`.
- Existing external agent route tests for hosted MCP and `illo_ask`.
- Existing Cortex Thread tests for opening, posting, notifying, and run history.
- Existing chat tests for threaded room comments, mentions, and notifications.
- Existing trace export tests for bounded trace projections.

## MVP Scope Principle

The MVP is a thin vertical foundation for the universal context-to-Thread loop.
Anything that does not directly support that loop is out of scope unless
explicitly pulled in.

The loop is:

```text
personal agent submits context
-> inbound coordination records/routes it
-> Illo may create or update a Thread
-> existing Thread UI renders submitted context
-> Discussion exists as an attached comment surface
-> @illo from Discussion can continue work in the main Thread
```

Examples of work excluded by this principle include, but are not limited to:

- full backend rename from ideas to threads;
- a brand-new Thread page layout;
- Slack replacement behavior or general-purpose team chat expansion;
- hardcoded decision-request workflows;
- hardcoded Codex-only routing or UI behavior beyond source/provenance labels;
- automatic Illo participation in Discussion without an explicit summon;
- perfect visualization for every possible raw trace shape;
- complex personal-agent polling/handoff contracts beyond quick acknowledgement
  and optional Thread URL;
- many special-case MCP tools for individual submission scenarios.

These examples are not a closed list. The positive proof loop is the scope
boundary.

## Further Notes

This PRD deliberately narrows product mechanics while broadening the product
primitive. The important abstraction is not a larger list of situations where
Codex may write to Illo. The important abstraction is universal context ingress:
a personal agent submits ordered context parts, provenance, intent, constraints,
and correlation to Illo; Illo coordinates that context inside the team
workspace.

The current inbound coordination system already contains much of the necessary
architecture: event storage, idempotency, connection identity, policy matching,
decision receipts, Illo triage, and source-card observability. This PRD should
be implemented by evolving that system, not bypassing it.

The product language should move steadily toward Thread. The legacy "idea"
language was an older internal name and should not define the future user
mental model.

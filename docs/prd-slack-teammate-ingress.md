# PRD: Slack Teammate Ingress For Illo

Status: implemented MVP backend slice
Date: 2026-05-28
Owner: product/architecture discussion
Related docs:

- `docs/prd-inbound-coordination-layer.md`
- `docs/prd-universal-thread-context-ingress.md`
- `docs/personal-agent-connections-mvp.md`
- `docs/slack-teammate-self-hosted.md`

## Problem Statement

Many teams already live in Slack. If Illo requires teammates to leave Slack for
every collaboration moment, it will feel like another destination competing for
attention instead of a teammate that participates in the team's existing flow.

At the same time, Illospace is more than a chat surface. It contains durable
team memory, user-generated Domains, system workspace state, personal-agent
connections such as Codex, Threads as work surfaces, artifacts, decisions, and
outcomes. Slack can be the live conversation surface, but it cannot be the whole
workspace brain.

The product gap is therefore simple: Illo is not yet present where many teams
already talk. When a teammate mentions Illo in Slack or DMs Illo, Illo should
receive that Slack conversation as the triggering surface, use the normal
Illospace tool/workspace capabilities it already has, and reply naturally in
Slack.

## Solution

Build an Illo Slack app that behaves like a teammate in normal Slack
conversation.

The core relationship model is:

- Slack is where the team talks.
- Illo is a team member in that room.
- Illospace is the durable workspace, memory, tool, and action system behind
  that teammate.
- Slack changes where the conversation happens. It should not create a reduced
  or special Illo mode.

The MVP proof loop is:

```text
teammate mentions Illo in a Slack conversation
-> Slack event is received over Socket Mode, normalized, and recorded through
   inbound coordination
-> Illo receives Slack surface context with actor, audience, and message thread
-> Illo runs with its normal Illospace tool policy plus Slack read/reply tools
-> Illo replies naturally in the same Slack conversation
```

This PRD has one primary track: make `@Illo` and Illo DMs work in self-hosted
Slack. Other Slack polish can come later, but it is not required to prove that
Illo can be a teammate in Slack.

Do not build a private Slack-native Illo cockpit, split-view agent panel, or
top-bar assistant as the main direction. Slack's newer agent surfaces are useful
context, but the desired product is Illo flowing naturally in team
conversations, not opening a separate private agent UI inside Slack.

The first implementation is specifically for self-hosted open-source Illospace.
It should follow the simple Hermes-style setup:

```text
Illospace provides a Slack app manifest
-> self-hosting operator creates a Slack app in their workspace
-> operator enables Socket Mode and installs the app
-> operator gives Illospace a bot token and app-level Socket Mode token
-> Illospace opens an outbound Socket Mode connection to Slack
-> Illo receives app mentions and replies in Slack threads
```

Do not implement Slack HTTP Events API, OAuth-based app distribution, public
webhook endpoints, or Slack Marketplace-style install flows in this PRD. Those
belong to a later hosted-product PRD. The only architectural concession for
that future is a clean boundary between the Slack transport and the normalized
Slack surface envelope.

## Product Vocabulary

- **Slack Surface**: the Slack place where Illo was invoked, such as a channel,
  DM, or message thread.
- **Slack Conversation**: the Slack messages Illo can inspect for the current
  request, using the same intentional context-reading posture as other
  Illospace surfaces.
- **Slack Connection**: the installed Slack workspace/app connection mapped to
  an Illospace org.
- **Slack App Manifest**: the copyable manifest Illospace provides so a
  self-hosting operator can create their own Slack app.
- **Socket Mode Transport**: the self-hosted Slack event transport. Illospace
  connects outbound to Slack with an app-level token, so the server does not
  need a public HTTPS endpoint.
- **Slack Actor**: the Slack user who invoked Illo, mapped to an Illospace user
  when possible. In the self-hosted MVP, unmapped actors may still talk to Illo
  under permissive defaults while Illo learns or asks for mapping.
- **Slack Audience**: the people who can see Illo's reply in Slack, inferred
  from channel, DM, thread, and installation context.
- **Slack Reply Tool**: the Illo-visible capability for replying in the
  triggering Slack channel, thread, or DM.

## User Stories

1. As a teammate in Slack, I want to mention `@Illo` in a channel, so that I can
   ask for help without leaving the team conversation.
2. As a teammate in Slack, I want Illo to reply in the same Slack thread, so
   that the team can continue discussing in the place where the work started.
3. As a teammate in Slack, I want Illo to understand the immediate Slack thread,
   so that I do not have to restate the whole conversation.
4. As a teammate in Slack, I want Illo to answer simple questions directly in
   Slack, so that not every interaction sends me to another surface.
5. As a teammate in Slack, I want Illo to remember stable facts from Slack when
   asked, so that team knowledge can enter Illospace without manual copying.
6. As a teammate in Slack, I want Illo to update team/workspace state when
    asked, so that ownership, priorities, plans, or responsibilities remain
    current.
7. As a teammate in Slack, I want Illo to delegate work to Codex or another
    connected personal agent when appropriate, so that Slack can initiate real
    work instead of just discussion.
8. As a teammate in Slack, I want Illo to report delegated work progress back
    in Slack when it matters, so that the team sees useful updates where the
    request began.
9. As a teammate in Slack, I want Illo to know whether it was invoked in a
    public channel, private channel, DM, or Slack thread, so that its response
    matches the audience.
10. As a teammate in Slack, I want Illo to ask for clarification in Slack when a
    request is ambiguous, so that the team can resolve the ambiguity in flow.
11. As a teammate in Slack, I want to DM Illo directly, so that I can ask quick
    questions or coordinate privately without needing a channel.
12. As an Illospace user, I want my Slack identity linked to my Illospace
    identity, so that Illo can apply the right permissions and attribution.
13. As an Illospace workspace member, I want to connect Slack for my workspace,
    so that Illo can participate where the team invites it.
14. As an Illospace workspace member, I want Illo to work wherever my team adds
    it in Slack, so that Slack's own channel invitation model controls where
    Illo is present.
15. As an Illospace workspace member, I want clear audit records for
    Slack-origin actions, so that I can see what Slack request caused a
    workspace change.
16. As an operator, I want Slack events to use idempotency keys, so that retries
    do not create duplicate Threads, comments, records, or delegated tasks.
17. As Illo, I want Slack invocations to arrive through the inbound coordination
    layer, so that Slack, webhooks, MCP, and personal agents share one routing
    foundation.
18. As Illo, I want explicit tools for reading Slack conversation
    context, so that Slack messages do not become ambient prompt context for
    every run.
19. As Illo, I want explicit tools for replying in Slack, so that I can answer
    in the triggering surface.
20. As Illo, I want Slack-origin runs to have the normal Illo tool surface, so
    that Slack is a communication medium rather than a reduced-capability mode.
21. As a product builder, I want Slack surface metadata modeled consistently
    with other Illospace surfaces, so that future surfaces do not require
    hardcoded routing branches.
22. As a security reviewer, I want Illo's Slack replies to be audience-aware, so
    that a public Slack answer never reveals private workspace context.
23. As a security reviewer, I want Slack request provenance stored durably, so
    that Illo can explain which Slack workspace, channel, message, and actor
    caused an action.
24. As a self-hosting operator, I want Illospace to provide a Slack app
    manifest, so that I can create my own Slack app without hand-configuring
    every scope and event subscription.
25. As a self-hosting operator, I want the Slack integration to use Socket Mode,
    so that I do not need to expose my Illospace server to the public internet.
26. As a self-hosting operator, I want to configure only the Slack bot token and
    app-level Socket Mode token for the first slice, so that setup stays simple.
27. As a self-hosting operator, I want Slack token values to live in environment
    config or Illospace Vault, so that secrets are not committed to the repo.
28. As a self-hosting operator, I want a clear health check for the Slack
    connector, so that I can tell whether Illo is connected to Slack.
29. As a self-hosting operator, I want the first Slack integration to be
    permissive by default, so that my team can learn the workflow before tuning
    security policy.
30. As Illo, I want minimal tools for linking or remembering Slack-to-Illospace
    identity mappings, so that I can help the team establish identity context
    through conversation.

## Implementation Decisions

- Build a Slack app where Illo behaves as a normal Slack teammate through
  mentions, replies, and later shortcuts. Do not prioritize Slack's private
  agent split-view/cockpit surfaces for this direction.
- Scope the first implementation to self-hosted open-source deployments. Hosted
  OAuth install, HTTP Events API ingress, public redirect URLs, and Slack app
  marketplace/distribution concerns are out of scope until the hosted product
  track.
- Use Slack Socket Mode only for the self-hosted integration. Illospace should
  open an outbound WebSocket connection to Slack with an app-level token. The
  self-hosted server should not require a public HTTPS endpoint for Slack.
- Provide a Slack app manifest for operators. The manifest should follow the
  Hermes-style practical scope set for a capable Slack teammate, including app
  mentions, message history in channels where the bot is present, DMs, user
  lookup, and file read/write support.
- The intended setup UX is conversational from the Illospace webapp. A user can
  ask Illo to help set up Slack; Illo should know the required steps, surface
  the manifest/instructions, inspect current connector/source status, and ask
  the user to complete Slack-side actions that cannot be automated in
  self-hosted mode.
- Store the self-hosted Slack bot token and app-level Socket Mode token in
  environment config or Vault. The first self-hosted slice should not require a
  Slack client secret, OAuth redirect URL, or signing secret because there is no
  public HTTP Slack request endpoint.
- Treat Slack as a first-class external surface and source connection. Slack
  events should enter through the existing inbound coordination service rather
  than a Slack-only route that bypasses inbound event storage, idempotency,
  receipts, and source observability.
- Add a Slack source connection type that stores Slack workspace identity,
  installation metadata, allowed scopes, channel policy, bot token references,
  app-level token references, and health/status metadata.
- Treat Socket Mode envelope acknowledgement as transport plumbing. The
  connector should acknowledge Slack promptly, then hand normalized events to
  inbound coordination for durable processing.
- Keep the Slack transport boundary thin. Socket Mode specifics should not leak
  into Illo tool design or workspace UI behavior. A future hosted HTTP transport
  should be able to produce the same normalized Slack surface envelope.
- Normalize Slack events into an inbound envelope that includes:
  - event kind, such as mention, direct message, or message reply;
  - Slack workspace/team id;
  - channel id and channel type;
  - message timestamp and thread timestamp;
  - invoking Slack user id;
  - bounded text and message metadata;
  - Slack permalink when available;
  - idempotency key derived from Slack event id or event context;
  - surface metadata describing the triggering Slack surface.
- Model Slack as a triggering Surface. Illo runs admitted from Slack should know
  they came from Slack and should have explicit tools for reading from and
  replying to that Slack surface.
- Keep Slack conversation context bounded. The hot path should include the
  triggering message and enough thread/channel metadata to orient Illo. Larger
  Slack history reads should happen through explicit tools with limits.
- Enable Slack DMs in the self-hosted MVP. Illo should be able to respond to DMs
  naturally, matching the Hermes-style teammate feel. For the permissive
  self-hosted MVP, Illo should respond to every DM.
- Add an Illo-visible read tool for Slack conversation context. The tool should
  make scope explicit, such as triggering message, thread messages, recent
  channel messages, or permalink-targeted context.
- Add an Illo-visible write tool for Slack replies. The tool should support
  replying in the same Slack thread and should make ephemeral vs public replies
  explicit when both are available.
- Slack should not reduce Illo's workspace capabilities. A Slack-origin run
  should receive the normal Illo tool policy for that user/org, plus Slack
  surface read/reply tools. Slack changes the conversation surface, not Illo's
  ability to act in Illospace.
- Do not add Slack-specific Thread linking, sync, or rich preview behavior in
  the MVP. Those may emerge later, but they are not necessary to prove `@Illo`
  in Slack.
- Permission checks can become stricter later, especially for hosted/shared
  deployments. The self-hosted MVP should be permissive by default so the team
  can learn the interaction model, while leaving clear seams for stricter
  policy once real usage teaches the risks.
- Store Slack provenance on inbound events and any later workspace actions:
  workspace/team id, channel id, message timestamp, thread timestamp, permalink,
  actor, connection id, inbound event id, and dedupe keys.
- Identity mapping is a first-class requirement, but it should not block the
  first self-hosted experience. Slack users should map to Illospace users when
  possible, and Illo should have minimal tools to inspect, remember, or request
  mapping through conversation.
- Slack workspace installation should map to one Illospace org. Multi-org or
  shared-channel behavior is out of scope until the basic org mapping is proven.
- Installation/admin UX should allow an org admin to connect Slack, inspect
  connection health, and disconnect/revoke. The first channel policy should
  rely on Slack itself: Illo is present where users invite/add the bot.
- Slack retries should be idempotent. Duplicate events must not produce
  duplicate replies, Threads, Domain records, delegated tasks, or context
  attachments.
- Outbound Slack failures should be visible in inbound receipts/source cards so
  Illo and operators can diagnose bad tokens, missing channel access, or Slack
  API errors.
- Add a connector health signal for self-hosted deployments. At minimum it
  should expose whether the Socket Mode connection is configured, connected,
  recently disconnected, or failing because of invalid credentials/scopes.
- The Slack app should use the product name "Illo" in Slack-facing copy.
  Internal legacy "idea" terminology must not leak into Slack responses.

## Runtime Posture

Slack should not fork Illo's behavior. A Slack-origin request should feel like a
normal Illo request whose triggering and reply surface happens to be Slack.

The Slack integration adds:

- a self-hosted Slack Socket Mode transport;
- normalized Slack surface metadata;
- explicit Slack context-reading tools;
- explicit Slack reply tools;
- setup and identity-mapping support.

It does not add a separate Slack-specific product workflow. If Illo can update
workspace state, delegate to Codex, create a Thread, or use another existing
tool from the webapp, the same capability should be available from Slack under
the normal Illo tool policy.

## Testing Decisions

Good tests should validate external behavior, permission boundaries, and
durable contracts rather than implementation details.

Important coverage:

- Slack Socket Mode connector starts with valid bot/app tokens and refuses to
  start or reports unhealthy state when required tokens are missing.
- Socket Mode payloads are acknowledged promptly and then normalized into
  durable inbound events.
- Slack event normalization preserves workspace, channel, user, message,
  thread, permalink, surface, and idempotency metadata.
- Inbound coordination stores Slack-origin events with source actor, authority
  user/org, normalized envelope, receipts, and status transitions.
- Duplicate Slack retries replay idempotently without duplicate Slack replies
  or duplicate workspace actions.
- Slack mentions can admit a surface-aware Illo run.
- Illo can explicitly read bounded Slack thread context through a tool.
- Illo can explicitly reply to the triggering Slack thread through a tool.
- Simple Slack questions can complete with only a Slack reply and audit record.
- Slack-origin runs have the normal Illo tool policy plus Slack read/reply
  tools, so existing workspace actions remain available from Slack.
- Permission tests cover the current permissive self-hosted policy and document
  the stricter cases that will need enforcement before hosted rollout.
- Installation, token revocation, missing channel access, and Slack API failure
  paths produce inspectable source-card/receipt diagnostics.

Useful prior art in the codebase:

- Existing inbound webhook and MCP tests for shared envelope submission,
  idempotency, receipts, source connections, source cards, and replay.
- Existing Cortex/Thread tests for run admission, surface-aware Discussion
  replies, context submissions, and stable Thread URLs.
- Existing chat tests for mentions, agent-authored replies, notifications, and
  websocket behavior.
- Existing personal-agent connection tests for scoped identity, delegation, and
  external task lifecycle.

## MVP Slices

1. **Slack app install and mention loop**
   - Provide an operational static Slack app manifest for self-hosted
     operators.
   - Configure bot token and app-level Socket Mode token.
   - Start a self-hosted Slack connector that opens an outbound Socket Mode
     connection.
   - Receive app mentions and DMs.
   - Record the event through inbound coordination.
   - Admit a surface-aware Illo run.
   - Reply in the same Slack thread.

2. **Slack surface tools**
   - Add Slack context reads.
   - Add Slack replies for channels, threads, and DMs.
   - Give Slack-origin Illo runs the normal Illo tool policy plus Slack
     read/reply tools.

3. **Setup help and identity mapping**
   - Give Illo enough tools and docs-backed knowledge to help an operator set up
     the Slack app from the webapp.
   - Add minimal Slack-to-Illospace identity mapping tools.
   - Keep setup permissive enough that unmapped users do not block first use.

4. **Future Slack polish**
   - Link previews, message shortcuts, and sync behaviors are intentionally
     later work after `@Illo` feels right.

## Out Of Scope

- A private Slack-native Illo cockpit, split-view agent panel, or top-bar
  assistant as the primary product direction.
- Slack HTTP Events API ingress for the self-hosted MVP.
- OAuth-based "Add to Slack" installation and hosted Slack app distribution.
- Slack Marketplace/public app review.
- Requiring self-hosted operators to expose Illospace over public HTTPS for
  Slack callbacks.
- Treating Slack as the canonical workspace database.
- Mirroring every Slack message into Illospace.
- Mirroring every Illospace Thread Discussion message back into Slack.
- Replacing Illospace Threads, Domains, workspace state, or personal-agent
  connections with Slack-native state.
- Automatically creating a Thread for every Slack mention.
- Automatic Illo participation in Slack conversations without an explicit
  mention, shortcut, subscription, or configured policy.
- Broad channel monitoring without the bot being explicitly added/invited.
- Multi-org Slack Enterprise Grid complexity and Slack Connect/shared-channel
  semantics in the first slice.
- Full bidirectional Slack/Illospace sync.
- Agent-to-agent MCP integration through Slackbot or Slack-native agents as a
  near-term goal.

## Open Questions

No open product questions should block the first `@Illo` implementation. The
remaining details are implementation choices: exact code shape for setup help,
identity mapping, and connector health reporting.

## Implementation Clarity Needed

These decisions should be settled before the first engineering slice:

1. **Connector process shape**
   - Decision: run the Slack Socket Mode connector as a separate optional
     process in the same Illospace installation.
   - In Docker Compose this may be a separate service/container using the same
     image and config, not a second Illospace install.
   - Rationale: the connector has a long-lived WebSocket lifecycle and should be
     easy for self-hosters to enable or disable without mixing it into the API
     server lifecycle.

2. **Slack app manifest delivery**
   - Decision: ship a checked-in static manifest first.
   - Add a generated command later only if URLs, scopes, commands, or config
     become dynamic enough to justify it.

3. **Minimal Slack scopes and events**
   - Decision: choose an operational manifest rather than asking the user to
     design one.
   - Use the Hermes posture as the baseline: `app_mentions:read`,
     `channels:history`, `channels:read`, `chat:write`, `files:read`,
     `files:write`, `groups:history`, `groups:read`, `im:history`, `im:read`,
     `im:write`, and `users:read`.
   - Subscribe to app mentions plus channel/group/DM message events:
     `app_mention`, `message.channels`, `message.groups`, and `message.im`.
   - Do not include Slack assistant-view events in the first Illo manifest.
     Hermes uses them, but Illo is not pursuing Slack's native agent cockpit
     surface in this PRD.

4. **Allowed surface policy**
   - For the first self-hosted version, should Illo rely only on Slack's own
     invite/add-to-channel control, or also offer optional Illospace-side
     channel/user allowlists?
   - Decision: DMs are enabled for the self-hosted MVP.
   - Decision: Illo should respond to every DM in the self-hosted MVP.
   - Decision: rely on Slack bot invitation as the primary channel control.
     Users add Illo where they need it.
   - Optional Illospace-side allowlists can come later only if they stay out of
     the default self-hosted path.

5. **Identity policy**
   - Decision: identity mapping matters, but unmapped Slack users should not
     block the first self-hosted experience.
   - Add a small Illo-visible setup/mapping tool surface that can inspect Slack
     workspace/user metadata, show unmapped Slack users, and link a Slack user
     id to an Illospace user id when the human confirms it.
   - Illo should use this tooling conversationally when a user asks for help
     setting up Slack or when identity context matters.

6. **Secret storage**
   - Decision: use environment variables for the first self-hosted slice:
     `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`.
   - Create or refresh a Slack source connection/health record on connector
     startup so inbound events still have a durable source identity.
   - Vault-backed setup can come later.

7. **Slack reply/runtime behavior**
   - Decision: Slack should not dictate a special Illo behavior mode. Illo
     should respond as it would from any other surface, using Slack only as the
     triggering/reply surface.
   - Do not add a Slack-specific "I'm on it" acknowledgement unless the normal
     Illo run experience needs such an acknowledgement across surfaces.

8. **Operator health surface**
   - Decision: keep health simple for MVP.
   - The connector should log startup, connection, disconnect, credential, and
     Slack API errors clearly.
   - The Slack source connection/health record should expose a basic status for
     Illo to inspect if the user asks from the webapp.
   - A dedicated admin UI can come later.

## Research Notes

As of 2026-05-28, Slack is actively positioning itself as an agentic workspace
and agent hub. Relevant current references:

- Hermes Slack setup:
  `https://hermes-agent.nousresearch.com/docs/user-guide/messaging/slack`
- OpenClaw Slack setup:
  `https://docs.openclaw.ai/channels/slack`
- Slack announcement: `https://slack.com/blog/news/slack-is-where-agents-work`
- Slack AI app overview: `https://docs.slack.dev/ai/`
- Slack agent entry and interaction docs:
  `https://docs.slack.dev/ai/agent-entry-and-interaction/`
- Slack Socket Mode docs:
  `https://docs.slack.dev/apis/events-api/using-socket-mode/`
- Slack app manifest docs:
  `https://docs.slack.dev/app-manifests/configuring-apps-with-app-manifests/`
- Slack agent design guidance:
  `https://docs.slack.dev/concepts/agent-design/`

The product decision in this PRD is deliberate: even though Slack is investing
in private/native agent surfaces, Illo should first be a teammate in normal
Slack conversations. Native agent cockpit experiences are not the desired
center of gravity for Illo.

The implementation decision is also deliberate: Hermes is the better model for
the first self-hosted Illospace slice because it keeps setup simple with a
manifest and Socket Mode tokens. OpenClaw's dual Socket Mode/HTTP approach is
useful prior art, but the HTTP transport belongs to a later hosted-product path.

## Further Notes

Slack link previews, sync behaviors, message shortcuts, and other Slack polish
remain useful later. They are intentionally not part of this first self-hosted
Slack slice. The first slice succeeds when a user can set up the app, mention
or DM Illo in Slack, and get a natural Illo response with normal Illospace
capabilities behind it.

This PRD should be read as the Slack-specific evolution of the inbound
coordination and surface-aware Thread work. The same principles apply: inbound
events are durable, source-aware, idempotent, and inspectable; Illo is given
surface context and tools; the frontend renders durable state; and product
policy should live in Illo's coordination layer rather than scattered across
Slack-specific conditionals.

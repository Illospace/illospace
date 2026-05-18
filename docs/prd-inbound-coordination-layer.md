# PRD: Inbound Coordination Layer for IloSpace

Status: draft for review  
Date: 2026-05-18  
Owner: product/architecture discussion  

## Naming And Scope

This PRD uses the following vocabulary deliberately:

- **IloSpace** means the whole workspace, codebase, product, and team coordination system.
- **Ilo** means the agent that lives inside IloSpace: the LLM-powered coordinator and triage actor.
- **External Source Connection** means any outside tool, app, agent, webhook source, IDE, script, or personal worker connected to IloSpace.
- **Inbound Signal** means something an external source tells IloSpace happened.
- **External Task** means work IloSpace asks an external source to do.
- **Workspace Action** means a mutation inside IloSpace: post to a thread, create an idea, link to a project, notify a teammate, ask a human, or trigger an Ilo run.

Current implementation and repo docs may still use the existing `Illospace` / `Illo` spelling and `illo_*` API/tool prefixes. This PRD is about product architecture and uses the requested conceptual distinction: **IloSpace is the system; Ilo is the agent**.

## Problem Statement

Teams increasingly work through many personal and external tools: Codex, Hermes, OpenClaw, Claude Code, OpenCode, Figma, Jira, GitHub, Linear, scripts, webhooks, and future agents. Each tool can generate useful work context, but today those tools either need to know exactly where to write in IloSpace or they need a human to manually copy updates into the right thread.

That creates the wrong burden. External tools are good at doing local work, observing their own state, and sending factual updates. They are not the right place to decide how team context should be organized across IloSpace threads, ideas, pins, projects, notifications, and agent runs.

At the same time, routing every incoming event through a full Ilo LLM triage would be too slow, expensive, and noisy. A Jira webhook that always sends the same issue event should not make Ilo repeatedly rediscover what Jira is, what the payload means, and where it probably belongs. Repeated judgment should become reusable system knowledge.

The product needs a bare but strong foundation for inbound coordination:

- external tools submit signals and intent into IloSpace;
- IloSpace records, authenticates, normalizes, dedupes, and applies known policy deterministically;
- Ilo triages ambiguity when the system cannot safely decide;
- repeated Ilo triage decisions become routing receipts, learned patterns, and eventually deterministic rules;
- personal agents can still receive tasks from IloSpace without being conflated with simple inbound webhook sources.

## Solution

Build a generalized **External Source Connection + Inbound Signal** layer in IloSpace.

Every outside system is represented as an External Source Connection with identity, auth, scopes, and capabilities. A connection may be a personal agent, a coding tool, a webhook app, a design tool, a code host, or a custom script. The important distinction is not the product name; it is what the connection can do.

Connections can have capabilities such as:

- submit inbound signals;
- receive external tasks from IloSpace;
- report task events and task results;
- ask Ilo for private workspace context;
- request explicit workspace actions only when strongly scoped and explicitly targeted.

The default inbound API/MCP/webhook primitive should be signal submission, not direct workspace mutation. External tools should generally say:

> “Here is something that happened; IloSpace should decide what to do with it.”

They should not generally say:

> “Post this to that thread and trigger Ilo.”

IloSpace should route incoming envelopes through deterministic policy first. If the signal has an explicit task binding, explicit approved target, source rule, idempotency match, known payload fingerprint, or learned pattern, IloSpace can execute the action without invoking Ilo. If the signal is ambiguous, Ilo performs triage, chooses an action, and records a Triage Receipt. Over time, repeated receipts can promote into source rules or learned patterns so future events bypass or minimize Ilo triage.

Existing Hermes/OpenClaw work remains valuable. It becomes the first implemented specialization of this broader model: a personal-agent connection that can receive outbound tasks and report results. Jira/GitHub/Figma-style sources are connections that mostly submit inbound signals. Codex can be either: a normal session may only submit signals, while a long-running Codex daemon could also receive tasks.

## V1 Alignment And Parallel Work Split

The webhook integration workstream and the MCP/personal-tool workstream should land on the same foundation. They are not competing designs. They are two ingress lanes into one inbound coordination layer.

The shared rule is:

> Different ingress, same envelope, same policy/triage path, same workspace executor.

### Shared Foundation Workstream

One implementation owner should build the smallest common backend path first. This path must be usable by both webhook integrations and hosted MCP tools.

The foundation owns:

- External Source Connection identity, auth, org/workspace scope, source type, capabilities, and status.
- Inbound Envelope persistence for raw payload, normalized payload, idempotency, status, source metadata, and processing result.
- Source Policy lookup for allowed envelope kinds, instructions, origin patterns, action permissions, thresholds, review mode, and schema expectations.
- Deterministic policy evaluation before Ilo triage.
- Ilo triage interface that returns a strict Workspace Action Plan.
- Triage Receipt persistence for decision, confidence, target, action, reasoning summary, features used, and reusable-pattern candidate.
- Workspace Executor interface for the first safe action types.

The foundation should expose one internal service interface that both product surfaces call:

```text
submit_inbound_envelope(connection, envelope, ingress_context) -> InboundProcessingResult
```

The first stable envelope shape should be small:

```json
{
  "kind": "signal",
  "origin": "source.event_name",
  "payload": {},
  "summary": null,
  "hints": {},
  "desired_outcome": null,
  "idempotency_key": null
}
```

The first result shape should make review/debugging possible:

```json
{
  "status": "processed | review_required | quarantined | failed",
  "event_id": "inbound-event-id",
  "matched_policy_id": "policy-id-or-null",
  "action_plan": {},
  "action_result": {},
  "confidence": 0.91
}
```

### Webhook Integrations Workstream

This is the colleague’s V1 lane. It should implement configured inbound integrations around `POST /webhooks`, while calling the shared foundation service rather than building a separate webhook-only processing stack.

Webhook V1 owns:

- Public webhook ingress.
- Authenticated integration connection or integration token.
- Minimal external payload: `origin` and `payload`, plus optional idempotency/header metadata.
- Origin pattern matching with explicit priority/order.
- User-friendly incoming schema builder that compiles to internal JSON Schema or equivalent validation.
- Integration instructions for Ilo triage.
- Allowed actions and auto-execute thresholds.
- Dry-run/test payload flow.
- Event logs.
- Replay.
- Integrations UI.

The webhook proposal maps directly to the shared model:

```text
integrations                  -> External Source Connections / Source Policies
POST /webhooks                -> webhook ingress
origin + payload              -> Inbound Envelope
origin_patterns + priority    -> deterministic source policy matching
integration_events            -> Inbound Envelope/Event Store
LLM strict plan               -> Ilo triage action plan
allowed_actions + thresholds  -> Workspace Executor gate
event logs + replay           -> receipt/replay/audit tooling
```

For V1 webhook actions:

- `open_cortex_thread` may auto-execute above threshold.
- `create_memory` may auto-execute above threshold, but must preserve source provenance and remain easy to inspect/delete.
- `create_domain_record` must start in review/dry-run only, even at high confidence, because it writes into stricter domain structure.

Webhook integrations are **workspace/org-level system actors**, not user-owned personal actors. They must still be scoped to one IloSpace workspace/org. They must not be global across all orgs.

### MCP / Personal Tool Signal Workstream

This is the Codex/personal-tool lane. It should implement a hosted MCP tool that submits the same Inbound Envelope shape through the same foundation service.

MCP V1 owns:

- Hosted MCP tool conceptually named `submit signal` or `illo_submit_signal`.
- Tool description that clearly tells coding agents and personal tools not to pick threads or mutate workspace state by default.
- Input shape aligned with the shared envelope.
- Good defaults for coding-session updates: progress summary, repo/branch hints, files touched, task title, source tool, and desired outcome.
- Codex hook guidance showing when a personal tool should submit a signal.
- Examples for Codex, Claude Code, OpenCode, and other personal work tools.
- Regression tests ensuring default MCP guidance points clients toward signal submission rather than direct thread mutation.

The MCP lane should treat direct `create_thread` / `post_thread_message` style tools as advanced or compatibility tools, not the default path. The default path is:

```text
Personal tool -> hosted MCP submit signal -> Inbound Envelope -> Source Policy -> deterministic policy or Ilo triage -> Workspace Action
```

The MCP lane should not block on the webhook UI. It only needs the shared foundation service and a connection/token with `can_submit_signals`.

### Existing Hermes/OpenClaw Compatibility Workstream

The existing personal-agent task lane should remain intact while the inbound foundation lands.

For now:

- Keep External Tasks for IloSpace-to-personal-agent delegation.
- Keep task claim/event/artifact/complete/fail behavior working.
- Keep hosted personal-agent MCP reads and headless asks working.

Later:

- Task events and task results can be represented as bound Inbound Envelopes.
- Hermes/OpenClaw can also use `submit signal` for free-form progress outside a delegated task.

### First Shared Milestone

The first implementation milestone is successful when these two ingress paths create the same internal kind of record and use the same processing service:

```text
POST /webhooks
origin = jira.ticket_created
payload = {...}
```

and:

```text
MCP submit signal
origin = codex.progress
payload/summary = {...}
```

Both must produce:

- an authenticated External Source Connection;
- a stored Inbound Envelope/Event;
- a matched Source Policy or default policy;
- a deterministic decision or Ilo Triage Receipt;
- a Workspace Action result or review/quarantine outcome;
- provenance visible in logs and any workspace projection.

## Architecture Diagrams

### 1. Conceptual Boundary

```mermaid
flowchart LR
    subgraph Outside["Outside IloSpace"]
        Tools["External tools, apps, agents, webhooks"]
        People["Humans using personal tools"]
    end

    subgraph IloSpace["IloSpace system"]
        Ingress["Unified ingress<br/>MCP / webhook / REST"]
        Policy["Deterministic policy layer<br/>auth, scope, dedupe, rules, fingerprints"]
        Agent["Ilo<br/>LLM triage agent"]
        Executor["Workspace executor<br/>applies chosen action"]
        Workspace["Workspace state<br/>threads, ideas, pins, projects, tasks, notifications"]
    end

    People --> Tools
    Tools --> Ingress
    Ingress --> Policy
    Policy -->|"known / safe"| Executor
    Policy -->|"ambiguous"| Agent
    Agent --> Executor
    Executor --> Workspace
```

### 2. Connection Capability Model

```mermaid
flowchart TD
    Conn["External Source Connection<br/>identity, auth, scopes, capabilities"]

    Conn --> Signals["can_submit_signals"]
    Conn --> Tasks["can_receive_tasks"]
    Conn --> TaskEvents["can_report_task_events"]
    Conn --> Context["can_ask_context"]
    Conn --> Explicit["can_request_explicit_actions<br/>advanced / tightly scoped"]

    Signals --> S1["Codex session progress"]
    Signals --> S2["Jira issue event"]
    Signals --> S3["GitHub PR event"]
    Signals --> S4["Figma design update"]

    Tasks --> T1["Hermes task"]
    Tasks --> T2["OpenClaw task"]
    Tasks --> T3["Future Codex daemon task"]

    TaskEvents --> R1["started / blocked / completed / failed"]
    Context --> C1["private Ilo context answer"]
    Explicit --> E1["only when target + permission are explicit"]
```

### 3. Inbound Signal Flow

```mermaid
sequenceDiagram
    participant Source as External Source
    participant Ingress as IloSpace Ingress
    participant Store as Signal Store
    participant Policy as Policy Engine
    participant Ilo as Ilo Triage Agent
    participant Exec as Workspace Executor
    participant Workspace as IloSpace Workspace

    Source->>Ingress: submit envelope
    Ingress->>Ingress: authenticate, validate, rate limit
    Ingress->>Store: store raw payload + normalized signal
    Store->>Policy: evaluate signal

    alt explicit binding or known rule
        Policy->>Exec: deterministic decision
    else ambiguous signal
        Policy->>Ilo: request triage
        Ilo->>Store: write triage receipt
        Ilo->>Exec: chosen action
    end

    Exec->>Workspace: post/create/link/notify/ask/trigger
    Policy->>Store: update fingerprints and rule candidates
```

### 4. External Tasks Versus Inbound Signals

```mermaid
flowchart LR
    subgraph IloSpace["IloSpace"]
        Idea["Thread / idea / project context"]
        Task["ExternalTask<br/>IloSpace asks outside worker"]
        Signal["InboundSignal<br/>outside source tells IloSpace something"]
        Triage["Ilo or deterministic policy"]
        Action["Workspace Action"]
    end

    subgraph PersonalAgent["Personal agent / worker"]
        Hermes["Hermes"]
        OpenClaw["OpenClaw"]
        CodexDaemon["Future Codex daemon"]
    end

    Idea --> Task
    Task --> Hermes
    Task --> OpenClaw
    Task --> CodexDaemon

    Hermes -->|"task_event / task_result"| Signal
    OpenClaw -->|"task_event / task_result"| Signal
    CodexDaemon -->|"task_event / task_result"| Signal

    Signal --> Triage
    Triage --> Action
```

### 5. LLM Bypass Ladder

```mermaid
flowchart TD
    A["Inbound envelope arrives"] --> B{"Explicit task/result binding?"}
    B -->|"yes"| X["Deterministic executor"]
    B -->|"no"| C{"Explicit approved target?"}
    C -->|"yes"| X
    C -->|"no"| D{"Source rule matches?"}
    D -->|"yes"| X
    D -->|"no"| E{"Known payload fingerprint<br/>with high-confidence route?"}
    E -->|"yes"| X
    E -->|"no"| F{"Candidate route set is small?"}
    F -->|"yes"| G["Light Ilo triage<br/>choose among candidates"]
    F -->|"no"| H["Full Ilo triage<br/>interpret signal and decide"]
    G --> I["Triage receipt"]
    H --> I
    I --> X
    I --> J["Rule / fingerprint candidate<br/>for future fast path"]
```

### 6. Parallel Workstreams

```mermaid
flowchart TD
    Foundation["Shared Foundation<br/>External Source Connection<br/>Inbound Envelope Store<br/>Source Policy<br/>Ilo Triage<br/>Workspace Executor"]

    Webhook["Webhook Integrations V1<br/>POST /webhooks<br/>origin rules<br/>schema builder<br/>instructions<br/>logs/replay"]

    MCP["MCP / Personal Tool V1<br/>hosted submit signal tool<br/>Codex hook guidance<br/>personal tool examples"]

    Agents["Existing Personal Agent Lane<br/>Hermes/OpenClaw task lifecycle<br/>claim/events/artifacts/complete/fail"]

    Webhook -->|"same envelope"| Foundation
    MCP -->|"same envelope"| Foundation
    Agents -->|"later: task_event/task_result envelope"| Foundation

    Foundation --> Action["Workspace Action Result<br/>thread, memory, review, quarantine,<br/>domain record dry-run, trigger"]
```

## User Stories

1. As a teammate using Codex, I want Codex to submit a progress signal to IloSpace, so that the team can stay aware of meaningful work without me manually writing a status update.
2. As a teammate using any personal coding tool, I want the tool to submit the same kind of signal shape, so that IloSpace does not need a Codex-specific design.
3. As a teammate using Hermes or OpenClaw, I want my personal agent to share progress back into IloSpace, so that external autonomous work becomes visible to the team.
4. As a teammate using Hermes or OpenClaw, I want IloSpace to delegate work to my personal agent, so that I can use my preferred agent without leaving the team coordination layer.
5. As a teammate using a future Codex daemon, I want IloSpace to delegate tasks to that daemon when it supports task receiving, so that Codex can be treated like a capable external worker rather than a special case.
6. As a teammate using Jira, I want Jira issue events to enter IloSpace as signals, so that relevant project activity can be routed into team context.
7. As a teammate using GitHub, I want pull request and issue activity to enter IloSpace as signals, so that engineering changes can be connected to threads, projects, and pins.
8. As a designer using Figma, I want design updates to enter IloSpace as signals, so that design work can be coordinated with engineering and product threads.
9. As an admin, I want to register an External Source Connection, so that each outside source has identity, auth, scopes, and capabilities.
10. As an admin, I want to configure whether a connection can submit signals, receive tasks, report task events, ask for context, or request explicit actions, so that each source has the minimum required power.
11. As an admin, I want webhook sources to have source-specific policies, so that common payloads can be normalized and routed cheaply.
12. As an admin, I want connection tokens to be scoped, revocable, and auditable, so that external sources never need broad internal service credentials.
13. As an admin, I want repeated source behavior to produce proposed rules, so that IloSpace becomes faster and cheaper over time.
14. As an admin, I want to approve or reject proposed routing rules, so that learned automation stays understandable and governed.
15. As an admin, I want to inspect a connection’s recent signals, task events, failures, and decisions, so that I can debug integrations.
16. As an admin, I want low-confidence or sensitive actions to require human confirmation, so that external automation cannot silently create messy or risky workspace state.
17. As Ilo, I want incoming signals to include raw payload, normalized fields, hints, source identity, and policy context, so that I can triage only when deterministic policy is insufficient.
18. As Ilo, I want every triage decision to produce a receipt, so that future signals can reuse prior judgment without repeating full reasoning.
19. As Ilo, I want deterministic rules and payload fingerprints to handle common cases, so that my LLM reasoning is reserved for ambiguity.
20. As Ilo, I want to choose among candidate destinations when the policy engine narrows the search, so that light triage is cheaper than full workspace reasoning.
21. As Ilo, I want to create a new idea/thread when no suitable home exists, so that novel signals are not forced into the wrong context.
22. As Ilo, I want to post to an existing thread when the signal clearly belongs there, so that team progress stays attached to prior discussion.
23. As Ilo, I want to link signals to projects and pins, so that broader subjects can accumulate activity across many threads.
24. As Ilo, I want to ignore or privately store low-value signals, so that the workspace does not become noisy.
25. As Ilo, I want to trigger an agent run only when the signal calls for active reasoning or action, so that not every update becomes work.
26. As a teammate reading a thread, I want external updates to show their source and provenance, so that I understand where the information came from.
27. As a teammate reading a thread, I want task results from Hermes/OpenClaw/future workers to appear in the right thread automatically, so that delegated work closes the loop.
28. As a teammate reading a project, I want related external signals to be linked or summarized, so that project context includes work happening outside IloSpace.
29. As a teammate, I want the system to distinguish factual signal submission from explicit workspace commands, so that personal agents do not accidentally take over coordination decisions.
30. As a teammate, I want to explicitly tell an external tool to post to a known target when needed, so that deterministic commands remain possible for precise human-directed actions.
31. As a coding agent, I want a simple `submit signal` tool, so that I do not need to search the workspace, pick a thread, and decide whether to trigger Ilo.
32. As a coding agent, I want tool descriptions to make the default behavior clear, so that I submit intent instead of mutating workspace state directly.
33. As a webhook integration, I want a stable ingestion endpoint, so that events can enter IloSpace without pretending to be personal agents.
34. As a webhook integration, I want payload normalization recipes, so that repeated payload shapes become typed signals.
35. As a webhook integration, I want idempotency and replay protection, so that retries do not create duplicate updates.
36. As a system operator, I want raw payload storage with redaction policy, so that integrations are auditable without leaking unnecessary sensitive data.
37. As a system operator, I want rate limits per connection and source type, so that noisy external systems cannot overwhelm IloSpace.
38. As a system operator, I want source policy to define default visibility, allowed actions, and escalation behavior, so that each integration has predictable boundaries.
39. As a system operator, I want routing receipts to be searchable, so that I can understand why IloSpace posted, created, linked, ignored, or asked for help.
40. As a product builder, I want the existing Hermes/OpenClaw task lifecycle to remain intact, so that current personal-agent work does not need to be rewritten.
41. As a product builder, I want inbound signals to be separate from external tasks, so that Jira/GitHub/Figma events do not pollute the task lifecycle model.
42. As a product builder, I want shared auth and connection concepts across signals and tasks, so that the foundation stays simple even as capabilities differ.
43. As a product builder, I want direct workspace action tools to be demoted to internal executor or advanced scoped tools, so that the default MCP does not teach agents to route by themselves.
44. As a product builder, I want future direct action tools to require explicit target and permission, so that deterministic actions remain safe.
45. As a product builder, I want learned rules to be generated from receipts rather than hidden memory calls, so that the system’s learning is durable and inspectable.
46. As a product builder, I want a replay harness for old signals and receipts, so that routing policy changes can be tested against real historical examples.
47. As a product builder, I want source cards that summarize connection behavior, common payloads, rules, and examples, so that integration behavior is understandable at a glance.
48. As a teammate, I want IloSpace to coordinate among personal agents rather than compete with them, so that I can keep my preferred tools while the team gains shared context.
49. As a webhook integration builder, I want my `POST /webhooks` implementation to call the same inbound envelope service as MCP signal submission, so that the webhook lane does not drift into a separate architecture.
50. As an MCP integration builder, I want my hosted MCP tool to call the same inbound envelope service as webhooks, so that Codex-style progress updates and Jira-style webhook events become comparable system inputs.
51. As a product builder, I want a clear foundation workstream, webhook workstream, and MCP workstream, so that multiple people can build in parallel without stepping on each other.
52. As a webhook integration builder, I want `create_domain_record` to start as review/dry-run only, so that strict domain writes are not accidentally automated too early.
53. As a webhook integration builder, I want integrations to act as workspace/org-level system actors rather than user-owned actors, so that incoming Jira/GitHub/Stripe events are not incorrectly attributed to one teammate.
54. As a security-minded admin, I want integration actors to remain scoped to one workspace/org, so that “system actor” never means cross-org global write access.
55. As a coding agent, I want the hosted MCP tool to accept repo, branch, task, and file hints, so that IloSpace can route progress without forcing me to choose a thread.
56. As an operator, I want the first shared milestone to prove webhook and MCP inputs produce the same internal records, so that the foundation is real and not just conceptual.

## Implementation Decisions

- Add a generalized External Source Connection concept that is capability-based, not product-name-based. Hermes, OpenClaw, Codex, Jira, GitHub, Figma, and custom scripts are all connections with different capabilities.
- Use one shared inbound service for webhook, MCP, and future inbound lanes. Do not build a webhook-only service that cannot be reused by the hosted MCP tool.
- Implement the shared foundation before or alongside the first webhook/MCP slices. Both product lanes should call `submit_inbound_envelope` or an equivalent service boundary.
- Treat the colleague’s webhook proposal as the first configured-source implementation of the broader inbound architecture.
- Treat the Codex/MCP proposal as the first personal-tool signal implementation of the broader inbound architecture.
- Scope integration actors to a workspace/org while not attributing them to a specific user by default.
- Require authenticated source identity for public webhook ingress. The `origin` string is routing metadata, not proof of identity.
- Preserve the existing personal-agent task model for outbound delegation. It is the right shape for long-running remote work with status, artifacts, and completion.
- Do not stretch the external task lifecycle to represent arbitrary inbound webhook or progress events. Add a separate inbound signal model.
- Introduce an inbound envelope contract that can represent `signal`, `request`, `task_event`, and `task_result`.
- Treat `signal` as the default for external observations: progress updates, webhook events, design updates, ticket changes, and code host activity.
- Treat `request` as an external source asking IloSpace to consider doing something, without assuming the source gets to mutate the workspace directly.
- Treat `task_event` and `task_result` as deterministic updates tied to an existing External Task created by IloSpace.
- Add a default MCP/API/webhook entrypoint for signal submission. The preferred public tool should be conceptually named `submit signal`, even if the final code-level prefix follows existing naming conventions.
- Make direct workspace mutation tools advanced, scoped, and non-default. They may remain internally as executor capabilities, but automatic hooks and general personal-agent MCP guidance should prefer signal submission.
- Route all inbound envelopes through IloSpace. The system may bypass Ilo LLM triage through deterministic policy, but external sources should not perform fuzzy routing themselves.
- Add a deterministic policy layer before Ilo triage. It handles auth, scopes, validation, dedupe, idempotency, redaction, explicit task binding, explicit approved targets, source rules, payload fingerprints, and learned high-confidence patterns.
- Add Ilo triage only for ambiguous cases or cases where policy requires agent judgment.
- Add Triage Receipts as durable records of agent or policy decisions. Receipts should include signal id, decision, target, action, confidence, reasoning summary, features used, whether a human was asked, and whether the decision is reusable.
- Add learned pattern support from repeated receipts. When many similar signals are routed the same way, IloSpace can propose a deterministic rule.
- Add source policies for each connection. Policies define allowed envelope types, default visibility, allowed actions, triage prompt, deterministic rules, confirmation thresholds, redaction behavior, and cost policy.
- Add payload fingerprints that hash stable source/event/schema/features rather than full payload content. Fingerprints should support fast recognition of repeated payload shapes.
- Add source cards as an operator-facing summary of each connection: what it sends, common payloads, known rules, examples, recent errors, and current capabilities.
- Add a replay harness that can run historical signals against current policy and triage settings without mutating the workspace.
- Keep personal-agent connection tokens and scoped auth as the template for future connection security. Do not use broad internal service tokens for external sources.
- Add connection capabilities rather than separate tables per source type wherever practical. Capabilities should make Codex, Hermes, OpenClaw, Jira, GitHub, and Figma differ by behavior, not by bespoke architecture.
- For personal agents that can receive tasks, the existing outbound bridge model remains valid: IloSpace creates an External Task; the worker claims or receives it; task events and results return as bound inbound envelopes.
- For sources that only send events, the source only needs signal submission capability.
- For Codex, support both modes conceptually: ordinary sessions submit signals; future long-running Codex workers may receive tasks.
- For Jira/GitHub/Figma, start with inbound-only signal connections and source policies.
- For webhook V1, support origin matching with explicit priority/order. The first active integration/policy whose rules match the authenticated source and origin wins.
- For webhook V1, support a user-friendly schema builder that compiles to internal validation. Do not require users to hand-author raw JSON Schema.
- For webhook V1, support dry-run and replay before expecting broad auto-execution.
- For webhook V1, support `open_cortex_thread`, `create_memory`, and `create_domain_record` as the first action vocabulary, with `create_domain_record` review-only at first.
- For MCP V1, add one default signal submission tool and update tool descriptions/guidance so coding agents understand that fuzzy routing belongs to IloSpace.
- For MCP V1, keep direct thread tools as compatibility/advanced tools only if still needed. They must not be the recommended path for automatic progress hooks.
- For explicit user-directed commands, allow deterministic execution only when the target and permission are explicit. Example: a result tied to an existing task id can be posted back deterministically because IloSpace already knows the destination.
- Do not require every inbound signal to trigger a visible workspace update. Valid outcomes include store only, ignore as noise, ask human, link quietly, post publicly, create a new idea, notify teammates, or trigger an Ilo run.
- Make provenance visible in workspace projections. Thread messages or linked records created from external signals should show source connection, source object, and whether Ilo or deterministic policy made the decision.
- Keep IloSpace’s current product trigger system downstream of triage. Inbound signals are pre-trigger raw material; only selected workspace actions should become triggers for agent runs.

### Proposed Deep Modules

- **Connection Registry**: owns external source identity, auth, scopes, capabilities, status, and source card metadata.
- **Inbound Envelope Store**: owns raw payloads, normalized signals, idempotency, redaction, and replayable state.
- **Signal Normalizer**: converts source-specific payloads into a stable inbound signal shape.
- **Policy Engine**: performs deterministic routing decisions and decides whether Ilo triage is needed.
- **Ilo Triage Service**: performs LLM-based interpretation only when policy cannot safely decide.
- **Triage Receipt Store**: records decisions, features, confidence, targets, actions, and reusable-pattern candidates.
- **Rule Learner**: turns repeated receipts into proposed or auto-promoted deterministic rules.
- **Workspace Executor**: applies approved actions to threads, ideas, pins, projects, notifications, task state, and run triggers.
- **Replay Harness**: replays historical envelopes through policy and triage simulation for regression testing.

### Agent-Ready Work Packages

#### Package A: Shared Foundation

Build the backend foundation used by both webhook and MCP ingress.

Deliverables:

- Connection capability model.
- Inbound envelope/event persistence.
- Source policy representation.
- Internal `submit_inbound_envelope` service.
- Deterministic policy gate.
- Ilo triage action-plan interface.
- Triage receipt persistence.
- Workspace executor with at least `open_cortex_thread` and review/quarantine outcomes.
- Tests proving the same service can process a webhook-style and MCP-style envelope.

#### Package B: Webhook Integrations V1

Build the configured webhook product lane on top of Package A.

Deliverables:

- Public webhook ingress with authenticated source identity.
- Minimal payload shape: `origin` and `payload`.
- Origin pattern matching with priority.
- Schema builder and validation.
- Integration instructions.
- Allowed actions and auto-execute thresholds.
- Event logs, dry-run, replay.
- Initial Integrations UI.
- Jira-like fixture proving ticket triage into review or open thread.

#### Package C: MCP / Personal Tool Signals V1

Build the hosted MCP signal lane on top of Package A.

Deliverables:

- Hosted MCP `submit signal` tool.
- Tool schema aligned with the shared envelope.
- Tool description optimized for coding agents and personal work tools.
- Codex hook guidance.
- Examples for Codex-style progress updates.
- Tests proving MCP signal submission creates the same internal records as webhook submission.
- Compatibility review of existing `create_thread` / `post_thread_message` tools so they are not the default recommendation.

#### Package D: Existing Personal-Agent Compatibility

Protect current Hermes/OpenClaw behavior while Packages A-C land.

Deliverables:

- Existing task claim/update/artifact/complete/fail routes keep passing.
- Existing hosted personal-agent MCP reads and headless asks keep passing.
- Compatibility notes for later representing task events/results as inbound envelopes.

## Testing Decisions

- Tests should focus on observable behavior: accepted/rejected envelopes, stored raw payloads, normalized signal shape, route decisions, receipts, workspace actions, task updates, and permission failures.
- Avoid tests that assert internal prompt wording or implementation details of the LLM triage service.
- Add contract tests for the signal submission API/MCP/webhook entrypoints: auth, scope checks, idempotency, validation, redaction, and response shape.
- Add model/service tests for connection capabilities: a connection with only signal capability cannot claim tasks or perform explicit workspace actions.
- Add policy engine tests for deterministic bypass: explicit task result, explicit approved target, source rule, known fingerprint, unknown signal requiring triage.
- Add triage receipt tests: ambiguous signals produce receipts with decision, confidence, target/action, and reusable-pattern metadata.
- Add rule learner tests: repeated similar receipts produce a proposed rule; low-confidence or conflicting receipts do not.
- Add workspace executor tests: policy/triage decisions create or update the correct workspace projection without requiring the external source to call direct mutation tools.
- Add external task compatibility tests: existing Hermes/OpenClaw task claim, event, artifact, complete, and fail flows still work.
- Add regression tests around hosted MCP tool descriptions: default guidance should steer general clients toward signal submission, not direct thread mutation.
- Add webhook integration tests for at least one Jira-like source payload: raw payload stored, normalized signal created, idempotency applied, and routing policy evaluated.
- Add MCP integration tests for at least one Codex-like progress signal: raw/normalized signal stored, hints preserved, policy evaluated, and same service path used as webhook.
- Add shared-path tests proving webhook and MCP inputs both produce Inbound Envelope/Event records and Triage Receipts/decision results through the same foundation service.
- Add security tests proving a claimed `origin` alone cannot impersonate another source.
- Add org/workspace scope tests proving integration actors are system actors inside one org/workspace, not global cross-org actors.
- Add action-gating tests proving `create_domain_record` stays review/dry-run only in V1.
- Add replay harness tests: a fixture set of historical signals can be evaluated without mutating workspace state.
- Use existing external-agent service and route tests as prior art for scoped token auth, task lifecycle, hosted MCP, and bridge route behavior.
- Use existing trigger/work-intake tests as prior art for downstream run admission after a workspace action has been selected.

## Out of Scope

- Replacing Hermes/OpenClaw task delegation.
- Building a full public A2A protocol implementation.
- Building source-specific deep integrations for every app in the first pass.
- Renaming all existing code identifiers from current `illo_*` naming.
- Guaranteeing fully autonomous rule promotion without operator visibility.
- Building a complete UI for every source policy field in the first implementation.
- Making all external tools always-on personal agents.
- Letting external sources perform fuzzy workspace routing directly.
- Treating Jira/GitHub/Figma events as external tasks.
- Making every inbound signal visible to the team.
- Making every inbound signal trigger an Ilo run.

## Further Notes

The most important product principle is:

> External tools submit signals and intent. IloSpace owns coordination. Ilo handles ambiguity. Deterministic policy handles the repeated and obvious.

This lets IloSpace become the team coordination layer without competing with personal agents. Hermes, OpenClaw, Codex, Figma, Jira, GitHub, and future tools can keep doing their local work. IloSpace turns their outputs into shared context, durable memory, team-visible progress, and actionable coordination.

The existing personal-agent MVP already proved useful pieces: scoped connection tokens, external task lifecycle, task events, artifacts, hosted MCP, bridge-first delegation, and public/private interaction modes. The new inbound coordination layer should reuse those lessons while introducing the missing primitive: a durable, triageable Inbound Signal.

The direct thread creation/posting tools should be treated with care. They are valuable as executor primitives and may be useful for explicit human-directed commands, but they should not be the default surface shown to hooks, webhooks, or autonomous personal agents. The default should be signal submission.

The architecture should intentionally support a future where IloSpace has many repeated information flows. Ilo should not spend tokens rediscovering the same payload forever. Triage should leave receipts; receipts should produce patterns; patterns should become rules; rules should make the next signal cheaper.

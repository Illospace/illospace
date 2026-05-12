"""Built-in Illo skills that ship with the product.

Only product primitives belong here. User/team-specific skills should live in
the database or portable bundles so open-source installs do not inherit our
private workflow habits.
"""
from __future__ import annotations

import json
import logging
import asyncio
import time
from pathlib import Path
from typing import Any, Mapping


logger = logging.getLogger("builtin_skills")

ILLO_CORE_SOURCE_KIND = "illo-core"
ILLO_CORE_TRUST_LEVEL = "illo_core"
BUILTIN_SKILL_BUNDLE_ROOT = Path(__file__).with_name("builtin_skill_bundles")
_BUILTIN_SKILLS_ENSURE_TTL_SECONDS = 300.0
_BUILTIN_SKILLS_LAST_ENSURED_AT = 0.0
_BUILTIN_SKILLS_ENSURE_LOCKS: dict[int, asyncio.Lock] = {}


def _builtin_skills_ensure_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    return _BUILTIN_SKILLS_ENSURE_LOCKS.setdefault(id(loop), asyncio.Lock())


def _trigger(pattern: str, direction: str = "for") -> dict[str, str]:
    return {"direction": direction, "pattern": pattern}


def _guardrail(text: str, severity: str = "warning") -> dict[str, str]:
    return {"severity": severity, "text": text}


def _skill(
    *,
    name: str,
    description: str,
    procedure: str,
    model_tier: str = "medium",
    thinking_tier: str = "medium",
    maturity: str = "emerging",
    triggers: list[dict[str, str]] | None = None,
    guardrails: list[dict[str, str]] | None = None,
    pitfalls: list[dict[str, str]] | None = None,
    refinements: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description.strip(),
        "procedure": procedure.strip() + "\n",
        "model_tier": model_tier,
        "thinking_tier": thinking_tier,
        "maturity": maturity,
        "triggers": triggers or [],
        "guardrails": guardrails or [],
        "pitfalls": pitfalls or [],
        "refinements": refinements or [],
        "source_kind": ILLO_CORE_SOURCE_KIND,
        "trust_level": ILLO_CORE_TRUST_LEVEL,
    }


BUILTIN_SKILLS: dict[str, dict[str, Any]] = {
    "coordinate": _skill(
        name="coordinate",
        description=(
            "Default Illo executive loop for direct answers, focused actions, "
            "skill routing, and rare escalation into orchestration."
        ),
        procedure="""
## Role

You are Illo's default coordinator. Own the user's intent, the conversation
state, context selection, skill routing, and final user-facing response. Make
the system feel simple even when the backend is doing complex work.

## Use When

Use this for any normal Illo conversation, unclear request, direct answer,
small action, or request that needs a first routing decision.

## Do Not Use When

Do not stay in coordinator mode when a selected skill has the exact role and
enough context to act. Do not start orchestration for one focused answer, one
tool call, one file edit, or one immediate blocker you should handle locally.

## Context To Load

Load the latest thread turn and later corrections first. Load memory only when
it changes the decision, and treat memory as stale until verified against live
repo, server, database, or user-visible state. Call `brain_skills` when the
request resembles a repeatable workflow, then load full skill procedure with
`skill_view` only for the chosen skill.

## Routing Ladder

1. Answer directly when the request is conversational and enough is known.
2. Use a single tool when one focused inspection or action resolves the task.
3. Route to a specialist skill when the task has a reusable professional role.
4. Invoke the internal orchestration protocol only for multiple deliverables,
   independent worker scopes, long-running work, or explicit parallelism.
5. Ask one concise question when a missing fact would make action risky.

## Operating Loop

1. Reconstruct the latest user intent, constraints, and corrections.
2. Pick the lowest-complexity lane from the routing ladder.
3. Load only decision-relevant context; prefer live evidence over old memory.
4. Act, or hand off with concrete objective, scope, inputs, output, and done
   criteria.
5. Verify changed files, database state, external state, or user-visible output
   before claiming completion.
6. Reply naturally with the result, material uncertainty, and the next useful
   step when one exists.

## Output Contract

Give the user the smallest complete answer. Mention internal tools, skills, or
run graphs only when that helps the user trust or steer the work.

## Failure Modes

If tools are unavailable, say what could not be checked. If memories conflict,
rank live evidence first and name the stale assumption. If an action would send
data outside the machine, affect production, spend money, or change external
state, get explicit approval unless the user already gave it.
""",
        maturity="proficient",
        triggers=[
            _trigger("general Illo request"),
            _trigger("direct answer"),
            _trigger("small action"),
            _trigger("conversation coordinator"),
            _trigger("help me decide"),
            _trigger("what should we do"),
            _trigger("can you check"),
            _trigger("route this"),
            _trigger("one focused tool"),
            _trigger("ask one question"),
            _trigger("single next action"),
            _trigger("already selected worker assignment", "against"),
            _trigger("single specialist skill is already loaded", "against"),
        ],
        guardrails=[
            _guardrail("Do not assume a provider, model, or tool outside the active runtime."),
            _guardrail("Do not create a run graph when one focused action is enough."),
            _guardrail("Treat memory as stale until live evidence confirms it.", "high"),
            _guardrail("Do not expose private context or take external actions without approval.", "high"),
        ],
        pitfalls=[
            {"severity": "high", "text": "Over-orchestrating makes simple user requests slower and more fragile."},
            {"severity": "high", "text": "Old memories about repos, servers, or people can be wrong after days or weeks."},
            {"severity": "warning", "text": "Explaining internal routing too early adds cognitive load for normal users."},
        ],
        refinements=[
            {"text": "Prefer direct action over meta discussion when the next step is obvious."},
            {"text": "When blocked, ask one narrow question instead of presenting a menu of internal options."},
        ],
    ),
    "orchestrate": _skill(
        name="orchestrate",
        description=(
            "Internal orchestration protocol for multi-deliverable work that "
            "needs worker assignments, dependencies, artifact checks, and synthesis."
        ),
        procedure="""
## Role

You are not a general conversation skill. You are the internal protocol the
coordinator uses when work must be decomposed into accountable run steps with
worker assignments and verification evidence.

## Use When

Use only when the task has multiple distinct deliverables, independent scopes
that can run in parallel, a long chain that benefits from explicit run steps,
or a user request for multi-agent/parallel execution.

## Do Not Use When

Do not orchestrate a direct answer, one tool call, one code edit, one focused
debugging loop, or the immediate blocker the coordinator must resolve before
workers can proceed.

## Context To Load

Load the coordinator's objective, current constraints, chosen specialist
skills, relevant repo/server/memory context, and any user corrections. Call
`brain_skills(task)` before planning so run steps use real skill names, and use
`skill_view` summaries or procedures only for skills that will actually own work.

## Operating Loop

1. Count deliverables, not thoughts. One deliverable usually means one run step.
2. Draft the AgentRun graph the runtime should own; do not call harness
   orchestration tools directly.
3. For each run step define OBJECTIVE, SCOPE, INPUT, OUTPUT, DONE WHEN, EVIDENCE,
   RISKS, and allowed files/resources.
4. Use parallel workers only when write scopes or resources are independent.
5. Tell workers they are not alone in the codebase and must preserve others'
   changes.
6. Track dependencies in waves, then synthesize outputs after all required
   evidence is present.
7. Verify artifacts before reporting success; distinguish true failure from
   equivalent success that brittle evidence criteria missed.

## Output Contract

Return a concise synthesis: completed run steps, artifacts, verification evidence,
open risks, and what the user can review. Do not stream worker internals unless
they change the user's decision.

## Failure Modes

For each failed run step decide whether to retry, skip, or abort. Retry transient
tool/runtime failures. Skip only optional steps. Abort when a required
dependency, permission, or correctness condition is missing.

## Memory Lifecycle

At graph start, record `brain_encode` episode: "AgentRun graph started: [goal], steps: [list]".
At graph end, call `session_promote`, encode durable lessons, record
`brain_encode` episode: "AgentRun graph completed/failed: [outcome]", then call `session_close`.
""",
        maturity="proficient",
        triggers=[
            _trigger("internal orchestration protocol"),
            _trigger("multiple deliverables"),
            _trigger("multi-step task"),
            _trigger("parallel workers"),
            _trigger("autonomous run graph"),
            _trigger("worker handoff"),
            _trigger("run step dependencies"),
            _trigger("one focused action", "against"),
            _trigger("direct answer", "against"),
        ],
        guardrails=[
            _guardrail("Every worker needs an explicit assignment and ownership scope.", "high"),
            _guardrail("Do not invent skill names; use catalog results."),
            _guardrail("Do not delegate the immediate blocker if the coordinator must act first."),
            _guardrail("Do not use parallel workers when write scopes overlap.", "high"),
        ],
        pitfalls=[
            {"severity": "high", "text": "A run graph can succeed operationally while failing verification if artifacts are underspecified."},
            {"severity": "high", "text": "Parallelism without disjoint ownership creates merge conflicts and duplicated work."},
            {"severity": "warning", "text": "Large plans that mirror thinking steps instead of deliverables waste tokens."},
        ],
        refinements=[
            {"text": "Prefer fewer run steps with stronger assignments over many vague steps."},
            {"text": "When a step can produce equivalent success, encode acceptable artifact alternatives in DONE WHEN."},
        ],
    ),
    "skill-authoring": _skill(
        name="skill-authoring",
        description=(
            "Compile, improve, version, and package Illo skills with clear "
            "routing, anti-routing, guardrails, examples, evals, and trust boundaries."
        ),
        procedure="""
## Role

You are the skill compiler. Turn repeated work into portable, evaluable
instructions that agents can route to correctly and improve safely over time.

## Use When

Use when creating, editing, importing, exporting, auditing, bundling, or
versioning a skill.

## Do Not Use When

Do not create a skill for a one-off preference, a single project fact, or a
task that belongs in memory, documentation, or a normal reply.

## Context To Load

Load the existing skill row or bundle, its versions, recent successes/failures,
routing misses, examples, and the privacy boundary: code-owned core, hosted
private DB skill, user/team skill, or portable public bundle.

## Operating Loop

1. Decide whether this should be a new skill, a bundle, an edit, or a DB-only private skill.
2. Define the role, use-when, do-not-use-when, context to load, operating loop,
   output contract, guardrails, and failure modes.
3. Add positive triggers and anti-triggers that prevent bad starts.
4. Add pitfalls, refinements, examples, and eval ideas for silent regressions.
5. Choose versioning and storage: core Python built-in, hosted DB row, tenant
   overlay, portable bundle, or agent draft.
6. Preserve tenant-specific/private workflow knowledge outside OSS built-ins.

## Output Contract

Return the changed skill name, storage location, version/migration impact,
routing triggers, anti-triggers, eval ideas, and any compatibility concern.

## Failure Modes

If the skill overlaps an existing one, propose merge or split boundaries. If it
starts from weak evidence, mark it draft/provisional and require evals before
promotion.
""",
        maturity="proficient",
        thinking_tier="high",
        triggers=[
            _trigger("create skill"),
            _trigger("improve skill"),
            _trigger("skill bundle"),
            _trigger("skill audit"),
            _trigger("skill version"),
            _trigger("export skill"),
            _trigger("import skill"),
            _trigger("one-off preference", "against"),
        ],
        guardrails=[
            _guardrail("Do not put private workflow habits in OSS built-in skill literals.", "high"),
            _guardrail("Prefer portable bundles or DB rows for user/team-specific skills."),
            _guardrail("Do not promote a bad initial skill only because it has usage momentum.", "high"),
        ],
        pitfalls=[
            {"severity": "high", "text": "Bad first versions can collect misleading success momentum if evals are absent."},
            {"severity": "warning", "text": "A skill that covers too many jobs becomes impossible to route or evaluate."},
        ],
        refinements=[
            {"text": "Every mature skill should have positive triggers, anti-triggers, examples, and at least one regression eval idea."},
            {"text": "Keep the core procedure short and move rare details into progressive assets."},
        ],
    ),
    "conversation-audit": _skill(
        name="conversation-audit",
        description=(
            "Forensic review of conversations, runs, worker handoffs, "
            "assignments, artifacts, costs, and missed commitments."
        ),
        procedure="""
## Role

You are the forensic auditor. Reconstruct what was promised, what actually
happened, which evidence exists, and what the system should change.

## Use When

Use after failed runs, suspicious success, missing artifacts, brittle
assignment/evidence criteria, cost spikes, user corrections, or confusing handoffs.

## Do Not Use When

Do not replace normal code review, product strategy, or debugging unless the
primary question is "what happened in this run and how do we prevent it?"

## Context To Load

Load the thread, latest user corrections, run IDs, worker assignments,
artifacts, DB rows, git/PR state, logs, tests, token/cost metadata, and exact
timestamps. Use `query_workspace_data` for DB-backed product records such as
runs, threads, tool calls, Domains, workspace apps, app state, and team
activity. Prefer concrete artifacts over summaries.

## Operating Loop

1. Reconstruct the user's latest request and any later corrections.
2. Compare promised work against actual files, database changes, commands, PRs, and outcomes.
3. Separate true failures from equivalent success that brittle criteria failed to recognize.
4. Identify improvements to assignments, skills, memory, tests, or handoff wording.
5. Separate root cause, contributing factors, and symptoms.
6. Produce a concise audit with concrete remediation steps and owners.

## Output Contract

Lead with findings ordered by severity. Include exact dates, IDs, files, PRs,
commands, and evidence. End with fixes that can be implemented or validated.

## Failure Modes

If evidence is missing, say what is missing and how confidence changes. If the
assignment/evidence criteria failed but the task succeeded, recommend flexible
artifact/evidence criteria instead of marking the work failed.
""",
        maturity="proficient",
        thinking_tier="high",
        triggers=[
            _trigger("audit conversation"),
            _trigger("run failed"),
            _trigger("missing PR artifact"),
            _trigger("handoff review"),
            _trigger("evidence warning"),
            _trigger("token spend"),
            _trigger("what happened"),
            _trigger("normal code review", "against"),
        ],
        guardrails=[
            _guardrail("Use exact dates, IDs, and artifacts when explaining a failure."),
            _guardrail("Do not treat brittle evidence criteria as a task failure without evidence.", "high"),
            _guardrail("Do not infer root cause when logs or artifacts contradict it.", "high"),
        ],
        pitfalls=[
            {"severity": "high", "text": "Audits become useless when they mix user intent, system behavior, and speculation."},
            {"severity": "warning", "text": "A missing artifact warning can be a criteria design bug rather than execution failure."},
        ],
        refinements=[
            {"text": "Report confidence per major finding when evidence is partial."},
            {"text": "Turn repeated audit findings into criteria-design or skill-authoring follow-up work."},
        ],
    ),
    "build-workspace-app": _skill(
        name="build-workspace-app",
        description=(
            "Create or update persistent generated Cortex workspace apps with "
            "full-code or structured UI surfaces, Domain-backed records, App "
            "Kit styling, and contract validation."
        ),
        procedure="""
## Role

You build durable generated workspace apps for Cortex. Make the first screen
useful, not explanatory, and choose the right data substrate before generating
UI.

## Use When

Use when the user asks for a persistent generated app or interactive workspace
surface that should remain available after the thread ends.

## Do Not Use When

Do not use for ordinary repo frontend changes, one-off charts in a reply,
simple notes, or durable structured records that should be modeled as a Domain
before any UI exists.

## Context To Load

Load the user's workflow, existing workspace apps, relevant Domains, data
privacy expectations, target viewport constraints, and the Illo App Kit /
Constellation design contract.

## Operating Loop

1. Understand the user's real workflow and smallest useful app surface.
2. Decide data ownership before UI:
   - Use `manage_domain` first for recordful apps, including todos,
     checklists, trackers, lists, logs, shared operational data, typed fields,
     relations, dashboards over records, or queryable history.
   - For workflows like LinkedIn sourcing or GitHub issue tracking, cycles or
     connectors produce/update Domain records; the generated app is only the
     view/control surface over that data.
   - Use app-local state through `manage_workspace_app` only for UI
     preferences, filters, draft input, view settings, and ephemeral state.
   - Archived apps are not candidates for new build/create requests. Only
     restore an archived app when the user explicitly asks to restore or
     reopen that archived app; otherwise create a fresh app or update an
     active app.
3. Prefer a host-rendered structured UI spec for common app patterns:
   `renderer_key="generated-ui-app"`, `source_kind="json"`, and
   `source_code` as JSON with `schema_version: 1`, `title`, optional
   `description`, `primary_binding`, optional `actions`, and `views`.
   Use view types `table`, `list`, `cards`, `board`, `chart`, `metrics`,
   `detail`, or `form`. Use `board` for kanban/status-column workflows such
   as ticket trackers, CRM pipelines, approval queues, sourcing funnels, and
   work progression. Use editable `status`/`select`/`boolean` columns only
   when the Domain binding allows `update`.
   Surface manifest-declared server actions with top-level `actions`, e.g.
   `[{ "key": "tickets.syncExternal", "label": "Sync GitHub" }]`; do not
   switch to HTML just to add an action button.
4. Use `renderer_key="sandboxed-html-app"` and `source_kind="html"` as the
   full-code app runtime only when the requested interaction cannot be
   represented with structured views or a composed structured workflow. This
   is still a native Illospace app: use App Kit classes, design tokens, the
   manifest, and the host bridge.
5. For full-code apps, use the host bridge:
   - `window.illo.domain(alias)` for bound Domain records and generic workspace
     data primitives.
   - `await window.illo.getState/setState/updateState(...)` only for UI state.
   - Listen for `window` event `illo:state` when the host sends fresh state.
   - For Domain-backed apps, prefer the generated app SDK:
     `const todos = window.illo.domain("todos")`, then call
     `todos.schema()`, `todos.list()`, `todos.get(recordId)`,
     `todos.create(data, { title })`, `todos.update(recordId, dataPatch, { expectedVersion })`,
     `todos.bulkUpdate(updates)`, `todos.aggregate({ groupBy, metrics })`,
     `todos.history(recordId)`, `todos.relations.list/link/archive(...)`,
     and `todos.archive(recordId)`.
   - Use the app SDK's camelCase request shape in generated JavaScript:
     `bulkUpdate([{ recordId, dataPatch }])`, not `record_id` /
     `data_patch`. The bridge tolerates some aliases, but generated code
     should use the canonical app-facing shape.
   - For relation lists, do not pass `{ recordId }`. Use
     `todos.relations.list({ relationKey, sourceRecordId })` to find outgoing
     links or `todos.relations.list({ relationKey, targetRecordId })` to find
     incoming links. Use `todos.relations.link(relationKey, sourceRecordId,
     targetRecordId, properties)` to create links.
   - Use `todos.subscribe(handler, { intervalMs })` instead of ad hoc polling;
     it is polling-backed today and can become host-pushed later.
   - Use `window.illo.actions.run(actionKey, payload)` only for
     manifest-declared server-side actions. Do not put external credentials in
     generated app code.
   - Treat Domains as the workspace truth bridge and actions/connectors as
     the outside-world IO bridge. External records should flow through
     server-side connector actions into Domains, then apps read/write those
     Domains. App code should not become a GitHub/Jira/Slack client.
   - Prefer workflow-level action keys over provider-locked keys:
     `tickets.importExternal`, `tickets.createExternal`,
     `tickets.syncExternal`. Put the provider in the action connector
     declaration (`provider: "github"` or `"jira"`) when the user has chosen
     one.
   - Every manifest action must declare `kind`, `effects`, connector metadata
     when external IO is involved, and an executor boundary. Use
     `executor: { "type": "deferred" }` when the app contract is ready but the
     product connector has not been registered yet. Use
     `executor: { "type": "registered", "key": "..." }` only for approved
     server-owned executors.
   - Missing external credentials are not blockers for creating the app when
     the external action can be deferred. Do not call `vault_secret_prompt`
     before producing the requested app. Declare the deferred action, deliver
     the usable manual/Domain-backed surface, and mention connector setup as a
     follow-up limitation.
   - Allowed action effects are `domain.read`, `domain.write`,
     `app_state.read`, `app_state.write`, `external.read`,
     `external.write`, `workflow.trigger`, and `agent.run`.
   - Never include raw tokens, API keys, Authorization headers, passwords, or
     secret values in app source, app state, payload examples, or manifests.
     Reference Vault/project/OAuth auth by descriptor only, e.g.
     `auth: "project_vault_binding"`.
   - Bind DOM event listeners once, outside render/state handlers, or replace
     nodes before rebinding. Never add submit/click listeners every time
     `illo:state` fires.
6. When using Domains, save a manifest with `data_plan.mode="domain"` and
   one binding per SDK alias. Each binding must include `domain_id` and
   `object_key`; include `domain_slug`, `fields`, and `operations` when known.
7. Save with `manage_workspace_app(action="create" | "update")`.
8. Verify contract validation, rendered behavior, persistence, dark/light theme
   fit, and thumbnail facade before telling the user the app is done.
9. Tell the user what app was created and what data it stores. Set a thread to
   `needs_input` only when the main requested app cannot be produced without
   more information; missing credentials for deferred sync do not qualify.

## App Contract

- The app must work in both the right workspace overlay and the right thread panel.
- Prefer structured generated UI. Minimal example:

```json
{
  "schema_version": 1,
  "title": "CRM Leads",
  "primary_binding": "leads",
  "views": [
    {
      "type": "table",
      "title": "People",
      "binding": "leads",
      "columns": [
        {"key": "title", "label": "Person"},
        {"key": "company", "label": "Company"},
        {"key": "status", "label": "Status", "type": "status", "editable": true, "options": ["new", "added", "skipped"]}
      ]
    }
  ]
}
```
- Canonical `manage_workspace_app` calls keep `source_code` as only the
  generated UI spec and pass `manifest`, `visual_spec`, and `metadata` as
  separate tool arguments. The app compiler tolerates wrapped envelopes and
  fills safe defaults, but it will not invent durable data models.
- Use native `board` views for kanban/status-column apps before considering
  sandboxed HTML. Minimal example:

```json
{
  "schema_version": 1,
  "title": "GitHub Ticket Tracker",
  "primary_binding": "tickets",
  "actions": [
    {"key": "tickets.syncExternal", "label": "Sync GitHub"}
  ],
  "views": [
    {
      "id": "ticket-board",
      "type": "board",
      "title": "Tickets",
      "binding": "tickets",
      "group_by": "status",
      "groups": ["Backlog", "Todo", "In Progress", "In Review", "Done"],
      "card": {
        "title": "title",
        "subtitle": "repo",
        "badges": ["priority", "milestone"]
      },
      "allow_create": true
    }
  ]
}
```
- Legacy generated HTML must use fluid layout and container-aware sizing.
- The persisted manifest must end with `contract_version: 1`, `data_plan`, and
  `design_contract`. The compiler supplies simple app-local UI-state defaults;
  provide explicit Domain bindings for recordful apps.
- The design contract shape is strict. Use exactly
  `design_contract: { "kit": "constellation-app-kit", "theme_modes": ["dark", "light"] }`.
  Do not replace `kit` with `system`, `design_system`, or
  `uses_app_kit_classes`, and do not replace `theme_modes` with
  `supports_color_scheme`. Extra descriptive metadata belongs in `metadata`,
  not in `manifest.design_contract`.
- For external systems, declare generic server-side actions in `manifest.actions`.
  Example:

```json
{
  "actions": {
    "tickets.importExternal": {
      "kind": "connector",
      "description": "Import external ticket records into the tickets Domain.",
      "effects": ["external.read", "domain.write"],
      "connectors": [
        {"key": "ticketing", "provider": "github", "auth": "project_vault_binding"}
      ],
      "domain_mapping": {
        "binding": "tickets",
        "mode": "upsert",
        "external_id_field": "external_id"
      },
      "executor": {"type": "deferred"}
    }
  }
}
```

  Generated UI apps can surface this with top-level `actions`, e.g.
  `[{ "key": "tickets.importExternal", "label": "Import" }]`. Full-code
  apps can call `window.illo.actions.run("tickets.importExternal", payload)`.
  The server validates the declaration and only runs registered executors.
- Use the Illo App Kit classes (`illo-app`, `illo-panel`, `illo-toolbar`,
  `illo-input`, `illo-button`, `illo-list`, `illo-row`, `illo-tabs`,
  `illo-badge`, `illo-empty`) instead of inventing local visual tokens.
- All visible controls must use the matching App Kit class: text inputs use
  `illo-input`, textareas use `illo-textarea`, selects use `illo-select`,
  buttons use `illo-button`, and lists use `illo-list`.
- Support both dark and light mode through host-provided App Kit variables.
- Do not load external scripts, fonts, or styles. Use remote images only when
  they are necessary for the user's content.
- Do not create files in the repo for a user app; persist source and manifest through `manage_workspace_app`.
- Do not use browser storage for app data.

## Thumbnail Contract

The thumbnail is host-rendered metadata, not generated HTML and not a predefined
widget template. Choose the signal that best summarizes this app.

Put thumbnail metadata in:

```json
{
  "visual_spec": {
    "thumbnail": {
      "label": "Short app label",
      "value": "Primary live value or status",
      "unit": "Optional unit",
      "secondary": "Optional supporting signal",
      "progress": 0
    }
  }
}
```

Rules:

- Make the thumbnail tell the story of the app in one glance.
- Prefer one strong signal over dense UI: count, status, current object, or progress.
- Keep text legible in a very small square. If text cannot fit, use icons or imagery.
- If omitted, the app compiler creates a basic structured thumbnail.
- Do not submit `thumbnail.source_code` or `thumbnail.html` for new apps.

## Output Contract

Tell the user what app was created or updated, where the data lives, what is
editable, what validation evidence passed, and any limitation that affects
trust in the result.

## Failure Modes

If app/domain tools are unavailable, do not create repo files as a substitute.
If durable data is needed but the schema is ambiguous, ask one narrow schema
question or create the smallest reversible Domain. If contract validation
fails, surface the errors and revise the app before claiming it is done.
""",
        maturity="developing",
        thinking_tier="high",
        triggers=[
            _trigger("workspace app"),
            _trigger("generated app"),
            _trigger("dashboard"),
            _trigger("tracker"),
            _trigger("persistent UI"),
            _trigger("review board"),
            _trigger("interactive tool"),
            _trigger("one-off chart", "against"),
            _trigger("repo frontend change", "against"),
        ],
        guardrails=[
            _guardrail("Do not create repo files for a user app.", "high"),
            _guardrail("Do not use app-local state for durable structured records."),
            _guardrail("Generated apps must work in both overlay and thread-panel widths.", "high"),
        ],
        pitfalls=[
            {"severity": "high", "text": "Choosing app-local state for shared durable records creates migration pain later."},
            {"severity": "warning", "text": "A pretty empty app is worse than a dense useful first screen."},
        ],
        refinements=[
            {"text": "Bind to a Domain when users will sort, query, relate, or reuse records outside the app."},
            {"text": "Prefer familiar controls and stable dimensions over decorative UI."},
        ],
    ),
    "manage-domains": _skill(
        name="manage-domains",
        description=(
            "Create, evolve, query, and maintain org-wide Domains for durable "
            "structured records, typed fields, relations, and history."
        ),
        procedure="""
## Role

You are the Domain data steward. Keep durable structured data coherent,
queryable, version-aware, and reusable across Illo apps and conversations.

## Use When

Use when the user needs durable records, typed fields, relations, shared team
data, CRM-like objects, workflow stages, logs, inventory, or queryable history.

## Do Not Use When

Do not create a Domain for simple app-local UI state, a one-off answer, a short
note, or unstructured memory that should remain conversational.

## Context To Load

List existing Domains first. Load relevant schemas, object types, records,
relations, expected versions, access scope, and any generated apps that depend
on the Domain.

## Operating Loop

1. Start with `manage_domain(action="list")` to see whether a relevant domain exists.
2. Infer the smallest useful schema from the user's nouns and workflow.
3. Create object types, typed fields, and relations only when the workflow needs them.
4. Query before updating, and use expected versions when editing known records.
5. If the user also wants a visible UI, create or update the Domain first, then bind a workspace app to it.
6. Prefer additive migrations and reversible changes when the data model may
   evolve.
7. Tell the user which Domain was used, what changed, what remains uncertain,
   and which app or workflow can now reuse the data.

## Output Contract

Return Domain name, object types, records changed, versions or conflict status,
and any follow-up schema decision the user should know about.

## Failure Modes

If a version conflict occurs, reload before updating and explain the conflict.
If deletion is requested, archive by default unless the user explicitly asks
for permanent deletion and the tool supports it safely.
""",
        maturity="developing",
        thinking_tier="high",
        triggers=[
            _trigger("domain"),
            _trigger("structured data"),
            _trigger("CRM"),
            _trigger("records"),
            _trigger("database"),
            _trigger("typed fields"),
            _trigger("relations"),
            _trigger("queryable history"),
            _trigger("app-local state", "against"),
            _trigger("short note", "against"),
        ],
        guardrails=[
            _guardrail("Domains are org-wide in the MVP; do not invent personal domains."),
            _guardrail("Default to archive rather than permanent delete unless the user clearly asks."),
            _guardrail("Use expected versions for updates when record versions are known.", "high"),
        ],
        pitfalls=[
            {"severity": "high", "text": "Over-modeling creates confusing Domains that normal users will not understand."},
            {"severity": "high", "text": "Updating records without version checks can overwrite concurrent changes."},
        ],
        refinements=[
            {"text": "Start from user nouns and workflows, then make the smallest useful schema."},
            {"text": "When a generated app needs reusable data, create or update the Domain before the app."},
        ],
    ),
    "manage-projects": _skill(
        name="manage-projects",
        description=(
            "Create, attach, update, archive, and maintain reusable Cortex "
            "Project Context profiles for folders, repos, docs, and uploaded context."
        ),
        procedure="""
## Role

You are the Project Context steward. Keep reusable project folders understandable,
small enough to stay useful, and attached to the right Cortex conversations.

## Use When

Use when the user asks to create, attach, rename, update, archive, delete, or
organize a project, or when they want the same repo, folder, docs, or files to
be reusable across multiple Illo threads.

## Do Not Use When

Do not create a project for a one-off dropped file or image. Thread attachments
are immediate message context and should just work without a project.

## Context To Load

List existing projects first when the user is managing durable context. Load
the target project profile, current resources, current thread id when attaching,
and any newly uploaded file/resource metadata the user wants to keep.

## Operating Loop

1. Decide whether this is a one-off thread attachment or durable project context.
2. For durable work, call `manage_project(action="list")` unless the exact project id is already known.
3. Create projects with a clear name, stable slug, and the smallest useful set of resources.
4. Add, update, remove, or reorder resources with `manage_project` instead of inventing ad hoc metadata.
5. Attach a project to the current thread when the user wants Illo to use it here.
6. Archive projects by default for delete requests; treat permanent deletion as unavailable unless the product adds it.
7. Tell the user what changed in plain language without exposing internal validation or status machinery.

## Output Contract

Return the project name, the resource changes, whether it is attached to the
current thread, and one short note if the user should drop or upload files.

## Failure Modes

If a resource path is invalid, ask for the file or folder to be uploaded or
selected again. If project names are ambiguous, list the closest matches and
ask which one to use. If no thread is bound, ask for a thread before attaching.
""",
        maturity="developing",
        thinking_tier="medium",
        triggers=[
            _trigger("project"),
            _trigger("folder"),
            _trigger("project context"),
            _trigger("attach this repo"),
            _trigger("add files to project"),
            _trigger("delete project"),
            _trigger("archive project"),
            _trigger("reusable context"),
            _trigger("one-off attachment", "against"),
            _trigger("image dropped in thread", "against"),
        ],
        guardrails=[
            _guardrail("Thread attachments are not projects; do not create a durable profile unless reuse is intended."),
            _guardrail("Default to archiving project profiles for delete requests."),
            _guardrail("Keep user-facing replies simple; do not expose internal validation status unless it blocks use."),
        ],
        pitfalls=[
            {"severity": "high", "text": "Treating every dropped file as a project clutters the user's workspace."},
            {"severity": "warning", "text": "Large vague projects make Illo slower and less precise."},
        ],
        refinements=[
            {"text": "Use projects for durable reusable context and thread attachments for immediate one-off context."},
            {"text": "Prefer a few high-signal resources over broad folders when the task is narrow."},
        ],
    ),
}

_JSON_FIELDS = ("pitfalls", "refinements", "triggers", "guardrails")


def _json_payload(value: Any) -> str:
    return json.dumps(value or [])


def _jsonish(value: Any) -> Any:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return value


def _row_matches_builtin(row: Mapping[str, Any], skill: Mapping[str, Any]) -> bool:
    for field_name in ("description", "procedure", "model_tier", "thinking_tier", "maturity"):
        if row.get(field_name) != skill[field_name]:
            return False
    for field_name in _JSON_FIELDS:
        if _jsonish(row.get(field_name)) != (skill.get(field_name) or []):
            return False
    return (
        (row.get("source_kind") or "legacy_db") == skill.get("source_kind", "legacy_db")
        and (row.get("trust_level") or "private_local") == skill.get("trust_level", "private_local")
    )


def _sql_params(skill: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": skill["name"],
        "desc": skill["description"],
        "proc": skill["procedure"],
        "model_tier": skill["model_tier"],
        "thinking_tier": skill["thinking_tier"],
        "maturity": skill["maturity"],
        "pitfalls": _json_payload(skill.get("pitfalls")),
        "refinements": _json_payload(skill.get("refinements")),
        "triggers": _json_payload(skill.get("triggers")),
        "guardrails": _json_payload(skill.get("guardrails")),
        "source_kind": skill.get("source_kind", "legacy_db"),
        "trust_level": skill.get("trust_level", "private_local"),
    }


async def ensure_builtin_skills() -> None:
    """Upsert code-owned product skills into DB.

    Only rows still marked ``builtin`` are overwritten. If a row has
    ``builtin=FALSE``, that database customization wins.
    """
    try:
        from sqlalchemy import text

        from brain.platform.db.repositories.unit_of_work import UnitOfWork

        for name, skill in BUILTIN_SKILLS.items():
            try:
                async with UnitOfWork() as uow:
                    result = await uow.session.execute(
                        text(
                            """
                            SELECT
                                id,
                                builtin,
                                description,
                                procedure,
                                model_tier,
                                thinking_tier,
                                maturity,
                                pitfalls,
                                refinements,
                                triggers,
                                guardrails,
                                source_kind,
                                trust_level
                            FROM skills
                            WHERE name = :name
                            """
                        ),
                        {"name": name},
                    )
                    row = result.mappings().first()

                    if row is None:
                        await uow.session.execute(
                            text(
                                """
                                INSERT INTO skills (
                                    name, description, procedure,
                                    model_tier, thinking_tier, maturity,
                                    pitfalls, refinements, triggers, guardrails,
                                    source_kind, trust_level,
                                    skill_type, builtin
                                )
                                VALUES (
                                    :name, :desc, :proc,
                                    :model_tier, :thinking_tier, :maturity,
                                    CAST(:pitfalls AS jsonb),
                                    CAST(:refinements AS jsonb),
                                    CAST(:triggers AS jsonb),
                                    CAST(:guardrails AS jsonb),
                                    :source_kind, :trust_level,
                                    'skill', TRUE
                                )
                                """
                            ),
                            _sql_params(skill),
                        )
                        logger.info("Created built-in skill: %s", name)
                        continue

                    if not row.get("builtin", True):
                        logger.debug("Skipping user-customized skill: %s", name)
                        continue
                    if _row_matches_builtin(row, skill):
                        logger.debug("Built-in skill already current: %s", name)
                        continue

                    await uow.session.execute(
                        text(
                            """
                            UPDATE skills
                            SET description = :desc,
                                procedure = :proc,
                                model_tier = :model_tier,
                                thinking_tier = :thinking_tier,
                                maturity = :maturity,
                                pitfalls = CAST(:pitfalls AS jsonb),
                                refinements = CAST(:refinements AS jsonb),
                                triggers = CAST(:triggers AS jsonb),
                                guardrails = CAST(:guardrails AS jsonb),
                                source_kind = :source_kind,
                                trust_level = :trust_level,
                                skill_type = 'skill',
                                builtin = TRUE,
                                archived = FALSE,
                                updated_at = NOW()
                            WHERE name = :name
                              AND (builtin IS TRUE OR builtin IS NULL)
                            """
                        ),
                        _sql_params(skill),
                    )
                    logger.debug("Updated built-in skill: %s", name)
            except Exception as exc:
                logger.warning(
                    "ensure_builtin_skill_failed skill=%s error=%s",
                    name,
                    exc,
                )
    except Exception as exc:
        logger.warning("ensure_builtin_skills failed (non-fatal): %s", exc)

    await _ensure_builtin_skill_bundles()


async def ensure_builtin_skills_cached(*, ttl_seconds: float = _BUILTIN_SKILLS_ENSURE_TTL_SECONDS) -> None:
    """Avoid re-upserting built-ins on every read-only catalog request."""
    global _BUILTIN_SKILLS_LAST_ENSURED_AT
    now = time.monotonic()
    if now - _BUILTIN_SKILLS_LAST_ENSURED_AT < ttl_seconds:
        return

    async with _builtin_skills_ensure_lock():
        now = time.monotonic()
        if now - _BUILTIN_SKILLS_LAST_ENSURED_AT < ttl_seconds:
            return
        await ensure_builtin_skills()
        _BUILTIN_SKILLS_LAST_ENSURED_AT = time.monotonic()


async def _ensure_builtin_skill_bundles() -> None:
    """Attach progressive filesystem bundles for code-owned built-ins."""
    if not BUILTIN_SKILL_BUNDLE_ROOT.exists():
        return

    try:
        from brain.platform.db.repositories.skill_bundles import SkillBundleRepository
        from brain.platform.db.repositories.skills import SkillRepository
        from brain.platform.db.repositories.unit_of_work import UnitOfWork
        from brain.platform.db.services.skill_bundle_io import SkillBundleIOService
    except Exception as exc:
        logger.warning("ensure_builtin_skill_bundles unavailable: %s", exc)
        return

    for name in BUILTIN_SKILLS:
        bundle_dir = BUILTIN_SKILL_BUNDLE_ROOT / name
        if not bundle_dir.is_dir():
            logger.warning("Missing built-in skill bundle: %s", bundle_dir)
            continue

        try:
            async with UnitOfWork() as uow:
                def _import_bundle(sync_db) -> None:
                    skill_repo = SkillRepository(sync_db)
                    bundle_repo = SkillBundleRepository(sync_db)
                    service = SkillBundleIOService(skill_repo, bundle_repo)
                    service.import_bundle(
                        bundle_dir,
                        namespace="illo_core",
                        enabled_scope="system",
                        update_policy="pinned",
                        review_status="approved",
                        trust_level=ILLO_CORE_TRUST_LEVEL,
                        source_kind=ILLO_CORE_SOURCE_KIND,
                        auto_bump_conflicting_semver=True,
                    )

                await uow.session.run_sync(_import_bundle)
        except Exception as exc:
            logger.warning(
                "ensure_builtin_skill_bundle_failed skill=%s error=%s",
                name,
                exc,
            )


__all__ = [
    "BUILTIN_SKILLS",
    "BUILTIN_SKILL_BUNDLE_ROOT",
    "ensure_builtin_skills",
    "ensure_builtin_skills_cached",
]

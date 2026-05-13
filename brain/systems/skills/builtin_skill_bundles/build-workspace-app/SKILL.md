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
   - Archived apps are not candidates for new build/create requests. Do not
     inspect archived apps while building a new app unless the user explicitly
     asks about archived/restorable apps. Only restore an archived app when the
     user explicitly asks to restore or reopen that archived app; otherwise
     create a fresh app or update an active app.
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
   - For ordinary REST/JSON APIs described by user-provided docs, prefer the
     built-in `generic.http` executor over provider-specific code. Declare
     `executor: { "type": "registered", "key": "generic.http" }` plus a
     `connector_spec` with `request`, optional Vault/project-bound `auth`,
     optional `response.items_path`, and optional `sync` mapping into a Domain
     binding. Use `kind: "http_sync"` when the response should upsert Domain
     records, and `kind: "http_request"` for create/update/delete calls that
     only need to return the external response. Pair GET with `external.read`,
     non-GET methods with `external.write`, and add `domain.write` only when
     `sync` mutates the Domain. Sync mappings may use plain string paths
     (`"title"`), `{ "path": "nested.id" }`, `{ "const": "Todo" }`,
     `{ "template": "ISSUE-{number}" }`, or
     `{ "if": { "field": "completed", "equals": true }, "then": "Done",
     "else": "Todo" }`. Use `deferred` only when the API cannot fit the
     generic spec yet.
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
- Domain records have a virtual top-level `title` separate from object data
  fields. You may use `title` in generated UI columns, board cards, and binding
  `fields` even when the Domain object's field list has no `title` data field.
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
      "executor": {"type": "registered", "key": "generic.http"},
      "connector_spec": {
        "kind": "http_sync",
        "request": {
          "method": "GET",
          "url": "https://api.example.com/repos/{owner}/{repo}/issues"
        },
        "auth": {
          "type": "bearer",
          "source": "project_env",
          "env": "GITHUB_TOKEN",
          "project_slug": "{owner}/{repo}"
        },
        "response": {"items_path": "$"},
        "sync": {
          "binding": "tickets",
          "remote_id": "id",
          "remote_id_field": "external_id",
          "title": "title",
          "fields": {
            "external_id": "id",
            "number": "number",
            "identifier": {"template": "ISSUE-{number}"},
            "url": "html_url",
            "status": {
              "if": {"field": "state", "equals": "closed"},
              "then": "Done",
              "else": "Todo"
            }
          }
        }
      }
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

# Host Bridge

Generated apps are workspace-native app surfaces. The bridge exposes app-local
state for UI preferences and manifest-bound Domain APIs for durable workspace
records. Do not treat sandboxed HTML as a template escape hatch; use it when
the app needs full-code layout or interaction while still using Illospace App
Kit classes and design tokens.

## App State

- `await window.illo.getState()` to read state.
- `await window.illo.setState(nextState)` to replace state.
- `await window.illo.updateState(patch)` for shallow patches.
- `window.addEventListener('illo:state', handler)` for host-pushed state.

Use app state for filters, drafts, collapsed sections, selected tabs, and other
ephemeral UI preferences. Store durable shared rows in Domains.

## Domain Records

Bind Domains in `manifest.data_plan.bindings`, then use the friendly SDK:

```js
const tickets = window.illo.domain("tickets");

await tickets.schema();
await tickets.list({ limit: 100 });
await tickets.query({ search: "blocked" });
await tickets.get(recordId);
await tickets.create({ title: "Fix upload retry", status: "todo" }, { title: "Fix upload retry" });
await tickets.update(recordId, { status: "in_progress" }, { expectedVersion });
await tickets.bulkUpdate([{ recordId, dataPatch: { status: "done" } }]);
await tickets.archive(recordId);
```

Generated app code should use the canonical camelCase request shape shown
above. The bridge may accept snake_case aliases for compatibility, but new app
source should prefer `recordId`, `recordIds`, `dataPatch`, and
`expectedVersion`.

For simple charts and summaries, use the generic aggregation helper:

```js
await tickets.aggregate({
  groupBy: "status",
  metrics: [{ type: "count", as: "tickets" }]
});
```

For linked records, use relation helpers:

```js
await tickets.relations.list({ relationKey: "ticket_blocks_ticket", sourceRecordId: recordId });
await tickets.relations.link("ticket_blocks_ticket", blockerId, blockedId, { reason: "API dependency" });
await tickets.relations.archive(relationId);
```

Do not call `relations.list({ recordId })`. Relation lists need a
`relationKey` and either `sourceRecordId` for outgoing links or
`targetRecordId` for incoming links.

For audit trails and refresh:

```js
await tickets.history(recordId, { limit: 20 });
const unsubscribe = tickets.subscribe((records) => render(records), { intervalMs: 5000 });
```

`subscribe` is polling-backed today; prefer it over ad hoc intervals so the host
can replace it with push subscriptions later without changing app code.

Compatibility methods remain available as `window.illo.domains.query(...)`,
`create(...)`, `update(...)`, `archive(...)`, and related operations. Prefer
`window.illo.domain(alias)` in new app code.

## Workspace Actions

`window.illo.actions.run(actionKey, payload)` is the reserved shape for
manifest-declared server-side actions such as ticket imports, sync, Slack,
email, or workflow triggers. Do not put external credentials or direct
secret-bearing calls inside generated app code. The app sends an intent to the
host; the server validates the manifest action and only dispatches to approved
registered executors.

Every generated app manifest must also keep the strict App Kit design contract:

```json
{
  "design_contract": {
    "kit": "constellation-app-kit",
    "theme_modes": ["dark", "light"]
  }
}
```

Do not use alternate keys such as `system`, `design_system`,
`uses_app_kit_classes`, or `supports_color_scheme` inside
`manifest.design_contract`.

Use Domains as the workspace truth bridge:

```text
External system -> server connector action -> Domain records -> generated app
generated app -> Domain records -> server connector action -> External system
```

Prefer workflow-level action names over provider-specific names unless the UI is
explicitly provider-specific:

```js
await window.illo.actions.run("tickets.importExternal", { source: "github" });
await window.illo.actions.run("tickets.syncExternal", { ticketId: record.recordId });
```

Declare the provider and auth boundary in the manifest:

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

Use `executor.type = "deferred"` when the app contract is ready but product work
still needs to register a connector executor. Use `executor.type = "registered"`
with an executor key only for approved server-owned code. If no executor exists,
the host returns a clear error instead of silently running arbitrary code.

Never persist user app source as repo files. Use `manage_workspace_app`.

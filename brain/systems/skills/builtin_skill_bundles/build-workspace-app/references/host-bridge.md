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
manifest-declared server-side actions such as GitHub, Slack, email, or workflow
triggers. Do not put external credentials or direct secret-bearing calls inside
generated app code. If no server executor exists for an action, the host returns
a clear error instead of silently running arbitrary code.

Never persist user app source as repo files. Use `manage_workspace_app`.

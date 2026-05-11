# Host Bridge

Generated workspace apps are real app surfaces. They may use the host bridge for
UI state and for authenticated workspace Domain access. Do not infer that a
workflow needs a special template or built-in view type when generic HTML/JS plus
Domain bindings can express it.

## App-local UI State

Use app-local state only for UI preferences, filters, drafts, collapsed panels,
and other ephemeral interface state:

- `await window.illo.getState()` to read state.
- `await window.illo.setState(nextState)` to replace state.
- `await window.illo.updateState(patch)` for shallow patches.
- `window.addEventListener('illo:state', handler)` for host-pushed state.

Do not store durable shared records in app-local state.

## Domain Records

Use Domains for durable shared data such as tickets, tasks, leads, bugs, notes,
decisions, customers, logs, and other records the workspace should be able to
query or reuse outside one app.

For Domain-backed apps, bind a stable alias in `manifest.data_plan.bindings`,
then use the generated app SDK:

```js
const tickets = window.illo.domain("tickets");

await tickets.schema();
await tickets.list({ limit: 100 });
await tickets.query({ search: "blocked" });
await tickets.get(recordId);
await tickets.create({ title: "Fix upload flow", status: "todo" }, { title: "Fix upload flow" });
await tickets.update(recordId, { status: "in_progress" }, { expectedVersion });
await tickets.archive(recordId);
```

Compatibility calls are also available:

```js
await window.illo.domains.query({ alias: "tickets" });
await window.illo.domains.create({ alias: "tickets", data: { title: "New ticket" } });
await window.illo.domains.update({ alias: "tickets", recordId, dataPatch: { status: "done" } });
await window.illo.domains.archive({ alias: "tickets", recordId });
```

The host validates the app manifest binding, allowed operations, and declared
fields before persisted apps can use the Domain bridge.

Never persist user app source as repo files. Use `manage_workspace_app`.

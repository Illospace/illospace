# Host Bridge

App capsules are workspace-native app surfaces. The bridge exposes app-local
state for UI preferences, manifest-bound Domain APIs for durable workspace
records, and system bindings for scoped workspace reads.

## App State

- `await window.illo.state.get()` to read state.
- `await window.illo.state.set(nextState)` to replace state.
- `await window.illo.state.update(patch)` for shallow patches.
- `window.addEventListener('illo:state', handler)` for host-pushed state.

Use app state for filters, drafts, collapsed sections, selected tabs, and other
ephemeral UI preferences. Store durable shared rows in Domains.

## Collaborative Artifacts

For team-facing thread artifacts such as votes, brainstorm boards, critique
walls, async check-ins, retros, and approval flows, declare
`manifest.collaboration` and write structured events through `window.illo.collab`.
This is the durable shared interaction layer Illo can inspect later.

```json
{
  "collaboration": {
    "mode": "event_sourced",
    "actions": {
      "vote.cast": {
        "reducer": {
          "type": "choice_by_actor",
          "state_path": "votes",
          "value_field": "optionId"
        }
      },
      "note.add": {
        "reducer": {
          "type": "append",
          "state_path": "notes"
        }
      },
      "status.change": {}
    }
  }
}
```

```js
await window.illo.collab.event("vote.cast", { optionId: "b", confidence: 0.8 });
await window.illo.collab.event("note.add", { body: "B seems lower risk." });

const sharedState = await window.illo.collab.state();
const tail = await window.illo.collab.events({ afterEventId: 12, limit: 50 });
const unsubscribe = window.illo.collab.subscribe((snapshot) => render(snapshot), {
  intervalMs: 5000
});
```

Use `statePatch` only for simple host-materialized fields such as
`status`, `summary`, or `phase`. Prefer declared reducers for repeatable team
input like votes and notes.

## Data Bindings

Bind Domains or system sources in `manifest.data_plan.bindings`, then use the
friendly SDK:

```js
const tickets = window.illo.data("tickets");

await tickets.schema();
await tickets.list({ limit: 100 });
await tickets.query({ search: "blocked" });
await tickets.get(recordId);
await tickets.create({ title: "Fix upload retry", status: "todo" }, { title: "Fix upload retry" });
await tickets.update(recordId, { status: "in_progress" }, { expectedVersion });
await tickets.archive(recordId);
```

Generated app code should use the canonical camelCase request shape shown
above. The bridge may accept snake_case aliases for compatibility, but new app
source should prefer `recordId`, `dataPatch`, and `expectedVersion`.

For simple charts and summaries, use the generic aggregation helper:

```js
await tickets.aggregate({
  groupBy: "status",
  metrics: [{ type: "count", as: "tickets" }]
});
```

For refresh:

```js
const unsubscribe = tickets.subscribe((records) => render(records), { intervalMs: 5000 });
```

`subscribe` is polling-backed today; prefer it over ad hoc intervals so the host
can replace it with push subscriptions later without changing app code.

Legacy compatibility methods may exist in old renderers. Prefer
`window.illo.data(alias)` in new app-capsule code.

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

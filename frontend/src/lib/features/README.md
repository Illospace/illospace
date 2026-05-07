# Frontend Feature Architecture

This directory groups frontend code by product feature. The rule of thumb:

- `domain/` owns pure state shapes, reducers, and view-model mapping
- `controllers/` own effects, commands, persistence, and workflow orchestration
- `realtime/` owns websocket event adapters and idempotent event dispatch
- `api/` owns typed wrappers around backend calls
- `components/` owns Svelte presentation and route-level feature composition

New code should import from these feature paths or from shared design-system primitives directly.

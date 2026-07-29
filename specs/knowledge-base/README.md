# Illo Knowledge — live implementation handoff

Read [north-star.md](north-star.md) before changing the implementation. The
temporary build plan lives in [prd.md](prd.md).

## Status

- Slice 1 is shipped: the derived index, structural Domain Records and GitHub
  connectors, hybrid search, scheduler wiring, and MCP/in-run exposure exist.
- Slice 2 is implemented under issue #577 and awaiting merge/deploy.
- Slice 3 is tracked by issue #578 and begins after slice 2 merges.

## Dependency graph

```text
slice 1 (shipped)
  -> slice 2a Slack thread connector ---------+
  -> slice 2b headless LLM distillation -----+-> slice 2 integration (#577)
  -> slice 2c additive memory mirror --------+
                                                  -> slice 3 eval + R2 retarget (#578)
                                                  -> memory strangler only after measured win
```

## Slice 2 invariants

- Conversational sources remain lexically searchable as bounded raw text, but
  only distilled fields are embedded.
- Distillation is admitted through `admit_work`; it must survive scheduler
  restarts, remain idempotent per source version, and expose pending, failed,
  and completed accounting.
- A Slack reply re-pulls its parent and all siblings and produces one knowledge
  row per thread.
- Memory mirroring is additive. Mirror only workspace-visible consolidated
  memories; never expose private/user-scoped memory through the workspace-wide
  knowledge index.
- GitHub uses the same distillation path as Slack.
- Do not change existing memory recall in slice 2.

## Next Agent Prompt

Publish and merge issue #577 after its full verification completes. Then
implement #578's golden-question harness and routine retarget. Record baseline
and candidate scores against the north-star contract; only perform the memory
strangler swap if the measured evaluation demonstrates a win. Finally deploy
and verify Slack, GitHub, and memory connector accounting plus distillation
dispatch/harvest operationally.

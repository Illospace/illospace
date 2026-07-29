# Illo Knowledge — live implementation handoff

Read [north-star.md](north-star.md) before changing the implementation. The
temporary build plan lives in [prd.md](prd.md).

## Status

- Slice 1 is shipped: the derived index, structural Domain Records and GitHub
  connectors, hybrid search, scheduler wiring, and MCP/in-run exposure exist.
- Slice 2 is in progress under issue #577.
- Slice 3 is blocked on slice 2 and tracked by issue #578.

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

Complete issue #577 by integrating the Slack connector, restart-safe headless
distillation, GitHub distillation opt-in, and the additive privacy-preserving
memory mirror. Use TDD, run focused and full verification, update this handoff,
then publish and merge the PR. After #577 is merged, implement #578's golden
question harness and routine retarget; only perform the memory strangler swap
if the measured evaluation demonstrates a win. Finally deploy and verify the
knowledge sync operationally.

# Sol 6.1 routing rollout — 2026-10-03

The workspace moves from GPT-6 Astra/high to GPT-6.1 Sol/medium for routine
coordination and tool use. Sol/high covers harder diagnosis and implementation.
Astra/high remains available for a narrow unresolved review or failed acceptance
check; xhigh needs a specific reason. Workers choose their model and effort
explicitly. Bulk work stays on GPT-5.6 Luna, starting at low effort.

Availability fallback is Astra → Sol 6.1 → Sol 6 → Sol 5.6. Each fallback was
checked through the existing ChatGPT subscription route. GPT-5.5 remains an
explicit legacy selection but is excluded from automatic fallback ahead of its
[October 14 subscription retirement](https://learn.chatgpt.com/docs/models#gpt-55-retirement).
No API key is introduced. Authentication and tool failures do not cause quality
escalation.

Sol 6/6.1 use the existing semantic compaction path at a soft 240,000-token
trigger, targeting 168,000 tokens. This is an operating policy, not a reduced
model context window. Explicit compaction configuration still takes precedence.
The purpose is to avoid repeatedly sending large histories in routine loops.

Cost reports are Standard API-equivalent estimates, not ChatGPT invoices. The
estimator uses the model's cache rate and the full-request long-context price
band before aggregation. Cache writes are a subset of OpenAI input usage and
are not counted twice. Per-call telemetry preserves auth mode, reported service
tier and reasoning tokens. Unknown provider fields and historical rows remain
null; reasoning tokens are already part of output tokens.

## Acceptance screen

Thirty synthetic cases were run on each route with the same prompts and checks:
6 issue extractions, 4 revised-memory lookups, 4 incomplete-release reviews,
4 code-tracing tasks, 4 dependency-order checks, 6 native read-tool round trips,
and 2 long-context lookups (539,087 and 899,087 input tokens). All used strict
JSON output. The fixtures contain no customer data and perform no external
writes. Deterministic grading ignores whitespace between comma-separated IDs.

| Route | Passed | Native tool tasks | Median task time | Output / reasoning tokens |
| --- | ---: | ---: | ---: | ---: |
| Astra/high | 30/30 | 6/6 | 3.48 s | 620 / 94 |
| Sol 6.1/high | 30/30 | 6/6 | 5.90 s | 915 / 361 |
| Sol 6.1/medium | 30/30 | 6/6 | 5.03 s | 627 / 101 |

Sol 6/medium and Sol 5.6/medium each passed one additional native tool round
trip. This is a compatibility and routine-task screen, not proof of general
quality parity. The tasks are simple, cache conditions differ, and timings are
not a controlled speed benchmark. Difficult reviews retain Astra. Actual
subscription usage must be measured after rollout; the audit's fixed-token
repricing is not a promise of realized savings.

The account's model-discovery list omitted Sol 6.1 even though requests worked.
The list is advisory. Its request requires a semantic client version; the shim
now supplies the tested version instead of the rejected `illo-brain` string.

## Activation and rollback

Code changes alone do not replace a stored workspace default. After deployment,
apply Sol 6.1/medium through runtime settings, set bulk effort to low, and update
cycle 2 to medium through the cycle command service. Cycle 9 stays high; cycle 8
stays explicitly on Luna/low. Refresh and inspect the stored orchestrate skill.
Preserve explicit model pins and disabled cycles. Save the prior configuration
and cycle settings before activation for a configuration rollback.

Verify the merged revision on all runtime images, database migration, deployment
health, live cycle context admission, and a real AgentRun with tool use. Check
actual model/effort/auth attribution independently of its completion message.

The sanitized audit, evaluation fixtures and raw results are retained in the
operator's durable report directory `illo-sol61-rollout-2026-10-03`.

## Primary references

- [Sol 6.1 model contract](https://developers.openai.com/api/docs/models/gpt-6.1-sol)
- [Model selection](https://developers.openai.com/api/docs/guides/model-selection)
- [Standard API prices](https://developers.openai.com/api/docs/pricing)
- [ChatGPT/Codex token credits](https://learn.chatgpt.com/docs/pricing#token-rates)
- [Deployment evaluation guidance](https://developers.openai.com/api/docs/guides/deployment-checklist)

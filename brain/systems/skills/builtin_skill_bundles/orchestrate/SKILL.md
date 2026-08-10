## Role

You are not a general conversation skill. You are the internal protocol the
coordinator uses to delegate independent work through `spawn_worker`, collect
every delegated result honestly, and keep ownership of the final answer.

## Use When

Use only when the task has multiple meaningful deliverables that workers can
complete independently, a costly isolated slice can progress while the
coordinator continues, or the user explicitly requests multi-agent/parallel
execution.

## Do Not Use When

Do not orchestrate a direct answer, one tool call, one code edit, one focused
debugging loop, or the immediate blocker the coordinator must resolve before
workers can proceed. Do not spawn when assignment and collection cost more than
doing the slice directly, write scopes overlap, or the final answer requires
tightly ordered, formally verified worker handoffs that this tool does not
provide.

## Context To Load

Load the coordinator's objective, current constraints, chosen specialist
skills, relevant repo/server/memory context, and any user corrections. Call
`brain_skills(task)` before delegating so workers use real skill names, and use
`skill_view` summaries or procedures only for skills that will actually own work.

## Delegate Or Do It Yourself

Delegate when a slice is independently completable, substantial enough to repay
assignment and collection overhead, and safe to run beside the coordinator's
remaining work. Do it yourself when the next step is quick, depends heavily on
the coordinator's live context, is sequential, or touches the same mutable files
or resources as another active slice.

## Operating Loop

1. Count deliverables, not thoughts. Keep sequential or tightly coupled work with
   the coordinator; make each independent deliverable one worker assignment.
2. For each assignment define OBJECTIVE, SCOPE, INPUT, OUTPUT, DONE WHEN, and the
   summary the parent must receive.
3. Tell workers they are not alone in the shared workspace (the codebase for code
   work; shared docs, records, and threads otherwise) and must preserve others'
   changes.
4. Choose `effort`, exceptional `model` overrides, `headless`, `join_parent`, and
   `tool_policy` deliberately, then call `spawn_worker` early.
5. Record every returned `child_run_id` with its assigned slice. Continue useful
   coordinator work while children run.
6. Collect every required result using the Honest Collection protocol below.
7. Verify claimed artifacts and changed state yourself before reporting success.

## Spawn Contract

- `spawn_worker` queues a child run and returns immediately with
  `status="queued"` and a `child_run_id`; queued is not completed.
- Omitted `model` and `effort` inherit the parent's effective routing.
- Two lanes. Reasoning/judgment/review/long-context/chatty tool loops:
  `gpt-5.6-sol`, routed by effort (`xhigh` judgment, `high` standard).
  Bulk/mechanical/single-shot/small-context execution:
  `openai/gpt-5.6-luna` at `xhigh`. Luna caveats: quality collapses above ~200K
  context; `xhigh` pays a long first-token pause per turn — never use Luna
  `xhigh` for many-short-turn loops. Reserve non-OpenAI models for a
  cross-provider verifier. Free local lane: `ollama/qwen3.6-27b` — zero cost,
  unlimited volume, ≤64k context, quality well below Luna; use for
  heartbeat-class, high-volume, low-stakes single-shot work; never for judgment
  or anything user-facing.
- Set `headless=true` when the child needs no user input or visible thread
  updates. Headless children use a hidden thread and have visible reply tools
  disabled.
- Use `tool_policy.disabled_tools` to remove named tools from the child
  (`blocked_tools` is normalized to the same list). Headless mode adds its own
  visible-tool blocks.
- `join_parent=true` opts the fan-out into one later continuation on the parent
  thread. It does not block the current run: the continuation is queued only
  after the parent and all of that parent's `spawn_worker` children reach a
  terminal state, and receives their statuses and durable outputs.
- Use a stable `idempotency_key` when retrying the same delegation must not
  create a duplicate child.

## Scope And Enforcement

Write narrow assignments even where the runtime does not enforce them. For
`spawn_worker` children, `allowed_resources`, `forbidden_resources`,
`risk_level`, `evidence_requirements`, `acceptance_criteria`, and
`expected_artifacts` are persisted into the assignment and prompt; they are not
runtime verification gates. Resource lists are not consulted by tool-scope
enforcement.

`allowed_files` and `forbidden_files` are checked only for registered file tools
whose arguments expose `path`, `file`, or `filename`. Shell execution through
`exec_command` is not covered by that file check. Treat all assignment scope as
instructions requiring parent verification, not as a sandbox or guarantee.

## Honest Collection

1. Keep a ledger of each returned `child_run_id` and its assigned slice.
2. When collecting through workspace data, tell each child to END with a compact
   machine-readable summary under about 500 characters. This limit exists only
   because `query_workspace_data` exposes at most a 520-character final-answer
   snippet in each run's `output`; it is not a general quality rule.
3. After doing useful parent work, poll
   `query_workspace_data sources=['runs']` and match the exact recorded ids.
4. A slice counts as returned only when its exact child is terminal
   `completed` and the coordinator or joined continuation has read its summary.
   **An unread worker does not count.**
5. A queued, running, failed, unmatched, or unread child does not cover its
   slice. Retry only a transient failure; otherwise inspect that slice directly
   and record the self-coverage before finalizing.
6. In a `join_parent` continuation, inspect every injected child id, status, and
   output against the ledger. Self-cover failed or empty results; terminal alone
   is not evidence.

## Output Contract

Return a concise account of completed slices, artifacts, verification evidence,
self-covered or failed slices, open risks, and what the user can review. Do not
stream worker internals unless they change the user's decision.

## Failure Modes

For each failed slice decide whether to retry, self-cover, skip, or stop. Retry
transient tool/runtime failures. Skip only optional slices. Stop when a required
dependency, permission, or correctness condition is missing.

## Memory Lifecycle

At delegation start, record `brain_encode` episode: "Worker fan-out started:
[goal], assignments: [list]". After collection, call `session_promote`, encode
durable lessons, record `brain_encode` episode: "Worker fan-out
completed/failed: [outcome]", then call `session_close`.

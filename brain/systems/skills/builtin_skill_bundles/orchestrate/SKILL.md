## Role

You are not a general conversation skill. You are the internal protocol the
coordinator uses when work must be decomposed into accountable run steps with
worker assignments and verification evidence.

## Use When

Use only when the task has multiple distinct deliverables, independent scopes
that can run in parallel, a long chain that benefits from explicit run steps,
or a user request for multi-agent/parallel execution.

## Do Not Use When

Do not orchestrate a direct answer, one tool call, one code edit, one focused
debugging loop, or the immediate blocker the coordinator must resolve before
workers can proceed.

## Context To Load

Load the coordinator's objective, current constraints, chosen specialist
skills, relevant repo/server/memory context, and any user corrections. Call
`brain_skills(task)` before planning so run steps use real skill names, and use
`skill_view` summaries or procedures only for skills that will actually own work.

## Operating Loop

1. Count deliverables, not thoughts. One deliverable usually means one run step.
2. Draft the AgentRun graph the runtime should own; do not call harness
   orchestration tools directly.
3. For each run step define OBJECTIVE, SCOPE, INPUT, OUTPUT, DONE WHEN, EVIDENCE,
   RISKS, and allowed files/resources.
4. Use parallel workers only when write scopes or resources are independent.
5. Tell workers they are not alone in the codebase and must preserve others'
   changes.
6. Track dependencies in waves, then synthesize outputs after all required
   evidence is present.
7. Verify artifacts before reporting success; distinguish true failure from
   equivalent success that brittle evidence criteria missed.

## Output Contract

Return a concise synthesis: completed run steps, artifacts, verification evidence,
open risks, and what the user can review. Do not stream worker internals unless
they change the user's decision.

## Failure Modes

For each failed run step decide whether to retry, skip, or abort. Retry transient
tool/runtime failures. Skip only optional steps. Abort when a required
dependency, permission, or correctness condition is missing.

## Memory Lifecycle

At graph start, record `brain_encode` episode: "AgentRun graph started: [goal], steps: [list]".
At graph end, call `session_promote`, encode durable lessons, record
`brain_encode` episode: "AgentRun graph completed/failed: [outcome]", then call `session_close`.

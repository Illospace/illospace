# Orchestration Failure Handling

Use four outcomes for failed worker slices:

- retry_slice: transient provider/tool/runtime failure or missing temporary dependency.
- self_cover_slice: the coordinator can inspect the assigned scope directly.
- skip_slice: optional work only; user value remains intact without it.
- stop: a required dependency, permission, correctness condition, or artifact is missing.

Queued, running, failed, unmatched, empty, or unread children have not covered
their slices. Never infer completion from a terminal status without reading the
result and verifying load-bearing claims.

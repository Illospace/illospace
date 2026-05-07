# Orchestration Failure Handling

Use three outcomes for failed run steps:

- retry_step: transient provider/tool/runtime failure, missing temporary dependency, or recoverable verifier glitch.
- skip_step: optional step only; user value remains intact without it.
- abort_graph: required dependency, permission, correctness condition, or artifact is missing.

Brittle evidence criteria are not automatically task failure. Check whether equivalent evidence proves the user outcome before escalating.

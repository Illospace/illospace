# Product Survivability Testing

The testing suite exists to answer one question: can the product survive
realistic change without breaking the behaviours users rely on?

Line coverage does not answer that. A large number of isolated unit tests does
not answer that. The suite should encode core product promises as executable
journeys, keep contracts honest between layers, and make every escaped bug
become a regression test at the behavioural level that would have caught it.

The Capability Survivability Index is the measurement layer for that vision. It
is not a replacement for passing tests. It is a risk-weighted map of whether
each product capability has the evidence types needed before a PR can claim
confidence.

Run the repo baseline:

```bash
make survivability
```

Run the core product journeys:

```bash
make test-product
```

Run the frontend product journeys:

```bash
(cd frontend && npx playwright install chromium)
make test-frontend-e2e
```

Run it for a PR diff:

```bash
BASE_REF=origin/main make survivability-pr
```

The capability map lives in
[`docs/survivability-capabilities.json`](survivability-capabilities.json).

## Scoring Model

Each capability is scored from 0 to 100 percent using these categories:

- `critical_invariants` - tests that encode the rules that must never break.
- `contracts` - API, DB, event, schema, generated-client, and UI contracts.
- `real_integration` - tests with real dependencies or realistic cross-module flow.
- `user_journeys` - end-to-end or route-level product journeys.
- `adversarial` - property, mutation, hardening, malformed-input, or abuse tests.
- `static_safety` - architecture, security, type, lint, or repository guardrails.

Capability scores are weighted by criticality. Impacted score is computed from
capabilities whose configured product paths or evidence files match changed
files. Editing or deleting `tests/test_api_auth.py` should therefore impact
`auth_rbac`, not just generic test-suite operability.

Changed files that do not map to any capability are reported as unmapped. In PR
mode, unmapped files should fail the gate: an unknown product surface is not
evidence of safety.

## Behavioural Core

The first-class behavioural suite lives in
[`tests/test_core_product_journeys.py`](../tests/test_core_product_journeys.py).
Those tests run against the real FastAPI app and Docker PostgreSQL schema. They
are intentionally named as user outcomes, not implementation details.

Current core journeys:

- First-run onboarding: setup check, register the first workspace owner, then read `/api/me` from the same session.
- Workspace thread lifecycle: create an idea, add a message, read history, mark done.
- Chat DM lifecycle: bootstrap chat, create a DM, send a message, observe unread state, read it.
- Multi-org isolation: outside users cannot read another org's Cortex or chat data.
- Workspace app lifecycle: create generated app, persist state, update/list/archive/restore state, block cross-org access, and preserve nested UI preferences when patching state.

When someone finds a product break during normal use, the fix should usually add
or extend one of these journeys first. Unit tests can still cover the narrow
cause, but the product journey is the guardrail that protects the user promise.

Do not keep manual provider diagnostics, one-off hypotheses, browser-harness
smokes, or route-smoke buckets that assert only mocked `200` responses in pytest
collection. Keep those as manual scripts or replace them with deterministic
product journeys/contracts.
Likewise, avoid source-string UI surveillance when a rendered component or
browser journey is the real product promise.

## Reading The Result

The output intentionally lists missing evidence. A low score does not always
mean the code is broken; it means the suite is under-instrumented for that
capability. Adding one more duplicate unit test should usually not move the
score. Adding the missing journey, contract, property, or integration evidence
should.

Suggested PR thresholds:

- Normal product changes: impacted score at least 85%.
- Core runtime, memory, or workspace changes: impacted score at least 90%.
- Auth, RBAC, vault, secrets, or DB schema changes: impacted score at least 95%.

The PR gate should also fail when an impacted capability is below its configured
threshold. Lowering the global minimum should be an explicit exception, not the
default path.

When a production bug escapes, classify which capability and evidence category
failed to catch it, add the missing product/regression test, then update the
capability map. That keeps the metric tied to reality instead of vanity
coverage.

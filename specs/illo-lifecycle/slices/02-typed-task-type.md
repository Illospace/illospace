# Slice 2 — Typed task domain

**No infra dependency. Parallel to Slice 1.** The domain axis is the spine for
scope (right handling for non-eng work) and for Slice 3 (deterministic
assignment).

## Naming note
Spec "task_type (domain)" == code **`task_domain`**. The codebase already uses
`task_type` for a *work-mode* axis (`code`/`investigation`/`delegation`;
`implement`/`edit_file`/`review`) describing HOW work is done. The new axis
describes WHAT KIND of work it is, so it lands as `task_domain` to avoid
overloading the existing key.

## The two-axis reality (corrected after reading the code)
The two existing classifiers are NOT the domain axis — they're the
work-mode/execution axis. So the fix is not "merge them into one"; it's **add the
domain axis and make the existing quality bars domain-aware**, overriding the
engineering bar only on positive non-engineering evidence.

## Slice 2a — shipped ✅ (domain classifier + domain-aware self-assess)
- **[brain/systems/task_domain.py](../../../brain/systems/task_domain.py)** —
  `TaskDomain` enum (`engineering|product|business|ops|other`) +
  `classify_task_domain(text, *, repo=None, policy=None)`. One owner of the
  domain axis. Precedence: policy/repo prior > keyword heuristic; ambiguous →
  `OTHER` (never silently engineering). Signals are domain nouns, never neutral
  verbs — kills the greedy-verb trap.
- **[brain/app/hooks/self_assess.py](../../../brain/app/hooks/self_assess.py)** —
  computes domain alongside work-mode and selects via
  `select_checklist(domain, work_mode)`: business/product/ops get their own
  checklist; engineering + ambiguous keep the existing work-mode checklist.
  Output adds `task_domain` + `checklist_label`; all prior keys preserved.
- **Tests** — [tests/test_task_domain.py](../../../tests/test_task_domain.py) (14)
  + existing [tests/test_self_assess.py](../../../tests/test_self_assess.py) (17)
  all green. Covers the greedy-verb traps ("create the Q3 launch plan" → business,
  not code) and the no-regression case ("investigate login failures" keeps the
  investigation checklist rather than falling to a bare bar).

## Slice 2b — remaining (thread domain through triage + prompt surfaces)
These need `task_domain` available at triage/orchestration time (the accurate
repo/policy prior), so they come after persistence:
1. **Persistence** — Alembic migration to store `task_domain` on the triaged Idea
   (and project onto the domain record). The triage agent sets it from the
   repo/policy prior, falling back to the heuristic.
2. **`run.py`** — make the `implement` template's TDD line conditional on
   `task_domain == engineering`; tag the payload with `task_domain`. Keep
   `classify_task` behavior (locked by `tests/test_runs.py`).
3. **Orchestrate worker template** — "you are not alone in the codebase" →
   "…in this workspace/project" when `task_domain != engineering`.
4. **report-workspace-blocker** — non-engineering blocker path (no
   stack-trace/`gh issue create` framing when `task_domain != engineering`).
5. **Routing evals** — add non-eng examples so routing generalizes.
6. **Surface on read** — `task_domain` visible in `illo_read`/`domain.inspect`.

## Verification
- 2a (done ✅): fixture table prompt→domain incl. greedy-verb traps; self_assess
  picks the right checklist per domain; existing suites green.
- 2b: a business task through real triage stores `task_domain=business` and gets
  the business bar; an eng task still `engineering`; grep proves one domain
  classifier owner (no second domain vocabulary).

## Must stay green
- `tests/test_self_assess.py` (done), `tests/test_runs.py` (2b must not change
  `classify_task` outcomes).

## Feedback that would change this slice
- The exact `TaskDomain` set (confirmed: engineering|product|business|ops|other).
- Whether `task_domain` is model-inferred, repo/policy-derived, or both
  (confirmed: both, policy/repo wins; heuristic is the text-only fallback).

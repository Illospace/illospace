# Slice 01 — Dossier core (pure assembly, budgets, truncation honesty)

## Contract unlocked
A typed, deterministic answer to "what does the assignee need to know?":
given already-fetched raw pieces, produce one `Dossier` — ordered, budgeted,
deduped, with explicit omission markers. Pure module, no I/O, no DB.

## API seam
New module `brain/systems/briefing/core.py` (package `briefing/` owns
gathering across the feature; keep `__init__.py` re-exports minimal).

```python
@dataclass(frozen=True)
class SourcePiece:          # one already-fetched fragment
    source: str             # "slack_thread" | "record" | "github_issue" | "github_pr" | "deploy_state" | "decision" | "evidence"
    ref: str                # stable pointer (permalink, record id, issue url)
    title: str
    body: str               # raw text, possibly long
    ts: datetime | None
    weight: int = 0         # caller-supplied salience hint

@dataclass(frozen=True)
class DossierSection:
    source: str
    items: tuple[DossierItem, ...]   # each item: ref, title, rendered_excerpt, omitted: bool
    omitted_count: int               # visible marker material

@dataclass(frozen=True)
class Dossier:
    job_ref: str                     # the owning record/idea key — job truth stays in the record
    headline: str
    sections: tuple[DossierSection, ...]
    total_chars: int
    budget: DossierBudget            # what was allowed
    omissions: tuple[str, ...]       # human-readable, e.g. "slack_thread: 14 older messages omitted"

def assemble_dossier(pieces: Sequence[SourcePiece], *, budget: DossierBudget) -> Dossier: ...
```

Policy inside `assemble_dossier` (all deterministic):
- per-source and total char budgets (`DossierBudget`, env-overridable by
  callers later — the core takes explicit values);
- ordering: recency-weighted within source, sources in fixed priority order
  (record → slack origin → github → deploy_state → decisions → evidence);
- dedupe by `ref`;
- excerpting never mid-word, always appending an explicit marker when cut;
- **every omission is counted and surfaced** — nothing silently dropped
  (README invariant; the run-1057 lesson).

## What the human can run/see
CLI probe: `python -m brain.systems.briefing --fixture tests/fixtures/briefing/uwear_bug.json`
prints the assembled dossier as readable text + a JSON dump. (Precedent:
`brain/systems/cortex/__main__.py`.)

## Verification
- Unit tests: budgets respected, ordering stable, dedupe, omission counts,
  zero-piece and oversized-piece edge cases, marker text present when cut.
- Golden fixture: `uwear_bug.json` → snapshot JSON of the dossier (assert
  exact), so composer work in slice 02 has a frozen input.

## Stays green
Full fast suite; no existing module imports change (additive package).

## Feedback that would change this slice
Reda prefers different section priorities, tighter default budgets, or wants
salience to be model-scored rather than deterministic (deliberately NOT in
v1 — deterministic first, judged by usage).

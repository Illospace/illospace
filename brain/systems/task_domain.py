"""Illo Brain — Task Domain classifier.

The *domain* axis of a task: is this engineering, product, business, ops, or
something else? This is deliberately distinct from the pre-existing *work-mode*
axis (``code``/``investigation``/``delegation`` in
``brain/app/hooks/self_assess.py``) and *execution-template* axis
(``implement``/``edit_file``/``review`` in ``brain/app/cli/run.py``), which
describe HOW a task is carried out. The domain describes WHAT KIND of work it is,
and it drives domain-appropriate quality bars, routing, and assignment.

This is the single owner of the domain axis — other subsystems classify through
``classify_task_domain`` rather than growing their own domain vocabulary.

Precedence (see ``specs/done/illo-lifecycle``): an explicit policy/repo prior wins;
the keyword heuristic only runs when no prior is given. The heuristic is a
best-effort fallback for text-only callers — the accurate signal is the
repo/policy prior available in the triage path. Ambiguous text resolves to
``OTHER`` (a real but minimal bar), never silently to ``ENGINEERING``: the whole
point is to stop applying engineering expectations to non-engineering work.

Signals are domain *nouns*, never neutral verbs. ``add``/``create``/``build``/
``update``/``write`` are excluded on purpose — they appear in every kind of work
("create the launch plan", "build the roadmap") and are exactly the greedy-verb
trap that mislabeled non-engineering tasks as code before.
"""

from __future__ import annotations

import re
from enum import Enum


class TaskDomain(str, Enum):
    ENGINEERING = "engineering"
    PRODUCT = "product"
    BUSINESS = "business"
    OPS = "ops"
    OTHER = "other"


# Ordered most-specific first; the first domain with a keyword hit wins. Keep
# these as domain nouns/phrases — no neutral action verbs (see module docstring).
_DOMAIN_SIGNALS: list[tuple[TaskDomain, re.Pattern[str]]] = [
    (
        TaskDomain.ENGINEERING,
        re.compile(
            r"(?i)\b("
            r"bug|hotfix|stack ?trace|exception|regression|"
            r"refactor\w*|endpoint|api|webhook|function|module|library|"
            r"migration|schema|query|database|\bdb\b|sql|cache|queue|"
            r"deploy\w*|rollback|latency|throughput|rate ?limit\w*|"
            r"authentication|\bauth\b|middleware|token|encryption|dependency|"
            r"server|backend|frontend|unit ?test|pytest|lint|compile|"
            r"merge conflict|pull request|\bPRs?\b|commit|codebase|repo"
            r")\b"
        ),
    ),
    (
        TaskDomain.OPS,
        re.compile(
            r"(?i)\b("
            r"incident|outage|on.?call|runbook|pager|sev ?[012]\b|prod\w* down|"
            r"provision\w*|infra\w*|terraform|kubernetes|\bk8s\b|scaling|capacity|"
            r"backup|restore|credential\w*|secret\w*|access request|permission grant"
            r")\b"
        ),
    ),
    (
        TaskDomain.PRODUCT,
        re.compile(
            r"(?i)\b("
            r"roadmap|\bprd\b|product spec|user stor\w+|feature request|backlog|"
            r"prioriti\w+|acceptance criteria|user research|persona|wireframe|"
            r"mockup|\bux\b|user experience|product decision"
            r")\b"
        ),
    ),
    (
        TaskDomain.BUSINESS,
        re.compile(
            r"(?i)\b("
            r"launch plan|go.?to.?market|\bgtm\b|marketing|campaign|pricing|"
            r"revenue|\bsales\b|\blead\b|leads|customer success|partnership|"
            r"contract|invoice|budget|forecast|hir(e|ing)|recruit\w*|onboarding|"
            r"legal|compliance|\bseo\b|content|blog post|newsletter|social media|"
            r"\bads?\b|ad campaign"
            r")\b"
        ),
    ),
]


def _coerce(value) -> "TaskDomain | None":
    """Accept a TaskDomain, its ``.value`` string (any case), or None."""
    if value is None:
        return None
    if isinstance(value, TaskDomain):
        return value
    try:
        return TaskDomain(str(value).strip().lower())
    except ValueError:
        return None


def classify_task_domain(text: str, *, repo=None, policy=None) -> TaskDomain:
    """Return the work domain for a task.

    Precedence: explicit ``policy`` prior > ``repo`` default > keyword heuristic.
    ``repo``/``policy`` may be a :class:`TaskDomain` or its string value; an
    unrecognized value is ignored (falls through to the heuristic). Ambiguous or
    empty text resolves to :attr:`TaskDomain.OTHER`, never to ``ENGINEERING``.
    """
    pinned = _coerce(policy) or _coerce(repo)
    if pinned is not None:
        return pinned
    if not text:
        return TaskDomain.OTHER
    for domain, pattern in _DOMAIN_SIGNALS:
        if pattern.search(text):
            return domain
    return TaskDomain.OTHER


def coerce_domain(value) -> "TaskDomain | None":
    """Public coercion: a :class:`TaskDomain`, its ``.value`` string (any case),
    or ``None`` -> ``TaskDomain`` or ``None``. Unknown strings return ``None``."""
    return _coerce(value)

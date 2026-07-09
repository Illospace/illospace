"""Deterministic owner resolution for triaged items.

Replaces interpreted prose ("route known repos to X, don't guess ownership") with
a typed, ordered decision, so the recurring wrong-owner fumble cannot happen: a
high-stakes route is a *rule*, not a model guess. Items that neither a rule nor a
connection owner resolves go to the unclaimed pool (owner ``None``) — parked and
visible for a teammate to pick up, never force-assigned and never silently
skipped.

Resolution order:
  1. **rule**        — ``task_domain`` -> owner, else repo -> owner
                       (e.g. business/product -> Reda).
  2. **connection**  — the human authority behind the inbound connection.
  3. **unassigned**  — no owner; parked in the unclaimed pool.

This module is pure (no DB, no I/O) so it is unit-testable and safe to import
anywhere. The rule table is *data* (``AssignmentRules``); ``default_rules()``
builds it from env. Callers pass the rules in.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum

from brain.systems.task_domain import TaskDomain, coerce_domain


class OwnerBasis(str, Enum):
    RULE = "rule"
    CONNECTION = "connection"
    UNASSIGNED = "unassigned"


@dataclass(frozen=True)
class OwnerDecision:
    user_id: str | None
    basis: OwnerBasis

    @property
    def is_assigned(self) -> bool:
        return self.user_id is not None


@dataclass(frozen=True)
class AssignmentRules:
    """Typed routing table. Domain rules take precedence over repo rules."""

    domain_owners: dict = field(default_factory=dict)
    repo_owners: dict = field(default_factory=dict)

    def owner_for(self, task_domain=None, repo=None) -> "str | None":
        dom = coerce_domain(task_domain)
        if dom is not None and dom in self.domain_owners:
            return self.domain_owners[dom]
        if repo:
            key = str(repo).strip().lower()
            for rk, owner in self.repo_owners.items():
                rk_norm = str(rk).strip().lower()
                # Match a full "owner/name" slug or a bare repo name.
                if key == rk_norm or key.endswith("/" + rk_norm):
                    return owner
        return None


def resolve_owner(
    *,
    task_domain=None,
    repo=None,
    connection_owner_id=None,
    rules: "AssignmentRules | None" = None,
) -> OwnerDecision:
    """Resolve the owner of a triaged item. Pure and deterministic.

    See module docstring for the resolution order. ``task_domain`` may be a
    :class:`TaskDomain` or its string value.
    """
    rules = rules or AssignmentRules()
    ruled = rules.owner_for(task_domain=task_domain, repo=repo)
    if ruled:
        return OwnerDecision(ruled, OwnerBasis.RULE)
    if connection_owner_id:
        return OwnerDecision(connection_owner_id, OwnerBasis.CONNECTION)
    return OwnerDecision(None, OwnerBasis.UNASSIGNED)


def _parse_repo_owners(raw: str) -> dict:
    """Parse ``ILLO_REPO_OWNERS`` of the form ``repo=uid,owner/repo=uid``."""
    out: dict = {}
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        repo, owner = chunk.split("=", 1)
        repo, owner = repo.strip(), owner.strip()
        if repo and owner:
            out[repo] = owner
    return out


def default_rules() -> AssignmentRules:
    """Build the rule table from env.

    ``ILLO_BUSINESS_OWNER_USER_ID`` routes business (and product, unless
    ``ILLO_PRODUCT_OWNER_USER_ID`` overrides) to that user. Unset owners simply
    produce no rule, so the item falls through to the connection authority or the
    unclaimed pool — never a guess.

    Single-tenant: these owner ids are global, not per-org — fine for the current
    one-team deployment; revisit if Illo ever routes across multiple orgs. A rule
    sets the triage *item* owner (who shepherds it), not a GitHub PR reviewer, so
    it does not conflict with the SOUL norm of nudging the author.
    """
    domain_owners: dict = {}
    biz = os.environ.get("ILLO_BUSINESS_OWNER_USER_ID", "").strip()
    prod = os.environ.get("ILLO_PRODUCT_OWNER_USER_ID", "").strip() or biz
    if biz:
        domain_owners[TaskDomain.BUSINESS] = biz
    if prod:
        domain_owners[TaskDomain.PRODUCT] = prod
    return AssignmentRules(
        domain_owners=domain_owners,
        repo_owners=_parse_repo_owners(os.environ.get("ILLO_REPO_OWNERS", "")),
    )

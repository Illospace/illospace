"""Deterministic cheap gate for the Uwear promotion-readiness Cycle."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import select

from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.platform.db.models.domain import Domain, DomainObjectType, DomainRecord
from brain.systems.cycles.common import SCHEDULED_DIGEST_RUN_KIND, cycle_run_launch_context
from brain.systems.cycles.contract_gate import persist_cycle_run_short_circuit_verdict
from brain.systems.cycles.contracts import (
    PROMOTION_READINESS_CYCLE_NAME,
    PROMOTION_READINESS_REPO,
    PROMOTION_READINESS_STATUS_DOMAIN_SLUG,
    PROMOTION_READINESS_STATUS_OBJECT_KEY,
)


PromotionReadinessOutcome = Literal[
    "unchanged",
    "idle",
    "evaluate",
    "unavailable",
]
BaselineReader = Callable[[Any, str], Awaitable[tuple[str | None, str | None]]]
TokenResolver = Callable[[Cycle], Awaitable[str]]
BranchHeadReader = Callable[..., Awaitable[str]]
BranchComparisonReader = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class PromotionReadinessGateDecision:
    """One cheap-gate result recorded before any agent sweep starts."""

    outcome: PromotionReadinessOutcome
    evaluated_at: datetime
    staging_sha: str | None = None
    main_sha: str | None = None
    previous_staging_sha: str | None = None
    previous_main_sha: str | None = None
    ahead_by: int | None = None
    error: str | None = None

    @property
    def short_circuit(self) -> bool:
        return self.outcome in {"unchanged", "idle"}

    @property
    def skip_agent(self) -> bool:
        return self.outcome == "idle"

    @property
    def requires_per_pr_review(self) -> bool:
        return self.outcome in {"evaluate", "unavailable"}

    @property
    def reaches_posting_path(self) -> bool:
        return self.outcome in {"unchanged", "evaluate"}

    @property
    def skip_reason(self) -> str | None:
        if not self.skip_agent:
            return None
        return f"promotion_readiness_{self.outcome}"

    def snapshot(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "evaluated_at": self.evaluated_at.isoformat(),
            "repo": PROMOTION_READINESS_REPO,
            "staging_sha": self.staging_sha,
            "main_sha": self.main_sha,
            "previous_staging_sha": self.previous_staging_sha,
            "previous_main_sha": self.previous_main_sha,
            "ahead_by": self.ahead_by,
            "short_circuit": self.short_circuit,
            "skip_agent": self.skip_agent,
            "requires_per_pr_review": self.requires_per_pr_review,
            "reaches_posting_path": self.reaches_posting_path,
            "error": self.error,
        }

    def closing_block_verdict(self) -> dict[str, str]:
        evaluated = (
            f"{self.evaluated_at.isoformat()}; "
            f"last_staging_sha={self.staging_sha}; last_main_sha={self.main_sha}"
        )
        if self.outcome == "idle":
            return {
                "risk": "IDLE",
                "evaluated": f"{evaluated}; staging ahead_by=0",
                "posted": "No — staging is not ahead; expensive per-PR sweep was skipped",
                "outcome": "skipped_idle",
            }
        raise ValueError("only idle decisions close before the agent posting path")


async def _async_last_evaluated_pair(
    session: Any,
    org_id: str,
) -> tuple[str | None, str | None]:
    record = (
        await session.scalars(
            select(DomainRecord)
            .join(Domain, Domain.id == DomainRecord.domain_id)
            .join(
                DomainObjectType,
                DomainObjectType.id == DomainRecord.object_type_id,
            )
            .where(
                Domain.slug == PROMOTION_READINESS_STATUS_DOMAIN_SLUG,
                Domain.org_id == org_id,
                Domain.archived_at.is_(None),
                DomainObjectType.key == PROMOTION_READINESS_STATUS_OBJECT_KEY,
                DomainObjectType.archived_at.is_(None),
                DomainRecord.org_id == org_id,
                DomainRecord.archived_at.is_(None),
            )
            .order_by(DomainRecord.updated_at.desc())
            .limit(1)
        )
    ).first()
    if record is None or not isinstance(record.data, dict):
        return None, None
    return (
        str(record.data.get("last_staging_sha") or "").strip() or None,
        str(record.data.get("last_main_sha") or "").strip() or None,
    )


async def _async_cycle_repo_token(cycle: Cycle) -> str:
    from brain.systems.vault import async_resolve_org_project_bound_env_tokens

    org_id = str(cycle.org_id or "").strip()
    if not org_id:
        raise RuntimeError("promotion-readiness Cycle has no workspace org")
    env = await async_resolve_org_project_bound_env_tokens(
        org_id=org_id,
        accessed_by="cycle_promotion_readiness_gate",
        project_slug=PROMOTION_READINESS_REPO,
        github_app_only=True,
        github_app_permissions={"contents": "read"},
    )
    token = str(env.get("GITHUB_TOKEN") or env.get("GH_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("no project-bound GitHub App token is available")
    return token


def _records_gate(cycle: Cycle, run: CycleRun) -> bool:
    launch_context = cycle_run_launch_context(run)
    return (
        str(cycle.name or "").strip() == PROMOTION_READINESS_CYCLE_NAME
        and launch_context.get("run_kind") == SCHEDULED_DIGEST_RUN_KIND
    )


async def async_apply_promotion_readiness_gate(
    session: Any,
    *,
    cycle: Cycle,
    run: CycleRun,
    now: datetime | None = None,
    baseline_reader: BaselineReader | None = None,
    token_resolver: TokenResolver | None = None,
    branch_head_reader: BranchHeadReader | None = None,
    branch_comparison_reader: BranchComparisonReader | None = None,
) -> PromotionReadinessGateDecision | None:
    """Short-circuit unchanged/idle scheduled runs before agent admission."""

    if not _records_gate(cycle, run):
        return None

    evaluated_at = now or datetime.now(timezone.utc)
    baseline_reader = baseline_reader or _async_last_evaluated_pair
    token_resolver = token_resolver or _async_cycle_repo_token
    if branch_head_reader is None or branch_comparison_reader is None:
        from brain.systems.cortex.project_context.github import (
            async_compare_repo_branches,
            async_get_repo_branch_head,
        )

        branch_head_reader = branch_head_reader or async_get_repo_branch_head
        branch_comparison_reader = (
            branch_comparison_reader or async_compare_repo_branches
        )

    previous_staging_sha: str | None = None
    previous_main_sha: str | None = None
    staging_sha: str | None = None
    main_sha: str | None = None
    try:
        org_id = str(cycle.org_id or "").strip()
        if not org_id:
            raise RuntimeError("promotion-readiness Cycle has no workspace org")
        previous_staging_sha, previous_main_sha = await baseline_reader(
            session,
            org_id,
        )
        token = await token_resolver(cycle)
        staging_sha, main_sha = await asyncio.gather(
            branch_head_reader(
                PROMOTION_READINESS_REPO,
                "staging",
                token=token,
            ),
            branch_head_reader(
                PROMOTION_READINESS_REPO,
                "main",
                token=token,
            ),
        )
        comparison = await branch_comparison_reader(
            PROMOTION_READINESS_REPO,
            "main",
            "staging",
            token=token,
        )
        ahead_by = comparison.get("ahead_by")
        if not isinstance(ahead_by, int) or isinstance(ahead_by, bool):
            raise RuntimeError("GitHub comparison omitted ahead_by")
        pair_unchanged = bool(
            staging_sha
            and main_sha
            and (staging_sha, main_sha)
            == (previous_staging_sha, previous_main_sha)
        )
        decision = PromotionReadinessGateDecision(
            outcome=(
                "idle"
                if ahead_by == 0
                else "unchanged"
                if pair_unchanged
                else "evaluate"
            ),
            evaluated_at=evaluated_at,
            staging_sha=staging_sha,
            main_sha=main_sha,
            previous_staging_sha=previous_staging_sha,
            previous_main_sha=previous_main_sha,
            ahead_by=ahead_by,
        )
    except Exception as exc:  # noqa: BLE001 - failed cheap reads must degrade open
        decision = PromotionReadinessGateDecision(
            outcome="unavailable",
            evaluated_at=evaluated_at,
            staging_sha=staging_sha,
            main_sha=main_sha,
            previous_staging_sha=previous_staging_sha,
            previous_main_sha=previous_main_sha,
            error=f"{type(exc).__name__}: {str(exc)[:240]}",
        )

    context_snapshot = dict(run.context_snapshot or {})
    context_snapshot["promotion_readiness_gate"] = decision.snapshot()
    run.context_snapshot = context_snapshot
    if decision.skip_agent:
        run.started_at = run.started_at or evaluated_at
        persist_cycle_run_short_circuit_verdict(
            run,
            decision.closing_block_verdict(),
        )
    return decision


__all__ = [
    "PromotionReadinessGateDecision",
    "async_apply_promotion_readiness_gate",
]

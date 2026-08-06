"""Deterministic cheap-gate policy for Uwear promotion readiness."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from sqlalchemy import select

from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.platform.db.models.domain import Domain, DomainObjectType, DomainRecord
from brain.systems.cycles.common import SCHEDULED_DIGEST_RUN_KIND, cycle_run_launch_context
from brain.systems.cycles.contracts import (
    CLOSING_BLOCK_VERDICT_REQUIRED_OUTPUT,
    CycleResultContractExtension,
    extend_cycle_result_contract,
)
from brain.systems.cycles.cycle_verdict_ledger import (
    ClosingBlockVerdict,
    persist_cycle_run_short_circuit_verdict,
)

BaselineReader = Callable[[Any, str], Awaitable[tuple[str | None, str | None]]]
TokenResolver = Callable[[Cycle], Awaitable[str]]
BranchHeadReader = Callable[..., Awaitable[str]]
BranchComparisonReader = Callable[..., Awaitable[dict[str, Any]]]


class PromotionReadinessOutcome(StrEnum):
    """The one persisted state for the promotion-readiness policy."""

    UNCHANGED = "unchanged"
    IDLE = "idle"
    EVALUATE = "evaluate"
    UNAVAILABLE = "unavailable"
    CONFIGURATION_ERROR = "configuration_error"


@dataclass(frozen=True)
class PromotionReadinessPolicy:
    """The single configuration point for the promotion-readiness policy."""

    execution_policy_key: str
    repository: str
    status_domain_slug: str
    status_object_key: str
    gate_name: str
    snapshot_key: str

    @property
    def contract_extension(self) -> CycleResultContractExtension:
        """Return this policy's typed result-contract contribution."""

        return CycleResultContractExtension(
            required_outputs=(CLOSING_BLOCK_VERDICT_REQUIRED_OUTPUT,),
            agent_instructions=(
                {
                    "name": self.gate_name,
                    "decision_source": (
                        f"cycle_memory.context.{self.snapshot_key}"
                    ),
                    "must_run_before": "per_pr_review",
                    "outcomes": {
                        "idle": (
                            "Stop without per-PR review or posting; emit the "
                            "required closing verdict."
                        ),
                        "unchanged": (
                            "Do not perform per-PR review. Continue directly to "
                            "the scheduled posting decision, then emit the "
                            "required closing verdict."
                        ),
                        "evaluate": (
                            "Run the bounded per-PR review, then continue to the "
                            "posting decision and required closing verdict."
                        ),
                        "unavailable": (
                            "The cheap gate could not decide. Run the mission and "
                            "report the evidence gap in the required closing "
                            "verdict."
                        ),
                    },
                },
            ),
        )


PROMOTION_READINESS_POLICY = PromotionReadinessPolicy(
    execution_policy_key="uwear_backend_promotion_readiness",
    repository="uwear-ai/uwear-backend",
    status_domain_slug="promotion-readiness",
    status_object_key="status",
    gate_name="promotion_readiness_sha_pair",
    snapshot_key="promotion_readiness_gate",
)


async def _async_last_evaluated_pair(
    session: Any,
    org_id: str,
) -> tuple[str | None, str | None]:
    policy = PROMOTION_READINESS_POLICY
    record = (
        await session.scalars(
            select(DomainRecord)
            .join(Domain, Domain.id == DomainRecord.domain_id)
            .join(
                DomainObjectType,
                DomainObjectType.id == DomainRecord.object_type_id,
            )
            .where(
                Domain.slug == policy.status_domain_slug,
                Domain.org_id == org_id,
                Domain.archived_at.is_(None),
                DomainObjectType.key == policy.status_object_key,
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
        project_slug=PROMOTION_READINESS_POLICY.repository,
        github_app_only=True,
        github_app_permissions={"contents": "read"},
    )
    token = str(env.get("GITHUB_TOKEN") or env.get("GH_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("no project-bound GitHub App token is available")
    return token


def _apply_contract_extension(run: CycleRun) -> None:
    context_snapshot = dict(run.context_snapshot or {})
    result_contract = context_snapshot.get("result_contract")
    if not isinstance(result_contract, dict):
        result_contract = {}
    context_snapshot["result_contract"] = extend_cycle_result_contract(
        result_contract,
        PROMOTION_READINESS_POLICY.contract_extension,
    )
    run.context_snapshot = context_snapshot


def _record_outcome(
    run: CycleRun,
    outcome: PromotionReadinessOutcome,
    evidence: dict[str, Any],
) -> None:
    context_snapshot = dict(run.context_snapshot or {})
    context_snapshot[PROMOTION_READINESS_POLICY.snapshot_key] = {
        "outcome": outcome.value,
        "evidence": evidence,
    }
    run.context_snapshot = context_snapshot


def _idle_closing_verdict(evidence: dict[str, Any]) -> ClosingBlockVerdict:
    evaluated_at = str(evidence.get("evaluated_at") or "unknown")
    staging_sha = str(evidence.get("staging_sha") or "None")
    main_sha = str(evidence.get("main_sha") or "None")
    return ClosingBlockVerdict(
        risk="IDLE",
        evaluated=(
            f"{evaluated_at}; last_staging_sha={staging_sha}; "
            f"last_main_sha={main_sha}; staging ahead_by=0"
        ),
        posted="No — staging is not ahead; expensive per-PR sweep was skipped",
        outcome="skipped_idle",
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
) -> PromotionReadinessOutcome | None:
    """Record and return the policy outcome for the configured scheduled Cycle."""

    launch_context = cycle_run_launch_context(run)
    if launch_context.get("run_kind") != SCHEDULED_DIGEST_RUN_KIND:
        return None

    _apply_contract_extension(run)
    evaluated_at = now or datetime.now(timezone.utc)
    evidence: dict[str, Any] = {
        "evaluated_at": evaluated_at.isoformat(),
        "repository": PROMOTION_READINESS_POLICY.repository,
    }
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
                PROMOTION_READINESS_POLICY.repository,
                "staging",
                token=token,
            ),
            branch_head_reader(
                PROMOTION_READINESS_POLICY.repository,
                "main",
                token=token,
            ),
        )
        comparison = await branch_comparison_reader(
            PROMOTION_READINESS_POLICY.repository,
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
        outcome = (
            PromotionReadinessOutcome.IDLE
            if ahead_by == 0
            else PromotionReadinessOutcome.UNCHANGED
            if pair_unchanged
            else PromotionReadinessOutcome.EVALUATE
        )
        evidence.update(
            {
                "staging_sha": staging_sha,
                "main_sha": main_sha,
                "previous_staging_sha": previous_staging_sha,
                "previous_main_sha": previous_main_sha,
                "ahead_by": ahead_by,
            }
        )
    except Exception as exc:  # noqa: BLE001 - failed cheap reads must degrade open
        outcome = PromotionReadinessOutcome.UNAVAILABLE
        evidence.update(
            {
                "staging_sha": staging_sha,
                "main_sha": main_sha,
                "previous_staging_sha": previous_staging_sha,
                "previous_main_sha": previous_main_sha,
                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
            }
        )

    _record_outcome(run, outcome, evidence)
    if outcome is PromotionReadinessOutcome.IDLE:
        run.started_at = run.started_at or evaluated_at
        persist_cycle_run_short_circuit_verdict(run, _idle_closing_verdict(evidence))
    return outcome


__all__ = [
    "PROMOTION_READINESS_POLICY",
    "PromotionReadinessOutcome",
    "async_apply_promotion_readiness_gate",
]

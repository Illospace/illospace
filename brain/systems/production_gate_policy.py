"""Pure policy for classifying closed issues against production evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import re

from brain.systems.deploy_state import (
    DeployState,
    DeployStateObservation,
)
from brain.systems.production_gate_github import FixingPullRequest, IssueClosure


_ISSUE_REF_RE = re.compile(
    r"\bgithub:(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+):"
    r"issue:(?P<number>[1-9][0-9]*)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ProductionEvidence:
    """One live-production signal that can raise a closure finding."""

    source: str
    reference: str
    signature: str
    occurred_at: datetime
    is_open: bool = False


@dataclass(frozen=True, slots=True)
class ProductionGateDecision:
    """The production workflow transition implied by one closed issue."""

    deployed_pr: FixingPullRequest | None
    nonproduction: tuple[
        tuple[FixingPullRequest, DeployStateObservation],
        ...,
    ]

    @property
    def primary_nonproduction(
        self,
    ) -> tuple[FixingPullRequest, DeployStateObservation] | None:
        return self.nonproduction[0] if self.nonproduction else None


@dataclass(frozen=True, slots=True)
class ProductionGateFinding:
    """One newly surfaced production-gate action."""

    record_id: int
    closure: IssueClosure
    pull_request: FixingPullRequest
    observation: DeployStateObservation
    production_evidence: ProductionEvidence | None

    def summary(self) -> dict[str, object]:
        evidence = self.production_evidence
        return {
            "record_id": self.record_id,
            "issue_number": self.closure.number,
            "pr_number": self.pull_request.number,
            "base_ref_name": self.pull_request.base_ref_name,
            "main_ancestry": self.observation.branch_ancestry_result("main"),
            "severity": "high" if evidence is not None else "normal",
            "production_evidence": evidence.reference if evidence is not None else None,
        }


def tracked_issue_identity(
    data: Mapping[str, object],
    *,
    title: str,
) -> tuple[str, int] | None:
    """Extract a GitHub issue identity from one tracker record."""

    candidates = (
        data.get("external_id"),
        data.get("ref"),
        data.get("url"),
        title,
    )
    for candidate in candidates:
        match = _ISSUE_REF_RE.search(_text(candidate))
        if match:
            return match.group("repo"), int(match.group("number"))

    repo = _text(data.get("repo"))
    raw_number = data.get("issue_number")
    if not repo or raw_number in (None, "") or isinstance(raw_number, bool):
        return None
    try:
        number = int(raw_number)
    except (TypeError, ValueError):
        return None
    return (repo, number) if number > 0 else None


def classify_closure(
    closure: IssueClosure,
    observations: Mapping[object, DeployStateObservation],
) -> ProductionGateDecision:
    """Choose the deployed or production-pending transition from ancestry."""

    nonproduction: list[
        tuple[FixingPullRequest, DeployStateObservation]
    ] = []
    deployed_pr: FixingPullRequest | None = None
    for pull_request in closure.fixing_pull_requests:
        key = (closure.repo, closure.number, pull_request.number)
        observation = observations.get(key)
        if observation is None:
            continue
        if observation.state is DeployState.DEPLOYED:
            deployed_pr = pull_request
        elif observation.state in {
            DeployState.STAGING,
            DeployState.UNMERGED,
        }:
            nonproduction.append((pull_request, observation))
    nonproduction.sort(
        key=lambda item: (
            _utc(item[0].merged_at)
            or datetime.min.replace(tzinfo=timezone.utc),
            item[0].number,
        ),
        reverse=True,
    )
    return ProductionGateDecision(
        deployed_pr=deployed_pr,
        nonproduction=tuple(nonproduction),
    )


def production_gate_progress_line(
    closure: IssueClosure,
    pull_request: FixingPullRequest,
    observation: DeployStateObservation,
) -> str:
    """Render the durable evidence note for one non-production fixing PR."""

    closed_at = _utc(closure.closed_at)
    timestamp = closed_at.isoformat() if closed_at else "unknown time"
    return (
        f"Staging-only closure detected at {timestamp}: PR #{pull_request.number} "
        f"({pull_request.canonical_ref}) merged to `{pull_request.base_ref_name}`; "
        f"main ancestry: {observation.branch_ancestry_result('main')}. "
        "Action: promote/verify."
    )


def matching_production_evidence(
    data: Mapping[str, object],
    closure: IssueClosure,
    evidence: Sequence[ProductionEvidence],
    *,
    since: datetime,
    until: datetime,
) -> ProductionEvidence | None:
    """Match recent live-production evidence to a tracker record or issue."""

    references = {
        _signature(data.get("rollbar_item")),
        _signature(data.get("alert_external_id")),
    }
    signatures = {
        _signature(data.get("error_signature")),
        _signature(data.get("signature")),
        _signature(closure.title),
    }
    references.discard("")
    signatures.discard("")
    for item in evidence:
        occurred_at = _utc(item.occurred_at)
        if occurred_at is None or not since <= occurred_at <= until:
            continue
        source = _text(item.source).casefold()
        if source == "rollbar":
            if not item.is_open:
                continue
        elif source not in {"#alerts", "alerts", "slack:#alerts"}:
            continue
        reference = _signature(item.reference)
        item_signature = _signature(item.signature)
        if reference and reference in references:
            return item
        if item_signature and any(
            item_signature == signature
            or item_signature in signature
            or signature in item_signature
            for signature in signatures
        ):
            return item
    return None


def _text(value: object) -> str:
    return str(value or "").strip()


def _signature(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", _text(value).casefold())


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

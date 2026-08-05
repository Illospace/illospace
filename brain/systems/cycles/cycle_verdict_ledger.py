"""Closing verdict parsing and the single durable Cycle verdict ledger sink."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Mapping, TypedDict

from brain.platform.db.models.cycle import CycleRun
from brain.systems.cycles.contracts import CLOSING_BLOCK_VERDICT_REQUIRED_OUTPUT

MISSION_RESULT_CONTRACT_VERDICT_KEY = "mission_result_contract_verdict"
SELF_REVIEW_SUMMARY_VERDICT_KEY = "self_review_summary"
CLOSING_BLOCK_VERDICT_KEY = "closing_block_verdict"

_CLOSING_BLOCK_FIELD_RES = {
    field.lower(): re.compile(
        rf"^[ \t]*(?:[-*][ \t]+)?(?:\*\*)?{field}(?:\*\*)?[ \t]*:[ \t]*(?:\*\*)?(?P<value>[^\r\n]+)",
        re.IGNORECASE | re.MULTILINE,
    )
    for field in ("Risk", "Evaluated", "Posted")
}


class ClosingBlockVerdict(TypedDict):
    """The readable three-line verdict plus its stable ledger outcome."""

    risk: str
    evaluated: str
    posted: str
    outcome: str


class CycleVerdictSettlement(StrEnum):
    """Supported settlements for one typed verdict envelope factory."""

    MISSION_SUCCESS = "mission_success"
    MISSION_SUCCESS_AFTER_REPAIR = "mission_success_after_repair"
    MISSION_CONTRACT_FAILED = "mission_contract_failed"
    MISSION_SHORT_CIRCUITED = "mission_short_circuited"
    GATE_NOT_REACHED = "gate_not_reached"


def _json_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _json_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def normalize_self_review_summary(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def extract_closing_block_verdict(
    candidate_answer: str | None,
) -> ClosingBlockVerdict | None:
    """Return the final complete Risk/Evaluated/Posted closing block."""

    answer = str(candidate_answer or "")
    values: dict[str, str] = {}
    positions: list[int] = []
    for field, pattern in _CLOSING_BLOCK_FIELD_RES.items():
        matches = list(pattern.finditer(answer))
        if not matches:
            return None
        match = matches[-1]
        value = str(match.group("value") or "").strip()
        if not value:
            return None
        values[field] = value
        positions.append(match.start())
    if positions != sorted(positions):
        return None
    return ClosingBlockVerdict(
        risk=values["risk"],
        evaluated=values["evaluated"],
        posted=values["posted"],
        outcome=_closing_block_outcome(values),
    )


def _closing_block_outcome(verdict: Mapping[str, str]) -> str:
    risk = _normalize_text(verdict.get("risk"))
    evaluated = _normalize_text(verdict.get("evaluated"))
    posted = _normalize_text(verdict.get("posted"))
    if "unchanged" in risk:
        return "skipped_unchanged"
    if "idle" in risk:
        return "skipped_idle"
    if risk in {"unknown", "not reached"} or evaluated.startswith(
        ("no ", "not reached", "unknown")
    ):
        return "gate_not_reached"
    if posted.startswith(("yes", "posted", "sent")):
        return "posted"
    if risk.startswith("low") and posted.startswith(
        ("no", "not posted", "withheld")
    ):
        return "evaluated_low_silent"
    return "evaluated_silent"


def format_closing_block_verdict(verdict: Mapping[str, Any]) -> str:
    """Render the stable three-line Cycle ledger verdict."""

    return "\n".join(
        f"{label}: {str(verdict.get(label.lower()) or '').strip()}"
        for label in ("Risk", "Evaluated", "Posted")
    )


def _ledger_self_review_summary(
    self_review_summary: str | None,
    closing_block_verdict: Mapping[str, Any] | None,
) -> str | None:
    summary = normalize_self_review_summary(self_review_summary)
    parts = [summary] if summary else []
    if closing_block_verdict:
        parts.append(format_closing_block_verdict(closing_block_verdict))
    return "\n".join(parts) or None


def cycle_contract_verdict(
    settlement: CycleVerdictSettlement,
    *,
    approved: bool,
    missing_outputs: list[Any] | None = None,
    final_missing_outputs: list[Any] | None = None,
    repair_attempted: bool = False,
    repair_succeeded: bool = False,
    visible_answer_source: str | None = None,
    self_review_summary: str | None = None,
    closing_block_verdict: Mapping[str, str] | None = None,
    base: Mapping[str, Any] | None = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build every normal, repaired, short-circuit, or fallback verdict."""

    verdict = dict(base or {})
    verdict.update(details or {})
    verdict.update(
        {
            "kind": "cycle_result_contract_verdict",
            "schema_version": 1,
            "approved": approved,
            "missing_outputs": list(missing_outputs or []),
            "final_missing_outputs": list(
                (missing_outputs or [])
                if final_missing_outputs is None
                else final_missing_outputs
            ),
            "repair_attempted": repair_attempted,
            "repair_succeeded": repair_succeeded,
            "settlement_status": settlement.value,
            "visible_answer_source": visible_answer_source,
            SELF_REVIEW_SUMMARY_VERDICT_KEY: normalize_self_review_summary(
                self_review_summary
            ),
            CLOSING_BLOCK_VERDICT_KEY: (
                dict(closing_block_verdict) if closing_block_verdict else None
            ),
        }
    )
    return verdict


def persisted_cycle_contract_verdict(
    cycle_run: CycleRun | None,
) -> dict[str, Any] | None:
    context_snapshot = _json_dict(getattr(cycle_run, "context_snapshot", None))
    verdict = context_snapshot.get(MISSION_RESULT_CONTRACT_VERDICT_KEY)
    return dict(verdict) if isinstance(verdict, dict) else None


def persist_cycle_contract_verdict(
    cycle_run: CycleRun,
    verdict: Mapping[str, Any],
    *,
    self_review_summary: str | None,
) -> None:
    """Write one verdict envelope and its readable #668 ledger projection."""

    normalized_summary = normalize_self_review_summary(self_review_summary)
    stored_verdict = dict(verdict)
    stored_verdict[SELF_REVIEW_SUMMARY_VERDICT_KEY] = normalized_summary
    closing_block = _json_dict(stored_verdict.get(CLOSING_BLOCK_VERDICT_KEY))
    context_snapshot = _json_dict(getattr(cycle_run, "context_snapshot", None))
    context_snapshot[MISSION_RESULT_CONTRACT_VERDICT_KEY] = stored_verdict
    cycle_run.context_snapshot = context_snapshot
    cycle_run.self_review_summary = _ledger_self_review_summary(
        normalized_summary,
        closing_block,
    )


def persist_cycle_run_short_circuit_verdict(
    cycle_run: CycleRun,
    closing_block_verdict: ClosingBlockVerdict,
) -> None:
    """Persist a deterministic pre-agent verdict on the #668 ledger surface."""

    verdict = cycle_contract_verdict(
        CycleVerdictSettlement.MISSION_SHORT_CIRCUITED,
        approved=True,
        closing_block_verdict=closing_block_verdict,
    )
    persist_cycle_contract_verdict(
        cycle_run,
        verdict,
        self_review_summary=None,
    )


def ensure_cycle_run_closing_verdict(
    cycle_run: CycleRun,
    *,
    status: str,
    error: str | None = None,
    skip_reason: str | None = None,
) -> ClosingBlockVerdict | None:
    """Persist gate-not-reached when the run's contract requires a closing block."""

    context_snapshot = _json_dict(getattr(cycle_run, "context_snapshot", None))
    result_contract = _json_dict(context_snapshot.get("result_contract"))
    required_outputs = _json_list(result_contract.get("required_outputs"))
    if CLOSING_BLOCK_VERDICT_REQUIRED_OUTPUT not in required_outputs:
        return None

    stored_verdict = _json_dict(
        context_snapshot.get(MISSION_RESULT_CONTRACT_VERDICT_KEY)
    )
    existing = _json_dict(stored_verdict.get(CLOSING_BLOCK_VERDICT_KEY))
    if all(
        str(existing.get(field) or "").strip()
        for field in ("risk", "evaluated", "posted")
    ):
        cycle_run.self_review_summary = _ledger_self_review_summary(
            stored_verdict.get(SELF_REVIEW_SUMMARY_VERDICT_KEY),
            existing,
        )
        return ClosingBlockVerdict(
            risk=str(existing["risk"]),
            evaluated=str(existing["evaluated"]),
            posted=str(existing["posted"]),
            outcome=str(existing.get("outcome") or _closing_block_outcome(existing)),
        )

    detail = str(error or skip_reason or status or "terminal state").strip()
    fallback = ClosingBlockVerdict(
        risk="UNKNOWN",
        evaluated=f"No — closing gate was not reached before {status}",
        posted=f"Unknown — no posting verdict was recorded ({detail})",
        outcome="gate_not_reached",
    )
    verdict = cycle_contract_verdict(
        CycleVerdictSettlement.GATE_NOT_REACHED,
        approved=False,
        missing_outputs=_json_list(stored_verdict.get("missing_outputs")),
        final_missing_outputs=_json_list(
            stored_verdict.get("final_missing_outputs")
        ),
        repair_attempted=bool(stored_verdict.get("repair_attempted")),
        repair_succeeded=bool(stored_verdict.get("repair_succeeded")),
        visible_answer_source=(
            str(stored_verdict.get("visible_answer_source") or "").strip() or None
        ),
        self_review_summary=normalize_self_review_summary(
            stored_verdict.get(SELF_REVIEW_SUMMARY_VERDICT_KEY)
        ),
        closing_block_verdict=fallback,
        base=stored_verdict,
    )
    persist_cycle_contract_verdict(
        cycle_run,
        verdict,
        self_review_summary=normalize_self_review_summary(
            verdict.get(SELF_REVIEW_SUMMARY_VERDICT_KEY)
        ),
    )
    return fallback


__all__ = [
    "CLOSING_BLOCK_VERDICT_KEY",
    "ClosingBlockVerdict",
    "CycleVerdictSettlement",
    "MISSION_RESULT_CONTRACT_VERDICT_KEY",
    "SELF_REVIEW_SUMMARY_VERDICT_KEY",
    "cycle_contract_verdict",
    "ensure_cycle_run_closing_verdict",
    "extract_closing_block_verdict",
    "format_closing_block_verdict",
    "normalize_self_review_summary",
    "persist_cycle_contract_verdict",
    "persist_cycle_run_short_circuit_verdict",
    "persisted_cycle_contract_verdict",
]

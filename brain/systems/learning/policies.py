"""Persist conservative policy promotion recommendations."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from statistics import mean

from sqlalchemy import select

from brain.platform.db.models.learning import LearningExample, PolicyPromotion
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from .genomes import persist_run_genome

logger = logging.getLogger(__name__)

POLICY_STATUS_RECOMMENDED = "recommended"
POLICY_STATUS_SHADOW = "shadow"
POLICY_STATUS_ACTIVE = "active"
POLICY_STATUS_ROLLED_BACK = "rolled_back"
POLICY_STATUS_DEMOTED = "demoted"

VALID_LEARNING_VISIBILITIES = {"private", "org", "global"}

LOW_RISK_PROMOTION_TYPES = {
    "scout_rule",
    "retrieval_boost",
    "route_override",
}

PROMOTION_ACTIVATION_THRESHOLDS = {
    "scout_rule": {
        "min_support_count": 2,
        "min_mean_readiness": 0.68,
        "max_mean_failure_rate": 0.35,
        "max_mean_rework_rate": 0.55,
    },
    "retrieval_boost": {
        "min_support_count": 3,
        "min_mean_readiness": 0.72,
        "max_mean_failure_rate": 0.30,
        "max_mean_rework_rate": 0.60,
    },
    "route_override": {
        "min_support_count": 3,
        "min_mean_readiness": 0.75,
        "max_mean_failure_rate": 0.28,
        "max_mean_rework_rate": 0.50,
    },
}

def _stable_json(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _normalize_json(value):
    if value is None or isinstance(value, dict):
        return value or {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    if isinstance(value, list):
        return value
    try:
        return dict(value)
    except Exception:
        return {}


def _clean_text(value) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _learning_scope(
    *,
    user_id: str | None,
    org_id: str | None,
    visibility: str | None = None,
    explicit_global_promotion: bool = False,
) -> dict:
    resolved_user_id = _clean_text(user_id)
    resolved_org_id = _clean_text(org_id)
    resolved_visibility = _clean_text(visibility) or ("org" if resolved_org_id else "private")
    if not resolved_user_id:
        raise ValueError("learning writes require user_id")
    if resolved_visibility not in VALID_LEARNING_VISIBILITIES:
        raise ValueError(f"invalid learning visibility: {resolved_visibility!r}")
    if resolved_visibility == "org" and not resolved_org_id:
        raise ValueError("org-scoped learning writes require org_id")
    if resolved_visibility == "global" and not explicit_global_promotion:
        raise ValueError("global learning promotion requires explicit_global_promotion=True")
    return {
        "user_id": resolved_user_id,
        "org_id": resolved_org_id,
        "visibility": resolved_visibility,
    }


def _same_scope(row, *, org_id: str | None, visibility: str) -> bool:
    row_visibility = getattr(row, "visibility", None) or "private"
    row_org_id = getattr(row, "org_id", None)
    if visibility == "global":
        return row_visibility == "global"
    if visibility == "org":
        return row_visibility == "org" and _same_identifier(row_org_id, org_id)
    return row_visibility == "private" and _same_identifier(row_org_id, org_id)


def _same_identifier(left, right) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return str(left).replace("-", "") == str(right).replace("-", "")


def _source_refs(run_id: int, genome: dict) -> dict:
    return {
        "run_id": run_id,
        "genome_id": genome.get("id"),
        "genome_hash": genome["genome_hash"],
    }


def _policy_version(uow, promotion_type: str, policy_key: str, *, org_id: str | None, visibility: str) -> int:
    rows = uow.session.scalars(
        select(PolicyPromotion).where(PolicyPromotion.promotion_type == promotion_type)
    ).all()
    versions = [
        int(row.version or 0)
        for row in rows
        if _policy_key_from_row(row) == policy_key
        and _same_scope(row, org_id=org_id, visibility=visibility)
    ]
    return (max(versions) if versions else 0) + 1


def _existing_policy(
    uow,
    promotion_type: str,
    source_kind: str,
    source_refs: dict,
    policy_key: str,
    *,
    org_id: str | None,
    visibility: str,
):
    stmt = select(PolicyPromotion).where(
        PolicyPromotion.promotion_type == promotion_type,
        PolicyPromotion.source_kind == source_kind,
    )
    rows = uow.session.scalars(stmt).all()
    for row in rows:
        row_source_refs = _normalize_json(row.source_refs)
        row_payload = _normalize_json(row.policy_payload)
        if (
            row_source_refs == source_refs
            and row_payload.get("policy_key") == policy_key
            and _same_scope(row, org_id=org_id, visibility=visibility)
        ):
            return row
    return None


def _policy_key_from_row(row: PolicyPromotion) -> str | None:
    payload = _normalize_json(row.policy_payload)
    return payload.get("policy_key")


def _promotion_rows(
    uow,
    promotion_type: str,
    policy_key: str,
    *,
    org_id: str | None,
    visibility: str,
) -> list[PolicyPromotion]:
    stmt = select(PolicyPromotion).where(
        PolicyPromotion.promotion_type == promotion_type,
    )
    rows = []
    for row in uow.session.scalars(stmt):
        if _policy_key_from_row(row) == policy_key and _same_scope(row, org_id=org_id, visibility=visibility):
            rows.append(row)
    return rows


def _promotion_metrics_from_row(row: PolicyPromotion) -> dict:
    evidence = _normalize_json(row.evidence)
    shadow_metrics = _normalize_json(row.shadow_metrics)
    signals = _normalize_json(evidence.get("signals"))

    readiness = shadow_metrics.get("readiness_score")
    if readiness is None:
        readiness = evidence.get("readiness_score")
    if readiness is None:
        readiness = evidence.get("promotion_readiness")
    if readiness is None:
        readiness = evidence.get("satisfaction_proxy")
    if readiness is None:
        readiness = shadow_metrics.get("satisfaction_proxy")
    readiness = float(readiness or 0.0)

    failure_rate = 1.0 if signals.get("success") is False or evidence.get("success") is False else 0.0
    rework_rate = 1.0 if signals.get("rework_required") or evidence.get("rework_required") else 0.0

    return {
        "readiness": max(0.0, min(1.0, round(readiness, 3))),
        "failure_rate": failure_rate,
        "rework_rate": rework_rate,
        "status": row.status,
        "created_at": row.created_at,
        "policy_key": _policy_key_from_row(row),
        "version": row.version,
    }
def record_policy_promotion(
    *,
    promotion_type: str,
    source_kind: str,
    source_refs: dict,
    policy_payload: dict,
    evidence: dict,
    user_id: str | None,
    org_id: str | None = None,
    visibility: str | None = None,
    shadow_metrics: dict | None = None,
    reviewer_id: str | None = None,
    shadow_enabled: bool = False,
    explicit_global_promotion: bool = False,
) -> dict | None:
    """Persist a versioned policy recommendation without activating it."""
    policy_key = policy_payload.get("policy_key")
    if not policy_key:
        raise ValueError("policy_payload.policy_key is required")
    scope = _learning_scope(
        user_id=user_id,
        org_id=org_id,
        visibility=visibility,
        explicit_global_promotion=explicit_global_promotion,
    )

    try:
        with UnitOfWork() as uow:
            existing = _existing_policy(
                uow,
                promotion_type,
                source_kind,
                source_refs,
                policy_key,
                org_id=scope["org_id"],
                visibility=scope["visibility"],
            )
            if existing:
                return {
                    "id": existing.id,
                    "version": existing.version,
                    "status": existing.status,
                    "policy_key": policy_key,
                    "visibility": existing.visibility,
                    "org_id": existing.org_id,
                    "duplicate": True,
                }

            version = _policy_version(
                uow,
                promotion_type,
                policy_key,
                org_id=scope["org_id"],
                visibility=scope["visibility"],
            )
            status = POLICY_STATUS_SHADOW if shadow_enabled else POLICY_STATUS_RECOMMENDED
            target = PolicyPromotion(
                promotion_type=promotion_type,
                source_kind=source_kind,
                user_id=scope["user_id"],
                org_id=scope["org_id"],
                visibility=scope["visibility"],
                source_refs=source_refs,
                policy_payload=policy_payload,
                status=status,
                evidence=evidence,
                version=version,
                shadow_metrics=shadow_metrics or {"shadow_enabled": shadow_enabled},
                reviewer_id=reviewer_id,
                explicit_global_promotion=explicit_global_promotion,
            )
            uow.session.add(target)
            uow.session.flush()
            return {
                "id": target.id,
                "version": version,
                "status": status,
                "policy_key": policy_key,
                "visibility": target.visibility,
                "org_id": target.org_id,
                "duplicate": False,
            }
    except Exception as exc:
        logger.debug("Policy promotion persistence failed: %s", exc)
        return None


def policy_activation_report(promotion_id: int) -> dict | None:
    """Summarize whether a policy promotion can move from shadow to active."""
    try:
        with UnitOfWork() as uow:
            promotion = uow.session.get(PolicyPromotion, promotion_id)
            if not promotion:
                return None

            policy_key = _policy_key_from_row(promotion)
            if not policy_key:
                return {
                    "promotion_id": promotion_id,
                    "eligible": False,
                    "reason": "policy promotion is missing a policy_key",
                }

            thresholds = PROMOTION_ACTIVATION_THRESHOLDS.get(promotion.promotion_type, {})
            if promotion.promotion_type not in LOW_RISK_PROMOTION_TYPES:
                return {
                    "promotion_id": promotion_id,
                    "promotion_type": promotion.promotion_type,
                    "policy_key": policy_key,
                    "eligible": False,
                    "reason": "only low-risk promotion types can be activated",
                    "thresholds": thresholds,
                }
            if promotion.visibility == "global" and not promotion.explicit_global_promotion:
                return {
                    "promotion_id": promotion_id,
                    "promotion_type": promotion.promotion_type,
                    "policy_key": policy_key,
                    "eligible": False,
                    "reason": "global promotion was not explicitly approved",
                    "thresholds": thresholds,
                }

            rows = _promotion_rows(
                uow,
                promotion.promotion_type,
                policy_key,
                org_id=promotion.org_id,
                visibility=promotion.visibility,
            )
            live_rows = [
                row for row in rows
                if row.status not in {POLICY_STATUS_ROLLED_BACK, POLICY_STATUS_DEMOTED}
            ]
            metrics = [_promotion_metrics_from_row(row) for row in live_rows]
            if not metrics:
                return {
                    "promotion_id": promotion_id,
                    "promotion_type": promotion.promotion_type,
                    "policy_key": policy_key,
                    "eligible": False,
                    "reason": "no evidence rows available for this policy_key",
                    "thresholds": thresholds,
                }

            active_rows = [row for row in live_rows if row.status == POLICY_STATUS_ACTIVE]
            if active_rows and promotion.status != POLICY_STATUS_ACTIVE:
                return {
                    "promotion_id": promotion_id,
                    "promotion_type": promotion.promotion_type,
                    "policy_key": policy_key,
                    "eligible": False,
                    "reason": "a different active version already exists for this policy_key",
                    "thresholds": thresholds,
                    "metrics": {
                        "support_count": len(metrics),
                        "mean_readiness": round(mean(m["readiness"] for m in metrics), 3),
                        "mean_failure_rate": round(mean(m["failure_rate"] for m in metrics), 3),
                        "mean_rework_rate": round(mean(m["rework_rate"] for m in metrics), 3),
                    },
                }

            support_count = len(metrics)
            mean_readiness = round(mean(m["readiness"] for m in metrics), 3)
            mean_failure_rate = round(mean(m["failure_rate"] for m in metrics), 3)
            mean_rework_rate = round(mean(m["rework_rate"] for m in metrics), 3)

            eligible = (
                support_count >= thresholds["min_support_count"]
                and mean_readiness >= thresholds["min_mean_readiness"]
                and mean_failure_rate <= thresholds["max_mean_failure_rate"]
                and mean_rework_rate <= thresholds["max_mean_rework_rate"]
            )

            reason = "evidence meets activation thresholds" if eligible else "evidence below activation thresholds"
            return {
                "promotion_id": promotion_id,
                "promotion_type": promotion.promotion_type,
                "policy_key": policy_key,
                "eligible": eligible,
                "reason": reason,
                "thresholds": thresholds,
                "metrics": {
                    "support_count": support_count,
                    "mean_readiness": mean_readiness,
                    "mean_failure_rate": mean_failure_rate,
                    "mean_rework_rate": mean_rework_rate,
                },
            }
    except Exception as exc:
        logger.debug("Policy activation report failed for promotion %s: %s", promotion_id, exc)
        return None


def activate_policy_promotion(promotion_id: int, *, reviewer_id: str | None = None) -> dict | None:
    """Activate a low-risk policy promotion when its evidence clears the gate."""
    report = policy_activation_report(promotion_id)
    if not report:
        return None
    if not report.get("eligible"):
        raise ValueError(report.get("reason") or "promotion is not eligible for activation")

    now = datetime.now(timezone.utc)
    try:
        with UnitOfWork() as uow:
            promotion = uow.session.get(PolicyPromotion, promotion_id)
            if not promotion:
                return None
            if promotion.status == POLICY_STATUS_ACTIVE:
                return {
                    "id": promotion.id,
                    "version": promotion.version,
                    "status": promotion.status,
                    "policy_key": _policy_key_from_row(promotion),
                    "visibility": promotion.visibility,
                    "org_id": promotion.org_id,
                    "already_active": True,
                }
            if promotion.status in {POLICY_STATUS_ROLLED_BACK, POLICY_STATUS_DEMOTED}:
                raise ValueError("rolled-back or demoted promotions cannot be activated again")

            previous_status = promotion.status
            promotion.status = POLICY_STATUS_ACTIVE
            promotion.activated_at = now
            promotion.rolled_back_at = None
            promotion.demoted_at = None
            promotion.demotion_reason = None
            promotion.shadow_metrics = {
                **_normalize_json(promotion.shadow_metrics),
                "activation_report": report,
                "activated_at": now.isoformat(),
                "activated_from": previous_status,
                "reviewer_id": reviewer_id,
            }
            promotion.reviewer_id = reviewer_id or promotion.reviewer_id
            uow.session.flush()
            return {
                "id": promotion.id,
                "version": promotion.version,
                "status": promotion.status,
                "policy_key": _policy_key_from_row(promotion),
                "visibility": promotion.visibility,
                "org_id": promotion.org_id,
                "activated_at": now.isoformat(),
                "activation_report": report,
            }
    except Exception as exc:
        logger.debug("Policy activation failed for promotion %s: %s", promotion_id, exc)
        raise


def rollback_policy_promotion(
    promotion_id: int,
    *,
    reason: str,
    reviewer_id: str | None = None,
) -> dict | None:
    """Roll back an active or shadow promotion while preserving its audit trail."""
    if not reason:
        raise ValueError("rollback reason is required")

    now = datetime.now(timezone.utc)
    try:
        with UnitOfWork() as uow:
            promotion = uow.session.get(PolicyPromotion, promotion_id)
            if not promotion:
                return None

            previous_status = promotion.status
            promotion.status = POLICY_STATUS_ROLLED_BACK
            promotion.rolled_back_at = now
            promotion.shadow_metrics = {
                **_normalize_json(promotion.shadow_metrics),
                "rollback_reason": reason,
                "rolled_back_at": now.isoformat(),
                "rolled_back_from": previous_status,
                "reviewer_id": reviewer_id,
            }
            promotion.reviewer_id = reviewer_id or promotion.reviewer_id
            uow.session.flush()
            return {
                "id": promotion.id,
                "version": promotion.version,
                "status": promotion.status,
                "policy_key": _policy_key_from_row(promotion),
                "visibility": promotion.visibility,
                "org_id": promotion.org_id,
                "rolled_back_at": now.isoformat(),
                "rollback_reason": reason,
            }
    except Exception as exc:
        logger.debug("Policy rollback failed for promotion %s: %s", promotion_id, exc)
        raise


def demote_policy_promotion(
    promotion_id: int,
    *,
    reason: str,
    reviewer_id: str | None = None,
) -> dict | None:
    """Demote an overfit promotion without erasing the evidence trail."""
    if not reason:
        raise ValueError("demotion reason is required")

    now = datetime.now(timezone.utc)
    try:
        with UnitOfWork() as uow:
            promotion = uow.session.get(PolicyPromotion, promotion_id)
            if not promotion:
                return None

            previous_status = promotion.status
            promotion.status = POLICY_STATUS_DEMOTED
            promotion.demoted_at = now
            promotion.demotion_reason = reason
            promotion.shadow_metrics = {
                **_normalize_json(promotion.shadow_metrics),
                "demotion_reason": reason,
                "demoted_at": now.isoformat(),
                "demoted_from": previous_status,
                "reviewer_id": reviewer_id,
            }
            promotion.reviewer_id = reviewer_id or promotion.reviewer_id
            uow.session.flush()
            return {
                "id": promotion.id,
                "version": promotion.version,
                "status": promotion.status,
                "policy_key": _policy_key_from_row(promotion),
                "visibility": promotion.visibility,
                "org_id": promotion.org_id,
                "demoted_at": now.isoformat(),
                "demotion_reason": reason,
            }
    except Exception as exc:
        logger.debug("Policy demotion failed for promotion %s: %s", promotion_id, exc)
        raise


def _learning_example_type(genome: dict) -> str:
    return "negative" if genome.get("learning_outcome") == "negative" else "unverified"


def _learning_example_lesson(genome: dict) -> str:
    evidence_status = genome.get("evidence_status") or "unverified"
    skill_name = genome.get("skill_name") or "general"
    task_family = genome.get("task_family") or "general"
    if genome.get("learning_outcome") == "negative":
        return (
            f"Do not promote the {skill_name} behavior for {task_family}: "
            f"the episode is a negative example ({evidence_status})."
        )
    return (
        f"Do not treat the {skill_name} behavior for {task_family} as a proven lesson: "
        f"the episode completed without verifier-passing or human-positive evidence ({evidence_status})."
    )


def record_learning_example(
    *,
    run_id: int | None,
    genome: dict,
    example_type: str | None = None,
    lesson: str | None = None,
) -> dict | None:
    """Persist a scoped negative/unverified example instead of promoting it."""
    scope = _learning_scope(
        user_id=genome.get("user_id"),
        org_id=genome.get("org_id"),
        visibility=genome.get("visibility"),
    )
    resolved_example_type = example_type or _learning_example_type(genome)
    if resolved_example_type not in {"negative", "unverified"}:
        raise ValueError("learning examples must be negative or unverified")

    try:
        with UnitOfWork() as uow:
            existing = uow.session.scalars(
                select(LearningExample).where(
                    LearningExample.run_id == run_id,
                    LearningExample.example_type == resolved_example_type,
                    LearningExample.evidence_status == genome.get("evidence_status"),
                )
            ).first()
            if existing:
                return {
                    "id": existing.id,
                    "run_id": existing.run_id,
                    "example_type": existing.example_type,
                    "evidence_status": existing.evidence_status,
                    "duplicate": True,
                }

            row = LearningExample(
                run_id=run_id,
                genome_id=genome.get("id"),
                user_id=scope["user_id"],
                org_id=scope["org_id"],
                visibility=scope["visibility"],
                example_type=resolved_example_type,
                evidence_status=genome.get("evidence_status") or "unverified",
                task_family=genome.get("task_family") or "general",
                target_family=genome.get("target_family") or "unspecified",
                skill_name=genome.get("skill_name"),
                lesson=lesson or _learning_example_lesson(genome),
                evidence=genome.get("evidence_gate") or {},
                signals={
                    "context_profile": genome.get("context_profile") or {},
                    "verifier_outcome": genome.get("verifier_outcome") or {},
                    "retrieval_profile": genome.get("retrieval_profile") or {},
                    "satisfaction_proxy": genome.get("satisfaction_proxy"),
                    "success": genome.get("success"),
                    "rework_required": genome.get("rework_required"),
                },
            )
            uow.session.add(row)
            uow.session.flush()
            return {
                "id": row.id,
                "run_id": row.run_id,
                "example_type": row.example_type,
                "evidence_status": row.evidence_status,
                "duplicate": False,
            }
    except Exception as exc:
        logger.debug("Learning example persistence failed for run %s: %s", run_id, exc)
        return None


def _recommendations_from_genome(run_id: int, genome: dict) -> list[dict]:
    if not genome.get("positive_learning_allowed"):
        return []

    context = genome["context_profile"]
    verifier = genome["verifier_outcome"]
    retrieval = genome["retrieval_profile"]
    predictions = genome["prediction_profile"]
    task_family = genome["task_family"]
    target_family = genome["target_family"]
    readiness_score = genome["satisfaction_proxy"]

    recommendations: list[dict] = []
    source_refs = _source_refs(run_id, genome)

    if context["cognitive_miss_count"] > 0 or not context["brain_recall_used"]:
        recommendations.append({
            "promotion_type": "route_override",
            "policy_payload": {
                "policy_key": f"route_override:{task_family}:{target_family}",
                "policy_scope": {
                    "task_family": task_family,
                    "target_family": target_family,
                },
                "recommendation": "shadow-preload-recall-before-full-route",
                "reason": "run had cognitive misses or no recall preload",
                "shadow_enabled": False,
            },
        })

        recommendations.append({
            "promotion_type": "retrieval_boost",
            "policy_payload": {
                "policy_key": f"retrieval_boost:{task_family}:{genome.get('skill_name') or 'general'}",
                "policy_scope": {
                    "task_family": task_family,
                    "skill_name": genome.get("skill_name"),
                },
                "recommendation": "raise-preload-budget-and-preload-relevant-memory",
                "reason": "run had memory misses or skipped recall",
                "shadow_enabled": False,
            },
        })

    if verifier["verification_attempts"] > 0 or (verifier.get("verification_last_error") and not genome["success"]):
        recommendations.append({
            "promotion_type": "verifier_requirement",
            "policy_payload": {
                "policy_key": f"verifier_requirement:{verifier['contract_type']}:{target_family}",
                "policy_scope": {
                    "contract_type": verifier["contract_type"],
                    "target_family": target_family,
                },
                "recommendation": "require-verifier-before-settlement",
                "reason": "verification attempts or verifier errors were recorded",
                "shadow_enabled": False,
            },
        })

    if genome.get("skill_name") and not genome["success"]:
        recommendations.append({
            "promotion_type": "skill_guardrail",
            "policy_payload": {
                "policy_key": f"skill_guardrail:{genome['skill_name']}",
                "policy_scope": {
                    "skill_name": genome["skill_name"],
                },
                "recommendation": "require-skill-guardrail-before-repeat-execution",
                "reason": "failed run should not harden into default behavior",
                "shadow_enabled": False,
            },
        })

    if retrieval["omission_risk_score"] >= 0.5 or retrieval["contradiction_risk_score"] >= 0.5:
        recommendations.append({
            "promotion_type": "retrieval_boost",
            "policy_payload": {
                "policy_key": f"retrieval_boost:{task_family}:{target_family}:shadow",
                "policy_scope": {
                    "task_family": task_family,
                    "target_family": target_family,
                },
                "recommendation": "shadow-rerank-high-risk-retrieval-paths",
                "reason": "retrieval decision logs show omission/contradiction risk",
                "shadow_enabled": False,
            },
        })

    if predictions["count"] > 0 and predictions["avg_prediction_error"] >= 0.3:
        recommendations.append({
            "promotion_type": "scout_rule",
            "policy_payload": {
                "policy_key": f"scout_rule:{task_family}:{genome['token_cost_bucket']}",
                "policy_scope": {
                    "task_family": task_family,
                    "token_cost_bucket": genome["token_cost_bucket"],
                },
                "recommendation": "shadow-add-scout-check-before-costly-runs",
                "reason": "prediction loop shows significant miss on this run family",
                "shadow_enabled": False,
            },
        })

    return [
        {
            "promotion_type": rec["promotion_type"],
            "source_kind": "run_genome",
            "source_refs": source_refs,
            "policy_payload": rec["policy_payload"],
            "evidence": {
                "run_id": run_id,
                "genome_hash": genome["genome_hash"],
                "promotion_readiness": readiness_score,
                "evidence_gate": genome.get("evidence_gate") or {},
                "evidence_status": genome.get("evidence_status"),
                "signals": {
                    "context_profile": context,
                    "verifier_outcome": verifier,
                    "retrieval_profile": retrieval,
                    "prediction_profile": predictions,
                    "satisfaction_proxy": genome["satisfaction_proxy"],
                    "success": genome["success"],
                    "rework_required": genome["rework_required"],
                },
            },
            "shadow_metrics": {
                "shadow_enabled": bool(rec["policy_payload"].get("shadow_enabled", False)),
                "satisfaction_proxy": genome["satisfaction_proxy"],
                "readiness_score": readiness_score,
                "activation_thresholds": PROMOTION_ACTIVATION_THRESHOLDS.get(rec["promotion_type"], {}),
                "genome_hash": genome["genome_hash"],
                "success": genome["success"],
                "rework_required": genome["rework_required"],
            },
        }
        for rec in recommendations
    ]


def recommend_policy_promotions(
    run_id: int,
    *,
    genome: dict | None = None,
    shadow_enabled: bool = False,
) -> list[dict]:
    """Persist conservative promotion recommendations for a run."""
    genome = genome or persist_run_genome(run_id)
    if not genome:
        return []
    if not genome.get("positive_learning_allowed"):
        return []

    try:
        recommendations = _recommendations_from_genome(run_id, genome)
        persisted: list[dict] = []
        for rec in recommendations:
            persisted_row = record_policy_promotion(
                promotion_type=rec["promotion_type"],
                source_kind=rec["source_kind"],
                source_refs=rec["source_refs"],
                policy_payload=rec["policy_payload"],
                evidence=rec["evidence"],
                user_id=genome.get("user_id"),
                org_id=genome.get("org_id"),
                visibility=genome.get("visibility"),
                shadow_metrics={
                    **rec["shadow_metrics"],
                    "shadow_enabled": shadow_enabled or rec["shadow_metrics"].get("shadow_enabled", False),
                },
                shadow_enabled=shadow_enabled or rec["shadow_metrics"].get("shadow_enabled", False),
            )
            if persisted_row:
                persisted.append({
                    **persisted_row,
                    "promotion_type": rec["promotion_type"],
                    "policy_payload": rec["policy_payload"],
                })
        return persisted
    except Exception as exc:
        logger.debug("Policy recommendation generation failed for run %s: %s", run_id, exc)
        return []


def record_run_learning_artifacts(
    run_id: int,
    *,
    shadow_enabled: bool = False,
) -> dict:
    """Persist a scoped genome plus either gated promotions or a separate example."""
    genome = persist_run_genome(run_id)
    promotions: list[dict] = []
    examples: list[dict] = []
    if genome:
        if genome.get("positive_learning_allowed"):
            promotions = recommend_policy_promotions(run_id, genome=genome, shadow_enabled=shadow_enabled)
        else:
            example = record_learning_example(run_id=run_id, genome=genome)
            if example:
                examples.append(example)
    return {
        "genome": genome,
        "promotions": promotions,
        "examples": examples,
    }

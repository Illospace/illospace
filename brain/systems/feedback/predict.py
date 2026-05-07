"""
Prediction-Reward Loop — The learning mechanism that makes the system smarter.

Before each run:  Predict outcome (quality, tokens, time)
After each run:   Compare prediction vs actual
On prediction error:   Extract insight → encode as heuristic or memory

This is how the system learns without explicit feedback:
- Prediction errors are the learning signal (like dopamine in biological brains)
- High error = something surprising happened = strong learning opportunity
- Low error = prediction model is well-calibrated = nothing new to learn

The prediction model itself improves over time as it accumulates
strategy events and prediction artifacts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text

from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow, AgentRunRow
from brain.systems.feedback.heuristics import task_family_from_text, task_markers_from_text
from brain.platform.db.repositories.unit_of_work import UnitOfWork

logger = logging.getLogger("feedback.predict")


@dataclass
class Prediction:
    """Pre-run prediction."""
    predicted_quality: float       # 0-1 expected success
    predicted_tokens: int          # expected total tokens
    predicted_duration_sec: int    # expected time
    confidence: float              # how confident in prediction
    basis: str                     # explanation for debugging
    skill_name: str | None = None


@dataclass
class RewardSignal:
    """Post-run reward computation."""
    quality: float                 # 0-1 actual quality
    efficiency: float              # predicted/actual tokens ratio
    prediction_error: float        # composite error
    quality_error: float           # |predicted - actual| quality
    token_error: float             # |predicted - actual| / predicted tokens
    insight: str | None = None     # extracted learning (if error is high)
    should_encode: bool = False    # whether this insight is worth storing


def _jsonable(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    return value


def _recording_section(recordings: dict | None, key: str) -> dict:
    if not isinstance(recordings, dict):
        return {}
    value = recordings.get(key) or {}
    return dict(value) if isinstance(value, dict) else {}


def _build_source_bundle(
    *,
    recordings: dict | None,
    run: object | None,
    task: str,
    task_family: str,
    source_skill: str | None,
    contract_type: str | None,
    target_status: str | None,
    workspace_fingerprint: str | None,
    runtime_fingerprint: str | None,
    context_shape: list[str],
) -> dict:
    summary = _recording_section(recordings, "run_summary")
    flight_recorder = _recording_section(recordings, "flight_recorder")
    recorder_context = _recording_section(flight_recorder, "context")
    recorder_target = _recording_section(flight_recorder, "target")
    recorder_routing = _recording_section(flight_recorder, "routing")

    summary_bundle = {
        "schema_version": summary.get("schema_version", 1),
        "settlement_state": summary.get("settlement_state"),
        "run_status": summary.get("run_status"),
        "skill_used": summary.get("skill_used"),
        "provider_used": summary.get("provider_used"),
        "model_used": summary.get("model_used"),
        "scout_class": summary.get("scout_class"),
        "verifier_status": summary.get("verifier_status"),
        "queue_wait_ms": summary.get("queue_wait_ms"),
        "planner_duration_ms": summary.get("planner_duration_ms"),
        "total_duration_ms": summary.get("total_duration_ms"),
        "tool_calls_count": summary.get("tool_calls_count"),
        "run_steps_count": summary.get("run_steps_count"),
        "tokens_total": summary.get("tokens_total"),
        "estimated_cost": summary.get("estimated_cost"),
        "shadow_group_id": summary.get("shadow_group_id"),
    }
    recorder_bundle = {
        "brain_context_loaded": recorder_context.get("brain_context_loaded"),
        "brain_recall_used": recorder_context.get("brain_recall_used"),
        "brain_skills_used": recorder_context.get("brain_skills_used"),
        "attention_required": recorder_context.get("attention_required"),
        "preloaded_memory_count": recorder_context.get("preloaded_memory_count"),
        "workspace_mode": recorder_context.get("workspace_mode"),
        "warm_start_used": recorder_context.get("warm_start_used"),
        "contract_type": recorder_target.get("contract_type") or contract_type,
        "target_status": recorder_target.get("status") or target_status,
        "worktree_path": getattr(run, "worktree_path", None),
        "worktree_branch": getattr(run, "worktree_branch", None),
        "skill_used": recorder_routing.get("skill_used") or source_skill,
        "model_used": recorder_routing.get("model_used"),
        "thinking_used": recorder_routing.get("thinking_used"),
    }
    return {
        "schema_version": 1,
        "task": task[:500],
        "task_family": task_family,
        "task_markers": task_markers_from_text(task),
        "source_skill": source_skill,
        "contract_type": contract_type,
        "target_status": target_status,
        "workspace_fingerprint": workspace_fingerprint,
        "runtime_fingerprint": runtime_fingerprint,
        "context_shape": list(context_shape),
        "summary": summary_bundle,
        "recording": recorder_bundle,
        "run_id": getattr(run, "id", None),
    }


def build_habit_signature(
    task: str,
    *,
    skill_name: str | None = None,
    run: object | None = None,
    agent_run_record: object | None = None,
    recordings: dict | None = None,
    success: bool | None = None,
    duration_sec: int | None = None,
    tokens_used: int | None = None,
    cost: float | None = None,
) -> dict:
    """Build a passive, queryable signature bundle for habit mining.

    The bundle stays intentionally narrower than a skill: it captures the task
    family plus a small set of run/runtime fingerprints so later compiler
    passes can group only very similar successful runs.
    """
    run_obj = run
    if run_obj is None and agent_run_record is not None and getattr(agent_run_record, "run_id", None):
        # Keep this helper usable without requiring a live run row.
        run_obj = None

    summary = _recording_section(recordings, "run_summary")
    flight_recorder = _recording_section(recordings, "flight_recorder")
    recorder_context = _recording_section(flight_recorder, "context")
    recorder_target = _recording_section(flight_recorder, "target")
    recorder_routing = _recording_section(flight_recorder, "routing")

    task_family = task_family_from_text(task)

    contract_type = (
        getattr(run_obj, "contract_type", None)
        or recorder_target.get("contract_type")
    )
    target_status = (
        getattr(run_obj, "target_status", None)
        or recorder_target.get("status")
    )
    workspace_path = getattr(run_obj, "worktree_path", None)
    workspace_branch = getattr(run_obj, "worktree_branch", None)
    workspace_mode = getattr(run_obj, "workspace_mode", None) or recorder_context.get("workspace_mode")
    runtime_model = (
        getattr(run_obj, "model_used", None)
        or getattr(agent_run_record, "model_used", None)
        or recorder_routing.get("model_used")
    )
    runtime_thinking = (
        getattr(run_obj, "thinking_used", None)
        or getattr(agent_run_record, "thinking_used", None)
        or recorder_routing.get("thinking_used")
    )
    source_skill = skill_name or getattr(run_obj, "skill_used", None) or getattr(agent_run_record, "skill_used", None) or recorder_routing.get("skill_used") or summary.get("skill_used")

    workspace_fingerprint = None
    for candidate in (workspace_branch, workspace_path, workspace_mode):
        if candidate:
            workspace_fingerprint = str(candidate).strip()
            break

    runtime_fingerprint = None
    if runtime_model or runtime_thinking:
        runtime_fingerprint = "|".join(
            part for part in [str(runtime_model or "").strip(), str(runtime_thinking or "").strip()] if part
        ) or None

    context_shape = []
    if getattr(run_obj, "brain_context_loaded", False) or getattr(agent_run_record, "brain_context_loaded", False):
        context_shape.append("brain_context")
    if getattr(run_obj, "attention_required", False) or getattr(agent_run_record, "attention_required", False):
        context_shape.append("attention_required")
    if getattr(run_obj, "preloaded_memory_count", 0) or getattr(agent_run_record, "preloaded_memory_count", 0):
        context_shape.append("preloaded_memory")
    if source_skill:
        context_shape.append(f"skill:{source_skill}")
    if contract_type:
        context_shape.append(f"contract:{contract_type}")
    if target_status:
        context_shape.append(f"target:{target_status}")
    if workspace_fingerprint:
        context_shape.append(f"workspace:{workspace_fingerprint}")
    if summary.get("settlement_state"):
        context_shape.append(f"settlement:{summary['settlement_state']}")
    if summary.get("verifier_status"):
        context_shape.append(f"verifier:{summary['verifier_status']}")
    if summary.get("scout_class"):
        context_shape.append(f"scout:{summary['scout_class']}")
    if summary.get("shadow_group_id"):
        context_shape.append(f"shadow_group:{summary['shadow_group_id']}")
    if recorder_context.get("brain_context_loaded"):
        context_shape.append("recorded:brain_context")
    if recorder_context.get("attention_required"):
        context_shape.append("recorded:attention_required")
    if recorder_context.get("preloaded_memory_count"):
        context_shape.append("recorded:preloaded_memory")
    if workspace_mode:
        context_shape.append(f"workspace_mode:{workspace_mode}")
    if runtime_model:
        context_shape.append(f"model:{runtime_model}")
    if runtime_thinking:
        context_shape.append(f"thinking:{runtime_thinking}")
    for marker in task_markers_from_text(task, max_terms=4):
        context_shape.append(f"task:{marker}")
    context_shape = list(dict.fromkeys(context_shape))

    from brain.systems.runs.task_analysis import task_hash

    source_bundle = _build_source_bundle(
        recordings=recordings,
        run=run_obj,
        task=task,
        task_family=task_family,
        source_skill=source_skill,
        contract_type=contract_type,
        target_status=target_status,
        workspace_fingerprint=workspace_fingerprint,
        runtime_fingerprint=runtime_fingerprint,
        context_shape=context_shape,
    )

    payload = {
        "schema_version": 1,
        "task": task[:500],
        "task_family": task_family,
        "task_hash": task_hash(task),
        "source_skill": source_skill,
        "contract_type": contract_type,
        "target_status": target_status,
        "workspace_fingerprint": workspace_fingerprint,
        "runtime_fingerprint": runtime_fingerprint,
        "context_shape": context_shape,
        "source_run_id": getattr(run_obj, "id", None),
        "source_strategy": getattr(agent_run_record, "strategy", None) or "agent_run",
        "source_bundle": source_bundle,
        "success": success,
        "duration_sec": duration_sec,
        "tokens_used": tokens_used,
        "cost": cost,
    }
    payload["signature_hash"] = hashlib.sha256(
        json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return payload


def predict_outcome(
    task: str,
    skill_name: str | None = None,
) -> Prediction:
    """Predict run outcome based on similar past runs.

    Uses historical data from strategy events and prediction artifacts
    to estimate what will happen. Falls back to heuristics if no history.
    """
    similar_runs = _find_similar_history(task, skill_name)

    prediction = None

    if similar_runs and len(similar_runs) >= 3:
        # History-based prediction
        avg_tokens = sum(d["tokens_used"] for d in similar_runs) / len(similar_runs)
        success_count = sum(1 for d in similar_runs if d["success"])
        avg_quality = success_count / len(similar_runs)
        avg_duration = sum(d.get("duration_sec", 60) for d in similar_runs) / len(similar_runs)
        confidence = min(0.9, 0.3 + len(similar_runs) * 0.1)
        basis = f"Based on {len(similar_runs)} similar past runs"

        prediction = Prediction(
            predicted_quality=round(avg_quality, 3),
            predicted_tokens=int(avg_tokens),
            predicted_duration_sec=int(avg_duration),
            confidence=round(confidence, 3),
            basis=basis,
            skill_name=skill_name,
        )

    # Skill-based prediction
    if not prediction and skill_name:
        skill_stats = _get_skill_stats(skill_name)
        if skill_stats:
            prediction = Prediction(
                predicted_quality=round(skill_stats.get("success_rate", 0.7), 3),
                predicted_tokens=int(skill_stats.get("avg_tokens", 15000)),
                predicted_duration_sec=int(skill_stats.get("avg_duration", 60)),
                confidence=0.4,
                basis=f"Based on skill '{skill_name}' stats ({skill_stats.get('use_count', 0)} uses)",
                skill_name=skill_name,
            )

    # Default heuristic prediction
    if not prediction:
        prediction = Prediction(
            predicted_quality=0.7,
            predicted_tokens=25000,
            predicted_duration_sec=90,
            confidence=0.2,
            basis="Default heuristic (no similar history)",
            skill_name=skill_name,
        )

    return _adjust_for_recent_failures(prediction)


def _adjust_for_recent_failures(prediction: Prediction) -> Prediction:
    """Adjust prediction based on recent error classifications for this skill.

    If the same skill has recent failures, lower predicted quality.
    If context_overflow occurred, increase predicted tokens.
    """
    if not prediction.skill_name:
        return prediction
    try:
        with UnitOfWork() as uow:
            cutoff = datetime.now(timezone.utc) - timedelta(days=7)
            rows = uow.session.scalars(
                select(AgentRunRow)
                .where(AgentRunRow.status == "failed", AgentRunRow.created_at > cutoff)
            ).all()
            failures: dict[str, int] = {}
            for row in rows:
                metadata = dict(row.metadata_ or {})
                if metadata.get("skill_used") != prediction.skill_name:
                    continue
                postmortem = metadata.get("postmortem") or metadata.get("error_classification") or {}
                category = postmortem.get("category") if isinstance(postmortem, dict) else None
                if category:
                    failures[str(category)] = failures.get(str(category), 0) + 1

        if not failures:
            return prediction

        total_failures = sum(failures.values())
        # Lower quality prediction if recent failures
        quality_penalty = min(0.3, total_failures * 0.05)
        prediction.predicted_quality = max(0.1, prediction.predicted_quality - quality_penalty)

        # Increase token prediction if context overflow occurred
        if failures.get("context_overflow", 0) > 0:
            prediction.predicted_tokens = int(prediction.predicted_tokens * 1.3)

        prediction.basis += f" (adjusted: {total_failures} recent failures)"
    except Exception as exc:
        logger.debug("Failure adjustment query failed: %s", exc)
    return prediction


def compute_reward(
    prediction: Prediction,
    actual_tokens: int,
    actual_status: str,
    actual_duration_sec: int = 0,
    follow_up_count: int = 0,
) -> RewardSignal:
    """Compute reward signal from prediction vs actual outcome.

    The prediction error is the key learning signal:
    - High error → something surprising → extract insight → store
    - Low error → well-calibrated → nothing to learn
    """
    # Quality: success + no corrections needed
    quality = 1.0 if actual_status == "completed" else 0.0
    quality = max(0.0, quality - follow_up_count * 0.15)  # penalize needing follow-ups

    # Quality error
    quality_error = abs(prediction.predicted_quality - quality)

    # Token error (relative)
    if prediction.predicted_tokens > 0:
        token_error = abs(prediction.predicted_tokens - actual_tokens) / prediction.predicted_tokens
    else:
        token_error = 0.5

    # Efficiency (higher is better — predicted vs actual tokens)
    if actual_tokens > 0:
        efficiency = min(2.0, prediction.predicted_tokens / actual_tokens)
    else:
        efficiency = 1.0

    # Composite prediction error
    prediction_error = quality_error * 0.6 + min(1.0, token_error) * 0.4

    # Extract insight if error is significant
    insight = None
    should_encode = False

    if prediction_error > 0.3:
        should_encode = True
        if quality < prediction.predicted_quality - 0.3:
            insight = (
                f"Quality overestimated for '{prediction.skill_name or 'unknown'}' skill "
                f"(predicted {prediction.predicted_quality:.1f}, got {quality:.1f})."
            )
        elif quality > prediction.predicted_quality + 0.3:
            insight = (
                f"Quality underestimated for '{prediction.skill_name or 'unknown'}' skill "
                f"(predicted {prediction.predicted_quality:.1f}, got {quality:.1f}). "
                f"The skill performs better than expected."
            )
        elif actual_tokens > prediction.predicted_tokens * 1.5:
            insight = (
                f"Token cost underestimated for '{prediction.skill_name or 'unknown'}' skill "
                f"(predicted {prediction.predicted_tokens:,}, used {actual_tokens:,})."
            )
        elif actual_tokens < prediction.predicted_tokens * 0.5:
            insight = (
                f"Token cost overestimated for '{prediction.skill_name or 'unknown'}' skill "
                f"(predicted {prediction.predicted_tokens:,}, used {actual_tokens:,}). "
                f"Could use a lower-cost approach next time."
            )

    return RewardSignal(
        quality=round(quality, 3),
        efficiency=round(efficiency, 3),
        prediction_error=round(prediction_error, 3),
        quality_error=round(quality_error, 3),
        token_error=round(min(1.0, token_error), 3),
        insight=insight,
        should_encode=should_encode,
    )


# ── Persistence ──────────────────────────────────────────────

def save_prediction(
    run_id: int,
    idea_id: str | None,
    prediction: Prediction,
) -> int | None:
    """Save a prediction artifact. Returns artifact row ID."""
    try:
        with UnitOfWork() as uow:
            run = uow.session.get(AgentRunRow, int(run_id))
            artifact = AgentRunArtifactRow(
                run_id=int(run_id),
                root_run_id=getattr(run, "root_run_id", None) or int(run_id),
                artifact_type="prediction",
                title="Outcome prediction",
                payload={
                    "idea_id": idea_id,
                    "skill_name": prediction.skill_name,
                    "strategy": "pipeline",
                    "predicted_quality": prediction.predicted_quality,
                    "predicted_tokens": prediction.predicted_tokens,
                    "predicted_duration_sec": prediction.predicted_duration_sec,
                    "confidence": prediction.confidence,
                    "basis": prediction.basis,
                    "resolved_at": None,
                },
                visibility="internal",
            )
            uow.session.add(artifact)
            uow.session.flush()
            return int(artifact.id)
    except Exception as e:
        logger.warning(f"Failed to save prediction: {e}")
        return None


def resolve_prediction(
    run_id: int,
    actual_tokens: int,
    actual_status: str,
    actual_duration_sec: int,
    reward: RewardSignal,
):
    """Update prediction with actual results and computed reward."""
    try:
        with UnitOfWork() as uow:
            artifacts = uow.session.scalars(
                select(AgentRunArtifactRow)
                .where(
                    AgentRunArtifactRow.run_id == int(run_id),
                    AgentRunArtifactRow.artifact_type == "prediction",
                )
                .order_by(AgentRunArtifactRow.created_at.asc(), AgentRunArtifactRow.id.asc())
            ).all()
            resolved_at = datetime.now(timezone.utc).isoformat()
            for artifact in artifacts:
                payload = dict(artifact.payload or {})
                if payload.get("resolved_at"):
                    continue
                payload.update(
                    {
                        "actual_quality": reward.quality,
                        "actual_tokens": actual_tokens,
                        "actual_duration_sec": actual_duration_sec,
                        "actual_status": actual_status,
                        "prediction_error": reward.prediction_error,
                        "quality_error": reward.quality_error,
                        "token_error": reward.token_error,
                        "reward_signal": reward.quality - reward.prediction_error,
                        "insight": reward.insight,
                        "resolved_at": resolved_at,
                    }
                )
                artifact.payload = payload
    except Exception as e:
        logger.warning(f"Failed to resolve prediction: {e}")


def encode_insight(reward: RewardSignal, skill_name: str | None = None):
    """Encode a prediction insight as a brain memory and/or heuristic.

    Called when prediction error is high enough to be worth learning from.
    """
    if not reward.insight or not reward.should_encode:
        return

    # Encode as brain memory (lesson type, high salience for surprises)
    try:
        from brain.app.cli.memory import add_memory
        salience = min(8.0, 5.0 + reward.prediction_error * 3)
        add_memory(
            content=reward.insight,
            memory_type="lesson",
            salience=salience,
            emotion="curious" if reward.quality > 0.5 else "concerned",
            source="prediction_loop",
            tags=["prediction", "learning", skill_name or "general"],
        )
        logger.info(f"Encoded prediction insight (salience={salience:.1f}): {reward.insight[:80]}...")
    except Exception as e:
        logger.debug(f"Failed to encode insight as memory: {e}")


# ── Historical Lookups ───────────────────────────────────────

def _find_similar_history(task: str, skill_name: str | None) -> list[dict]:
    """Find similar past run outcomes for prediction."""
    try:
        with UnitOfWork() as uow:
            if not skill_name:
                # Without skill, we can't find similar history reliably
                return []
            cutoff = datetime.now(timezone.utc) - timedelta(days=30)
            events = uow.session.scalars(
                select(AgentRunEventRow)
                .where(
                    AgentRunEventRow.event_type == "run.learning.strategy_observed",
                    AgentRunEventRow.created_at > cutoff,
                )
                .order_by(AgentRunEventRow.created_at.desc(), AgentRunEventRow.id.desc())
                .limit(100)
            ).all()
            history: list[dict] = []
            task_markers = set(task_markers_from_text(task, max_terms=6))
            for event in events:
                payload = dict(event.payload or {})
                if payload.get("skill_name") != skill_name:
                    continue
                signature = payload.get("task_signature") if isinstance(payload.get("task_signature"), dict) else {}
                markers = set(signature.get("task_markers") or [])
                if task_markers and markers and not (task_markers & markers):
                    continue
                history.append(
                    {
                        "success": bool(payload.get("success")),
                        "tokens_used": int(payload.get("tokens_used") or 0),
                        "duration_sec": int(payload.get("duration_sec") or 60),
                    }
                )
                if len(history) >= 20:
                    break
            return history
    except Exception:
        return []


def _get_skill_stats(skill_name: str) -> dict | None:
    """Get aggregate stats for a skill."""
    try:
        with UnitOfWork() as uow:
            row = uow.session.execute(text("""
                SELECT use_count, success_count, failure_count,
                       avg_duration_sec, confidence, fitness_score
                FROM skills WHERE name = :name AND NOT archived
            """), {"name": skill_name}).mappings().first()
            if row:
                use = row["use_count"] or 0
                succ = row["success_count"] or 0
                return {
                    "use_count": use,
                    "success_rate": succ / max(use, 1),
                    "avg_duration": row["avg_duration_sec"] or 60,
                    "avg_tokens": 15000,  # TODO: track per-skill token avg
                }
    except Exception:
        pass
    return None


# ── Calibration Stats ────────────────────────────────────────

def get_prediction_calibration(days: int = 30) -> dict:
    """Get prediction accuracy stats for the dashboard.

    Shows how well the system predicts outcomes, broken down by strategy.
    """
    try:
        with UnitOfWork() as uow:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            artifacts = uow.session.scalars(
                select(AgentRunArtifactRow)
                .where(
                    AgentRunArtifactRow.artifact_type == "prediction",
                    AgentRunArtifactRow.created_at > cutoff,
                )
            ).all()
            buckets: dict[str, list[dict[str, Any]]] = {}
            for artifact in artifacts:
                payload = dict(artifact.payload or {})
                if not payload.get("resolved_at"):
                    continue
                buckets.setdefault(str(payload.get("strategy") or "pipeline"), []).append(payload)
            return {
                strategy: {
                    "total": len(rows),
                    "avg_prediction_error": round(sum(float(row.get("prediction_error") or 0) for row in rows) / max(1, len(rows)), 3),
                    "avg_quality_error": round(sum(float(row.get("quality_error") or 0) for row in rows) / max(1, len(rows)), 3),
                    "avg_token_error": round(sum(float(row.get("token_error") or 0) for row in rows) / max(1, len(rows)), 3),
                    "actual_success_rate": round(sum(1.0 if float(row.get("actual_quality") or 0) > 0.5 else 0.0 for row in rows) / max(1, len(rows)), 3),
                    "avg_predicted_quality": round(sum(float(row.get("predicted_quality") or 0) for row in rows) / max(1, len(rows)), 3),
                }
                for strategy, rows in buckets.items()
            }
    except Exception as e:
        logger.debug(f"Prediction calibration query failed: {e}")
        return {}


def summarize_prediction_artifacts(run_id: int) -> dict:
    """Summarize persisted prediction artifacts for a run."""
    try:
        with UnitOfWork() as uow:
            artifacts = uow.session.scalars(
                select(AgentRunArtifactRow)
                .where(
                    AgentRunArtifactRow.run_id == int(run_id),
                    AgentRunArtifactRow.artifact_type == "prediction",
                )
                .order_by(AgentRunArtifactRow.created_at.asc(), AgentRunArtifactRow.id.asc())
            ).all()
            rows = [dict(artifact.payload or {}) for artifact in artifacts]
            resolved = [row for row in rows if row.get("resolved_at")]
            actual_tokens = [float(row.get("actual_tokens") or 0) for row in resolved if row.get("actual_tokens") is not None]
            actual_duration = [
                float(row.get("actual_duration_sec") or 0)
                for row in resolved
                if row.get("actual_duration_sec") is not None
            ]
            actual_quality = [
                float(row.get("actual_quality") or 0)
                for row in resolved
                if row.get("actual_quality") is not None
            ]
            prediction_error = [
                float(row.get("prediction_error") or 0)
                for row in resolved
                if row.get("prediction_error") is not None
            ]
            return {
                "total_count": len(rows),
                "resolved_count": len(resolved),
                "insight_count": sum(1 for row in rows if row.get("insight")),
                "avg_prediction_error": (sum(prediction_error) / len(prediction_error)) if prediction_error else None,
                "avg_actual_quality": (sum(actual_quality) / len(actual_quality)) if actual_quality else None,
                "avg_actual_tokens": (sum(actual_tokens) / len(actual_tokens)) if actual_tokens else None,
                "avg_actual_duration_sec": (sum(actual_duration) / len(actual_duration)) if actual_duration else None,
                "latest_status": next((row.get("actual_status") for row in reversed(resolved) if row.get("actual_status")), None),
            }
    except Exception as exc:
        logger.debug("Prediction summary query failed for run %s: %s", run_id, exc)
        return {
            "total_count": 0,
            "resolved_count": 0,
            "insight_count": 0,
            "avg_prediction_error": None,
            "avg_actual_quality": None,
            "avg_actual_tokens": None,
            "avg_actual_duration_sec": None,
            "latest_status": None,
        }

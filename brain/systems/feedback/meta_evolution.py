"""
Meta-Evolution — System evaluates and improves its own evolution process.

Answers questions like:
- Are predictions getting more accurate over time?
- Are heuristics useful or just noise?
- Is strategy selection well-calibrated?
- Which parts of the nightly pipeline produce the most value?

Runs as a nightly step after all other evolution phases.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import text

from brain.platform.db.repositories.unit_of_work import UnitOfWork, open_unit_of_work

logger = logging.getLogger("feedback.meta_evolution")


@dataclass
class EvolutionMetrics:
    """Rolling 7-day evolution metrics."""
    period_start: date
    period_end: date
    prediction_accuracy: float = 0.0      # 1 - avg prediction error
    prediction_calibration: float = 0.0   # how well confidence matches actual success
    heuristic_survival_rate: float = 0.0  # % of heuristics surviving pruning
    heuristic_avg_confidence: float = 0.0
    strategy_success_rates: dict = field(default_factory=dict)  # per-strategy
    strategy_token_efficiency: dict = field(default_factory=dict)  # tokens vs predicted
    skill_fitness_trend: float = 0.0       # avg fitness change over period
    memory_retrieval_quality: float = 0.0  # hit rate from retrieval_log
    total_runs: int = 0
    total_heuristics_active: int = 0
    total_skills_active: int = 0


@dataclass
class MetaInsight:
    """A finding from meta-evolution analysis."""
    category: str       # prediction, heuristic, strategy, fitness, retrieval
    severity: str       # info, warning, regression
    message: str
    metric_name: str
    current_value: float
    previous_value: float
    suggested_action: str | None = None


def compute_evolution_metrics(period_end: date | None = None, window_days: int = 7) -> EvolutionMetrics:
    """Compute rolling evolution metrics for a time window.

    Args:
        period_end: End of window (default: today)
        window_days: Window size in days
    """
    end = period_end or date.today()
    start = end - timedelta(days=window_days)

    metrics = EvolutionMetrics(period_start=start, period_end=end)

    with open_unit_of_work(UnitOfWork) as uow:
        session = uow.session

        # ── Prediction artifact accuracy ──
        result = session.execute(text("""
            SELECT
                COUNT(*) as total,
                AVG(CAST(payload->>'prediction_error' AS FLOAT)) as avg_error,
                AVG(CAST(payload->>'quality_error' AS FLOAT)) as avg_quality_error,
                AVG(ABS(CAST(payload->>'confidence' AS FLOAT) - CASE WHEN payload->>'actual_status' = 'completed' THEN 1.0 ELSE 0.0 END)) as calibration_gap
            FROM agent_run_artifacts
            WHERE created_at::date BETWEEN :start AND :end
              AND artifact_type = 'prediction'
              AND payload->>'prediction_error' IS NOT NULL
        """), {"start": start, "end": end})
        pred = result.mappings().first()
        if pred and pred["total"] and pred["total"] > 0:
            metrics.prediction_accuracy = 1.0 - float(pred["avg_error"] or 0)
            metrics.prediction_calibration = 1.0 - float(pred["calibration_gap"] or 0)
            metrics.total_runs = pred["total"]

        # ── Strategy success rates ──
        result = session.execute(text("""
            SELECT payload->>'strategy' as strategy, COUNT(*) as total,
                   AVG(CASE WHEN payload->>'success' IN ('true', '1', 'completed') THEN 1.0 ELSE 0.0 END) as success_rate,
                   AVG(CAST(payload->>'tokens_used' AS FLOAT)) as avg_tokens
            FROM agent_run_events
            WHERE created_at::date BETWEEN :start AND :end
              AND event_type = 'run.learning.strategy_observed'
            GROUP BY payload->>'strategy'
        """), {"start": start, "end": end})
        for row in result.mappings().all():
            metrics.strategy_success_rates[row["strategy"]] = round(float(row["success_rate"]), 3)
            metrics.strategy_token_efficiency[row["strategy"]] = int(row["avg_tokens"] or 0)

        # ── Heuristic health ──
        result = session.execute(text("""
            SELECT
                COUNT(*) FILTER (WHERE active) as active_count,
                COUNT(*) as total_count,
                AVG(confidence) FILTER (WHERE active) as avg_confidence,
                COUNT(*) FILTER (WHERE NOT active AND updated_at::date BETWEEN :start AND :end) as recently_pruned
            FROM skill_heuristics
        """), {"start": start, "end": end})
        heur = result.mappings().first()
        if heur and heur["total_count"] and heur["total_count"] > 0:
            metrics.total_heuristics_active = heur["active_count"] or 0
            metrics.heuristic_avg_confidence = round(float(heur["avg_confidence"] or 0), 3)
            recently_pruned = heur["recently_pruned"] or 0
            total_at_start = (heur["active_count"] or 0) + recently_pruned
            if total_at_start > 0:
                metrics.heuristic_survival_rate = round((heur["active_count"] or 0) / total_at_start, 3)
            else:
                metrics.heuristic_survival_rate = 1.0

        # ── Skill fitness trend ──
        result = session.execute(text("""
            SELECT COUNT(*) as total, AVG(fitness_score) as avg_fitness
            FROM skills WHERE NOT archived
        """))
        skills = result.mappings().first()
        metrics.total_skills_active = skills["total"] if skills else 0

        # Compare with fitness 7 days ago (from daily_metrics if available)
        result = session.execute(text("""
            SELECT AVG(fitness_score) as prev_fitness
            FROM skills WHERE NOT archived
        """))
        # We'll compute trend from run strategy-observation events instead.
        result = session.execute(text("""
            SELECT AVG(CASE WHEN payload->>'success' IN ('true', '1', 'completed') THEN 1.0 ELSE 0.0 END) as recent_sr
            FROM agent_run_events
            WHERE created_at::date BETWEEN :start AND :end
              AND event_type = 'run.learning.strategy_observed'
        """), {"start": start, "end": end})
        recent = result.mappings().first()
        result = session.execute(text("""
            SELECT AVG(CASE WHEN payload->>'success' IN ('true', '1', 'completed') THEN 1.0 ELSE 0.0 END) as prev_sr
            FROM agent_run_events
            WHERE created_at::date BETWEEN :start AND :end
              AND event_type = 'run.learning.strategy_observed'
        """), {"start": start - timedelta(days=window_days), "end": start})
        previous = result.mappings().first()

        if recent and previous and recent["recent_sr"] is not None and previous["prev_sr"] is not None:
            metrics.skill_fitness_trend = round(float(recent["recent_sr"]) - float(previous["prev_sr"]), 3)

        # ── Memory retrieval quality ──
        result = session.execute(text("""
            SELECT COUNT(*) as total,
                   COUNT(*) FILTER (WHERE feedback = 'hit') as hits
            FROM retrieval_log
            WHERE timestamp::date BETWEEN :start AND :end
        """), {"start": start, "end": end})
        ret = result.mappings().first()
        if ret and ret["total"] and ret["total"] > 0:
            metrics.memory_retrieval_quality = round(float(ret["hits"] or 0) / ret["total"], 3)

    return metrics


def compare_periods(current_end: date | None = None, window_days: int = 7) -> list[MetaInsight]:
    """Compare current period metrics with previous period to detect trends.

    Returns list of MetaInsight objects highlighting regressions and improvements.
    """
    end = current_end or date.today()
    current = compute_evolution_metrics(period_end=end, window_days=window_days)
    previous = compute_evolution_metrics(
        period_end=end - timedelta(days=window_days), window_days=window_days,
    )

    insights = []

    # Skip if no data in either period
    if current.total_runs == 0 and previous.total_runs == 0:
        return insights

    # ── Prediction accuracy trend ──
    if previous.total_runs >= 3:
        delta = current.prediction_accuracy - previous.prediction_accuracy
        if delta < -0.1:
            insights.append(MetaInsight(
                category="prediction",
                severity="regression",
                message=f"Prediction accuracy dropped {abs(delta):.1%} "
                        f"({previous.prediction_accuracy:.1%} → {current.prediction_accuracy:.1%})",
                metric_name="prediction_accuracy",
                current_value=current.prediction_accuracy,
                previous_value=previous.prediction_accuracy,
                suggested_action="Review recent prediction errors; model may need recalibration",
            ))
        elif delta > 0.1:
            insights.append(MetaInsight(
                category="prediction",
                severity="info",
                message=f"Prediction accuracy improved {delta:.1%}",
                metric_name="prediction_accuracy",
                current_value=current.prediction_accuracy,
                previous_value=previous.prediction_accuracy,
            ))

    # ── Heuristic survival ──
    if previous.total_heuristics_active >= 5:
        if current.heuristic_survival_rate < 0.5:
            insights.append(MetaInsight(
                category="heuristic",
                severity="warning",
                message=f"Heuristic survival rate low ({current.heuristic_survival_rate:.0%}) — "
                        "most heuristics being pruned. Extraction may be too noisy.",
                metric_name="heuristic_survival_rate",
                current_value=current.heuristic_survival_rate,
                previous_value=previous.heuristic_survival_rate,
                suggested_action="Increase extraction quality threshold or lower pruning aggressiveness",
            ))

    # ── Strategy calibration ──
    for strategy in set(list(current.strategy_success_rates.keys()) +
                        list(previous.strategy_success_rates.keys())):
        curr_sr = current.strategy_success_rates.get(strategy, 0)
        prev_sr = previous.strategy_success_rates.get(strategy, 0)
        if prev_sr > 0 and curr_sr < prev_sr - 0.15:
            insights.append(MetaInsight(
                category="strategy",
                severity="regression",
                message=f"Strategy '{strategy}' success rate dropped "
                        f"({prev_sr:.0%} → {curr_sr:.0%})",
                metric_name=f"strategy_{strategy}_success",
                current_value=curr_sr,
                previous_value=prev_sr,
                suggested_action=f"Consider escalating '{strategy}' tasks to higher-tier strategy",
            ))

    # ── Fitness trajectory ──
    if current.skill_fitness_trend < -0.1:
        insights.append(MetaInsight(
            category="fitness",
            severity="warning",
            message=f"Overall skill fitness declining ({current.skill_fitness_trend:+.1%})",
            metric_name="skill_fitness_trend",
            current_value=current.skill_fitness_trend,
            previous_value=0.0,
            suggested_action="Review failing skills; may need procedure updates or more training data",
        ))

    # ── Memory retrieval ──
    if previous.memory_retrieval_quality > 0:
        delta = current.memory_retrieval_quality - previous.memory_retrieval_quality
        if delta < -0.15:
            insights.append(MetaInsight(
                category="retrieval",
                severity="regression",
                message=f"Memory retrieval quality dropped {abs(delta):.0%}",
                metric_name="memory_retrieval_quality",
                current_value=current.memory_retrieval_quality,
                previous_value=previous.memory_retrieval_quality,
                suggested_action="Check embedding quality; may need re-indexing or embedding model update",
            ))

    return insights


def auto_tune_parameters(insights: list[MetaInsight]) -> dict:
    """Auto-adjust system parameters based on meta-insights.

    Currently tunes:
    - Heuristic pruning threshold (PRUNE_THRESHOLD in heuristics.py)
    - Strategy escalation sensitivity

    Returns dict of adjustments made.
    """
    adjustments = {}

    for insight in insights:
        if insight.category == "heuristic" and insight.severity == "warning":
            # If heuristics are dying too fast, relax pruning
            if insight.metric_name == "heuristic_survival_rate" and insight.current_value < 0.5:
                # Store adjusted threshold
                new_threshold = 0.15  # relaxed from 0.2
                _store_parameter("heuristic_prune_threshold", new_threshold)
                adjustments["heuristic_prune_threshold"] = {
                    "old": 0.2, "new": new_threshold, "reason": insight.message,
                }

        elif insight.category == "strategy" and insight.severity == "regression":
            # If a strategy is failing, lower its escalation bar
            strategy = insight.metric_name.replace("strategy_", "").replace("_success", "")
            _store_parameter(f"strategy_{strategy}_escalate_sensitivity", 0.55)
            adjustments[f"strategy_{strategy}_escalate_sensitivity"] = {
                "old": 0.50, "new": 0.55, "reason": insight.message,
            }

    return adjustments


def run_meta_evolution() -> dict:
    """Run full meta-evolution pipeline. Called nightly.

    1. Compute current + previous period metrics
    2. Compare and generate insights
    3. Auto-tune parameters if regressions detected
    4. Store insights as brain memories for future reference

    Returns summary dict.
    """
    insights = compare_periods()

    # Auto-tune based on insights
    adjustments = {}
    regression_count = sum(1 for i in insights if i.severity in ("regression", "warning"))
    if regression_count > 0:
        adjustments = auto_tune_parameters(insights)

    # Store insights as brain memories
    stored_count = 0
    for insight in insights:
        if insight.severity in ("regression", "warning"):
            try:
                content = (
                    f"[meta-evolution/{insight.category}] {insight.message}"
                    + (f" Suggested: {insight.suggested_action}" if insight.suggested_action else "")
                )
                with open_unit_of_work(UnitOfWork) as uow:
                    uow.session.execute(text("""
                        INSERT INTO memories (content, memory_type, salience, source, tags, decay_eligible)
                        VALUES (:content, 'insight', :salience, 'meta_evolution', :tags, TRUE)
                    """), {
                        "content": content[:500],
                        "salience": 8.0 if insight.severity == "regression" else 6.0,
                        "tags": ["meta_evolution", insight.category],
                    })
                stored_count += 1
            except Exception as e:
                logger.debug(f"Failed to store meta-insight: {e}")

    # Compute current metrics for the summary
    current_metrics = compute_evolution_metrics()

    stats = {
        "insights_total": len(insights),
        "regressions": regression_count,
        "improvements": sum(1 for i in insights if i.severity == "info"),
        "adjustments": adjustments,
        "insights_stored": stored_count,
        "metrics": {
            "prediction_accuracy": current_metrics.prediction_accuracy,
            "heuristic_survival": current_metrics.heuristic_survival_rate,
            "heuristic_avg_confidence": current_metrics.heuristic_avg_confidence,
            "strategy_success": current_metrics.strategy_success_rates,
            "runs": current_metrics.total_runs,
            "active_heuristics": current_metrics.total_heuristics_active,
            "active_skills": current_metrics.total_skills_active,
        },
    }

    logger.info(f"Meta-evolution: {stats['insights_total']} insights, "
                f"{stats['regressions']} regressions, {len(adjustments)} adjustments")
    return stats


# ── Helpers ──────────────────────────────────────────────────

def _store_parameter(name: str, value: float) -> None:
    """Store an auto-tuned parameter value.

    Uses the daily_metrics reflection_notes field for now.
    In the future, could use a dedicated parameters table.
    """
    try:
        with open_unit_of_work(UnitOfWork) as uow:
            uow.session.execute(text("""
                INSERT INTO memories (content, memory_type, salience, source, tags, decay_eligible)
                VALUES (:content, 'decision', 7.0, 'auto_tune', :tags, FALSE)
            """), {
                "content": f"[auto-tune] {name} = {value}",
                "tags": ["auto_tune", "parameter", name],
            })
    except Exception as e:
        logger.debug(f"Failed to store parameter: {e}")


def get_tuned_parameter(name: str, default: float) -> float:
    """Retrieve the most recent auto-tuned value for a parameter.

    Falls back to default if no tuned value exists.
    """
    try:
        with open_unit_of_work(UnitOfWork) as uow:
            result = uow.session.execute(text("""
                SELECT content FROM memories
                WHERE source = 'auto_tune' AND tags @> ARRAY[:name]
                  AND NOT archived
                ORDER BY created_at DESC LIMIT 1
            """), {"name": name})
            row = result.mappings().first()
            if row:
                # Parse "[auto-tune] name = value"
                parts = row["content"].split("=")
                if len(parts) == 2:
                    return float(parts[1].strip())
    except Exception:
        pass
    return default

"""Illo Brain — Budget Guardian.

Philosophy: Token efficiency comes from ARCHITECTURE (fresh sessions,
cognitive frames, strategy routing), NOT from restricting models.

The budget guardian is an EMERGENCY BRAKE, not a gatekeeper:
- Never downgrades models — strategy selection picks the right model
- Never blocks normal usage
- Only triggers on catastrophic runaway (e.g., infinite loop, broken agent)
- Logs warnings for visibility, so we learn and improve the architecture

The circuit breaker threshold is deliberately very high — it should
basically never fire during normal use. If it does, something is broken
and we want to stop the bleeding, not silently degrade quality.
"""

import logging
import os
import re
from datetime import datetime, timezone, timedelta

from brain.platform.db.repositories.unit_of_work import UnitOfWork, open_unit_of_work
from brain.systems.runs.token_usage import summarize_token_totals

logger = logging.getLogger("cortex.budget")

# ---------------------------------------------------------------------------
# Circuit breaker — emergency stop for catastrophic runaway only.
# These are intentionally HIGH. Normal Opus usage should never hit them.
# If they fire, something is architecturally broken.
# ---------------------------------------------------------------------------
CIRCUIT_BREAKER_PER_IDEA_HOUR = int(os.environ.get("BUDGET_CIRCUIT_BREAKER_IDEA_HOUR", "1000000"))  # 1M tokens
CIRCUIT_BREAKER_PER_DAY = int(os.environ.get("BUDGET_CIRCUIT_BREAKER_DAY", "10000000"))             # 10M tokens

# ---------------------------------------------------------------------------
# Warning thresholds — log for visibility, never restrict.
# ---------------------------------------------------------------------------
WARN_PER_IDEA_HOUR = int(os.environ.get("BUDGET_WARN_IDEA_HOUR", "300000"))   # 300K tokens
WARN_PER_DAY = int(os.environ.get("BUDGET_WARN_DAY", "3000000"))              # 3M tokens

# Tool result size limits (chars) — prevents accidentally dumping huge files into context
MAX_TOOL_RESULT_CHARS = int(os.environ.get("BUDGET_MAX_TOOL_RESULT_CHARS", "15000"))

# Context limits for run
MAX_THREAD_SUMMARY_CHARS = int(os.environ.get("BUDGET_MAX_THREAD_SUMMARY_CHARS", "2000"))
MAX_LAST_MESSAGES = int(os.environ.get("BUDGET_MAX_LAST_MESSAGES", "5"))
REPAIR_GRACE_MULTIPLIER = float(os.environ.get("BUDGET_REPAIR_GRACE_MULTIPLIER", "1.75"))
REPAIR_MAX_ESTIMATED_INPUT = int(os.environ.get("BUDGET_REPAIR_MAX_ESTIMATED_INPUT", "120000"))
BUDGET_STATUSES = ("completed", "running")

_REPAIR_TASK_RE = re.compile(
    r"(?i)\b("
    r"brain|memory|recall|context|migration|schema|db|database|pgvector|cursor|"
    r"transaction|graph|guardrail|circuit breaker|budget|diagnos\w*|investigat\w*|"
    r"debug\w*|repair|hotfix|root cause|backend"
    r")\b"
)


class BudgetDecision:
    """Result of a budget check."""
    def __init__(self, allowed: bool, reason: str = "",
                 warning: str | None = None, closure_mode: bool = False):
        self.allowed = allowed
        self.reason = reason
        self.warning = warning
        self.closure_mode = closure_mode
        # Kept for backward compatibility but never set
        self.downgrade_model = None
        self.downgrade_thinking = None

    def __repr__(self):
        return f"BudgetDecision(allowed={self.allowed}, reason={self.reason!r})"


def _looks_like_repair_task(task_description: str | None) -> bool:
    """Return True for bounded repair/diagnostic tasks that deserve grace."""
    if not task_description:
        return False
    return bool(_REPAIR_TASK_RE.search(task_description))


def check_budget(idea_id: str, estimated_input_tokens: int,
                 model: str | None = None,
                 task_description: str | None = None) -> BudgetDecision:
    """Check if a run should proceed.

    Normal usage: always allowed, maybe logs a warning.
    Circuit breaker: only fires on catastrophic runaway (1M/idea/hour or 10M/day).

    The model parameter is accepted for API compatibility but never
    used for downgrade decisions — model choice is not the budget's job.
    """
    now = datetime.now(timezone.utc)

    try:
        with open_unit_of_work(UnitOfWork) as uow:
            # 1. Per-idea hourly usage
            hour_ago = now - timedelta(hours=1)
            idea_hour = summarize_token_totals(
                uow.session,
                since=hour_ago,
                thread_id=idea_id,
                statuses=BUDGET_STATUSES,
            )
            idea_hour_tokens = int(idea_hour["tokens_total"] or 0)

            # Warning: log for visibility
            if idea_hour_tokens > WARN_PER_IDEA_HOUR:
                logger.warning(
                    f"Budget warning: idea {idea_id[:8]}… used {idea_hour_tokens:,} tokens "
                    f"in last hour (warn threshold: {WARN_PER_IDEA_HOUR:,})"
                )

            repair_like = _looks_like_repair_task(task_description)
            repair_grace_limit = int(CIRCUIT_BREAKER_PER_IDEA_HOUR * REPAIR_GRACE_MULTIPLIER)

            # Circuit breaker: something is catastrophically wrong
            if idea_hour_tokens > CIRCUIT_BREAKER_PER_IDEA_HOUR:
                if repair_like and idea_hour_tokens <= repair_grace_limit and estimated_input_tokens <= REPAIR_MAX_ESTIMATED_INPUT:
                    warning = (
                        f"Closure mode: idea {idea_id[:8]}… is over the normal breaker "
                        f"({idea_hour_tokens:,}/{CIRCUIT_BREAKER_PER_IDEA_HOUR:,}) but task "
                        f"looks like bounded repair work. Allowing one more run."
                    )
                    logger.warning(warning)
                    return BudgetDecision(
                        allowed=True,
                        warning=warning,
                        closure_mode=True,
                    )
                logger.error(
                    f"CIRCUIT BREAKER: idea {idea_id[:8]}… used {idea_hour_tokens:,} tokens "
                    f"in last hour. Stopping to prevent runaway."
                )
                return BudgetDecision(
                    allowed=False,
                    reason=f"Circuit breaker: {idea_hour_tokens:,} tokens on this idea in 1 hour "
                           f"(limit: {CIRCUIT_BREAKER_PER_IDEA_HOUR:,}). Something may be looping.",
                )

            # 2. Daily usage
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            daily = summarize_token_totals(
                uow.session,
                since=today_start,
                statuses=BUDGET_STATUSES,
            )
            daily_tokens = int(daily["tokens_total"] or 0)

            if daily_tokens > WARN_PER_DAY:
                logger.warning(
                    f"Budget warning: {daily_tokens:,} tokens used today "
                    f"(warn threshold: {WARN_PER_DAY:,})"
                )

            if daily_tokens > CIRCUIT_BREAKER_PER_DAY:
                repair_grace_daily = int(CIRCUIT_BREAKER_PER_DAY * REPAIR_GRACE_MULTIPLIER)
                if repair_like and daily_tokens <= repair_grace_daily and estimated_input_tokens <= REPAIR_MAX_ESTIMATED_INPUT:
                    warning = (
                        f"Closure mode: daily usage is over the normal breaker "
                        f"({daily_tokens:,}/{CIRCUIT_BREAKER_PER_DAY:,}) but task "
                        f"looks like bounded repair work. Allowing one more run."
                    )
                    logger.warning(warning)
                    return BudgetDecision(
                        allowed=True,
                        warning=warning,
                        closure_mode=True,
                    )
                logger.error(
                    f"CIRCUIT BREAKER: {daily_tokens:,} tokens used today. "
                    f"Stopping all runs."
                )
                return BudgetDecision(
                    allowed=False,
                    reason=f"Circuit breaker: {daily_tokens:,} tokens today "
                           f"(limit: {CIRCUIT_BREAKER_PER_DAY:,}). Check for runaway agents.",
                )

    except Exception as e:
        logger.error(f"Budget check failed: {e} — allowing run (fail-open)")
        return BudgetDecision(allowed=True, reason=f"Budget check error: {e}")

    # Build warning string for logging (never restricts)
    warning = None
    if idea_hour_tokens > WARN_PER_IDEA_HOUR or daily_tokens > WARN_PER_DAY:
        warning = f"High usage: {idea_hour_tokens:,}/hr, {daily_tokens:,}/day"

    return BudgetDecision(allowed=True, warning=warning)


def estimate_run_tokens(message: str, has_thread_history: bool = False,
                             estimated_workers: int = 2) -> int:
    """Estimate total input tokens for a run including child-worker fanout.

    Uses a simple chars/4 heuristic — good enough for budget decisions.
    Accounts for coordinator + N workers to avoid budget blind spots.
    """
    base_tokens = len(message) // 4

    # System prompt overhead (~3K tokens for coordinator)
    system_overhead = 3000

    # Brain context injection (~2K tokens)
    brain_overhead = 2000

    coordinator_tokens = base_tokens + system_overhead + brain_overhead

    # Each worker: ~5K system prompt + ~3K task + ~2K brain context
    worker_tokens_each = 10_000
    worker_total = estimated_workers * worker_tokens_each

    return coordinator_tokens + worker_total


def get_budget_status() -> dict:
    """Return current budget utilization for the dashboard."""
    now = datetime.now(timezone.utc)
    try:
        with open_unit_of_work(UnitOfWork) as uow:
            # Daily usage
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            daily = summarize_token_totals(
                uow.session,
                since=today_start,
                statuses=BUDGET_STATUSES,
            )

            # Hourly usage
            hour_ago = now - timedelta(hours=1)
            hourly = summarize_token_totals(
                uow.session,
                since=hour_ago,
                statuses=BUDGET_STATUSES,
            )

        return {
            "daily_tokens": daily["tokens_total"],
            "daily_warn": WARN_PER_DAY,
            "daily_circuit_breaker": CIRCUIT_BREAKER_PER_DAY,
            "daily_pct": round(100 * daily["tokens_total"] / CIRCUIT_BREAKER_PER_DAY, 1),
            "daily_runs": daily["runs"],
            "daily_cost": float(daily["estimated_cost"]),
            "hourly_tokens": hourly["tokens_total"],
            "hourly_runs": hourly["runs"],
        }
    except Exception as e:
        logger.error(f"Budget status query failed: {e}")
        return {"error": str(e)}

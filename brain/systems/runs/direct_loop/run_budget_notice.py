"""One-shot model-context notices for cumulative AgentRun token usage."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
import logging
import math
from typing import Any

from brain.kernel import config
from brain.systems.runs.runtime_activity import load_run_activity

logger = logging.getLogger("agent")

SOFT_NOTICE_KEY = "soft"
CEILING_NOTICE_KEY = "ceiling"
_MARKER_PREFIX = "[System run budget notice:"


@dataclass(frozen=True)
class RunBudgetNotice:
    """A newly due notice and its once-per-run identity."""

    key: str
    message: dict[str, str]


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, Sequence):
        return ""
    return "\n".join(
        str(block.get("text") or block.get("content") or "")
        for block in content
        if isinstance(block, Mapping)
    )


def _marker(key: str, run_id: int) -> str:
    return f"{_MARKER_PREFIX} {key}; run_id={run_id}]"


def budget_notices_seen(
    messages: Sequence[Mapping[str, Any]],
    *,
    run_id: int | None,
) -> set[str]:
    """Recover notice state for this run from its persisted transcript."""

    if run_id is None:
        return set()
    resolved_run_id = int(run_id)
    seen: set[str] = set()
    for message in messages:
        text = _message_text(message)
        for key in (SOFT_NOTICE_KEY, CEILING_NOTICE_KEY):
            if _marker(key, resolved_run_id) in text:
                seen.add(key)
    return seen


def _cumulative_run_token_budget() -> int:
    try:
        return max(
            0,
            int(
                getattr(
                    config,
                    "AGENT_RUN_CUMULATIVE_TOKEN_BUDGET",
                    0,
                )
                or 0
            ),
        )
    except (TypeError, ValueError):
        return 0


def _tool_call_phrase(count: int) -> str:
    return f"{count} tool call" if count == 1 else f"{count} tool calls"


def _notice(
    *,
    key: str,
    run_id: int,
    tokens_used: int,
    ceiling_tokens: int,
    tool_call_count: int,
) -> RunBudgetNotice:
    position = (
        f"Run {run_id} has used {tokens_used} of {ceiling_tokens} tokens "
        f"and made {_tool_call_phrase(tool_call_count)}."
    )
    if key == SOFT_NOTICE_KEY:
        instruction = (
            "You are nearing this run's token budget. "
            "Wrap up now and persist durable progress before closing."
        )
    else:
        instruction = (
            "This run has reached its token budget ceiling. Stop new work: "
            "persist what you have and emit the closing output now."
        )
    return RunBudgetNotice(
        key=key,
        message={
            "role": "user",
            "content": f"{_marker(key, run_id)} {position} {instruction}",
        },
    )


async def load_due_budget_notices(
    *,
    run_id: int | None,
    tool_calls_log: Sequence[str],
    sent: Collection[str],
) -> tuple[RunBudgetNotice, ...]:
    """Load ledger-backed usage and return each newly crossed notice once."""

    if run_id is None:
        return ()
    if {SOFT_NOTICE_KEY, CEILING_NOTICE_KEY}.issubset(sent):
        return ()
    ceiling_tokens = _cumulative_run_token_budget()
    if ceiling_tokens <= 0:
        return ()

    resolved_run_id = int(run_id)
    try:
        activity = await load_run_activity(resolved_run_id)
    except Exception:
        logger.warning(
            "Failed to load durable run activity for budget notice",
            extra={"run_id": resolved_run_id},
            exc_info=True,
        )
        return ()

    tokens_used = max(0, int(activity.get("tokens_used") or 0))
    tool_call_count = len(tool_calls_log)
    soft_threshold = math.ceil(
        ceiling_tokens * config.AGENT_RUN_BUDGET_NOTICE_FRACTION
    )
    notices: list[RunBudgetNotice] = []
    if tokens_used >= soft_threshold and SOFT_NOTICE_KEY not in sent:
        notices.append(
            _notice(
                key=SOFT_NOTICE_KEY,
                run_id=resolved_run_id,
                tokens_used=tokens_used,
                ceiling_tokens=ceiling_tokens,
                tool_call_count=tool_call_count,
            )
        )
    if tokens_used >= ceiling_tokens and CEILING_NOTICE_KEY not in sent:
        notices.append(
            _notice(
                key=CEILING_NOTICE_KEY,
                run_id=resolved_run_id,
                tokens_used=tokens_used,
                ceiling_tokens=ceiling_tokens,
                tool_call_count=tool_call_count,
            )
        )
    return tuple(notices)


__all__ = [
    "CEILING_NOTICE_KEY",
    "RunBudgetNotice",
    "SOFT_NOTICE_KEY",
    "budget_notices_seen",
    "load_due_budget_notices",
]

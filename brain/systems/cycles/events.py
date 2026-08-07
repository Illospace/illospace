"""Realtime notifications for Cycle changes."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def publish_cycle_change(
    *,
    action: str,
    org_id: str | None = None,
    user_id: str | None = None,
    cycle_id: int | None = None,
    target_idea_id: str | None = None,
) -> None:
    payload: dict[str, Any] = {"action": action}
    if org_id:
        payload["org_id"] = str(org_id)
    if user_id:
        payload["user_id"] = str(user_id)
    if cycle_id is not None:
        payload["cycle_id"] = int(cycle_id)
    if target_idea_id:
        payload["idea_id"] = str(target_idea_id)
        payload["target_idea_id"] = str(target_idea_id)

    try:
        from brain.platform.events import publish_safe

        publish_safe("cycles_changed", payload)
    except Exception:
        logger.debug("cycle_change_publish_failed", exc_info=True)

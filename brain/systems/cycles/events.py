"""Realtime notifications for Cycle changes."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def publish_cycle_change_safe(
    *,
    action: str,
    org_id: str | None = None,
    user_id: str | None = None,
    cycle_id: int | None = None,
    target_idea_id: str | None = None,
) -> None:
    payload = _cycle_change_payload(
        action=action,
        org_id=org_id,
        user_id=user_id,
        cycle_id=cycle_id,
        target_idea_id=target_idea_id,
    )
    try:
        from brain.platform.events import publish_safe

        publish_safe("cycles_changed", payload)
    except Exception:
        logger.debug("cycle_change_publish_failed", exc_info=True)


def publish_cycle_change_strict(
    *,
    action: str,
    org_id: str | None = None,
    user_id: str | None = None,
    cycle_id: int | None = None,
    target_idea_id: str | None = None,
) -> None:
    from brain.platform.events import publish

    publish(
        "cycles_changed",
        _cycle_change_payload(
            action=action,
            org_id=org_id,
            user_id=user_id,
            cycle_id=cycle_id,
            target_idea_id=target_idea_id,
        ),
    )


def _cycle_change_payload(
    *,
    action: str,
    org_id: str | None,
    user_id: str | None,
    cycle_id: int | None,
    target_idea_id: str | None,
) -> dict[str, Any]:
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
    return payload

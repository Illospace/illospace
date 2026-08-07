"""Realtime notifications for generated workspace apps."""
from __future__ import annotations

import logging
from typing import Any, Mapping

logger = logging.getLogger(__name__)


def publish_workspace_app_change(
    *,
    org_id: str,
    action: str,
    app: Mapping[str, Any] | None = None,
    app_id: str | None = None,
    key: str | None = None,
) -> None:
    """Notify open Cortex clients that the generated app list changed."""
    payload: dict[str, Any] = {
        "org_id": str(org_id),
        "action": action,
    }
    if app:
        payload["app"] = dict(app)
        payload["app_id"] = str(app.get("id") or app_id or "")
        payload["key"] = str(app.get("key") or key or "")
    else:
        if app_id:
            payload["app_id"] = str(app_id)
        if key:
            payload["key"] = str(key)

    try:
        from brain.platform.events import publish_safe

        publish_safe("workspace_apps_changed", payload)
    except Exception:
        logger.debug("workspace_app_change_publish_failed", exc_info=True)


def publish_workspace_app_collaboration_event(
    *,
    org_id: str,
    app_id: str,
    state: Mapping[str, Any] | None = None,
    events: list[Mapping[str, Any]] | None = None,
    duplicate: bool = False,
) -> None:
    """Notify open Cortex clients that a collaborative app event/state changed."""
    payload: dict[str, Any] = {
        "org_id": str(org_id),
        "app_id": str(app_id),
        "duplicate": bool(duplicate),
    }
    if state:
        payload["state"] = dict(state)
        payload["state_key"] = str(state.get("key") or "")
        payload["state_version"] = int(state.get("version") or 0)
    if events:
        payload["events"] = [dict(event) for event in events]

    try:
        from brain.platform.events import publish_safe

        publish_safe("workspace_app_collaboration_changed", payload)
    except Exception:
        logger.debug("workspace_app_collaboration_publish_failed", exc_info=True)

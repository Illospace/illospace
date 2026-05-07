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
        from brain.systems.cortex.events import publish_safe

        publish_safe("workspace_apps_changed", payload)
    except Exception:
        logger.debug("workspace_app_change_publish_failed", exc_info=True)

"""Canonical share links for generated workspace apps."""
from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote

from brain.systems.cortex.thread_links import LEGACY_CORTEX_ROUTE, thread_route_for_id, thread_url_for_route

WORKSPACE_APP_QUERY_PARAM = "app"


def thread_id_from_app_metadata(metadata: Mapping[str, Any] | None) -> str | None:
    data = dict(metadata or {})
    thread_artifact = data.get("thread_artifact")
    candidates = [
        data.get("thread_id"),
        thread_artifact.get("thread_id") if isinstance(thread_artifact, Mapping) else None,
    ]
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text
    return None


def workspace_app_route_for_id(app_id: Any, *, thread_id: Any | None = None) -> str:
    encoded_app_id = quote(str(app_id), safe="")
    base_route = thread_route_for_id(thread_id) if thread_id else LEGACY_CORTEX_ROUTE
    separator = "&" if "?" in base_route else "?"
    return f"{base_route}{separator}{WORKSPACE_APP_QUERY_PARAM}={encoded_app_id}"


def workspace_app_url_for_id(app_id: Any, *, thread_id: Any | None = None) -> str:
    return thread_url_for_route(workspace_app_route_for_id(app_id, thread_id=thread_id))


def workspace_app_link_payload(app_id: Any, *, metadata: Mapping[str, Any] | None = None) -> dict[str, str]:
    thread_id = thread_id_from_app_metadata(metadata)
    route = workspace_app_route_for_id(app_id, thread_id=thread_id)
    url = thread_url_for_route(route)
    payload = {
        "app_route": route,
        "app_url": url,
        "share_url": url,
        "url": url,
    }
    if thread_id:
        payload["thread_id"] = thread_id
    return payload


__all__ = [
    "WORKSPACE_APP_QUERY_PARAM",
    "thread_id_from_app_metadata",
    "workspace_app_link_payload",
    "workspace_app_route_for_id",
    "workspace_app_url_for_id",
]

"""
Cortex Reply Service — Post Illo's responses back to cortex idea threads.

Usage as module:
    from brain.systems.cortex.reply import reply_to_cortex
    result = reply_to_cortex(idea_id="<uuid>", content="my response")

Usage as CLI:
    python3 -m services.cortex_reply --idea-id <uuid> --content "my response"
"""
from __future__ import annotations
import argparse
import json
import os
import sys

import httpx

from brain.platform.async_io import http_post

DASHBOARD_URL = os.environ.get("ILLO_DASHBOARD_URL", "http://127.0.0.1:8000")


def _auth_headers() -> dict:
    """Build auth headers from the Illo internal service token."""
    token = os.environ.get("ILLO_API_TOKEN", "")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def reply_to_cortex(idea_id: str, content: str, attachments: list | None = None, metadata: dict | None = None) -> dict:
    """Post Illo's response to a cortex idea thread.

    Args:
        idea_id: UUID of the idea to reply to.
        content: Text content of the reply.
        attachments: Optional list of attachment dicts.

    Returns:
        The created thread message dict from the API.

    Raises:
        RuntimeError: If the API call fails.
    """
    url = f"{DASHBOARD_URL}/api/cortex/ideas/{idea_id}/thread"
    payload = {
        "content": content,
        "role": "illo",
        "attachments": attachments or [],
    }
    if metadata:
        payload["metadata"] = metadata
    headers = _auth_headers()
    try:
        resp = http_post(url, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        raise RuntimeError(f"Failed to post reply to cortex thread {idea_id}: {e}") from e


def main():
    global DASHBOARD_URL
    parser = argparse.ArgumentParser(description="Post Illo's response to a Cortex idea thread")
    parser.add_argument("--idea-id", required=True, help="UUID of the idea")
    parser.add_argument("--content", default=None, help="Response text")
    parser.add_argument("--dashboard-url", default=None, help=f"Dashboard URL (default: {DASHBOARD_URL})")
    parser.add_argument("--restart-dashboard", action="store_true",
                        help="Restart illo-dashboard service after posting reply")
    args = parser.parse_args()

    if args.dashboard_url:
        DASHBOARD_URL = args.dashboard_url

    metadata = None  # reserved for future use via CLI args

    try:
        if not args.content:
            print("Error: --content is required", file=sys.stderr)
            sys.exit(1)
        result = reply_to_cortex(args.idea_id, args.content, metadata=metadata)
        print(json.dumps(result, indent=2))
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.restart_dashboard:
        from brain.systems.cortex.restarter import restart_dashboard
        restart_dashboard()


if __name__ == "__main__":
    main()

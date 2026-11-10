"""Small Slack helpers that several areas share.

`ctx.slack_client` is a `slack_sdk` `AsyncWebClient`, or None when Slack is not connected.
Areas call its own methods (`chat_postMessage`, `chat_update`, `conversations_open`, ...).
This file adds only what more than one area needs.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from illo.ledger import people

log = logging.getLogger(__name__)


@dataclass
class ActionRouter:
    """Route a card button click to the area that owns its `action_id` prefix.

    The Slack channel calls `dispatch` for each click of a known person.
    A handler is `async handler(ctx, person, action)`. `action` has `action_id`,
    `value`, `channel`, `message_ts`, `thread_ts`, `trigger_id`, and `user_id`.
    The handler must check that this person may do this action.
    """

    handlers: dict[str, Callable[[Any, dict, dict], Awaitable[None]]] = field(default_factory=dict)

    def register(self, prefix: str, handler) -> None:
        if prefix in self.handlers:
            raise ValueError(f"Duplicate action prefix: {prefix}")
        self.handlers[prefix] = handler

    async def dispatch(self, ctx, person: dict | None, action: dict) -> bool:
        """Return True when a handler took the action. An unknown person does nothing."""
        if not person:
            return False
        action_id = action.get("action_id", "")
        for prefix in sorted(self.handlers, key=len, reverse=True):
            if action_id.startswith(prefix):
                await self.handlers[prefix](ctx, person, action)
                return True
        return False


async def dm_person(ctx, person_key: str, text: str, blocks: list | None = None) -> dict | None:
    """Send a DM to a team member. Return {channel, ts}, or None when it cannot be sent."""
    if not ctx.slack_client:
        return None
    async with ctx.pool.connection() as conn:
        person = await people.get(conn, person_key)
    if not person or not person.get("slack_id") or not person.get("active", True):
        return None
    opened = await ctx.slack_client.conversations_open(users=person["slack_id"])
    channel = opened["channel"]["id"]
    sent = await ctx.slack_client.chat_postMessage(channel=channel, text=text, blocks=blocks)
    return {"channel": channel, "ts": sent["ts"]}

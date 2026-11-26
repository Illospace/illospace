"""Auto Mode guard for internal team Slack messages requested by agents.

Layer 1 (deterministic code): enforces workspace policy, secret/credential blocking,
hidden-Unicode & mass-ping escalation, length cap, rate limits, and duplicate suppression.

Layer 2 (quarantined zero-tool review): runs an isolated brain call with profile
`comms_review` (zero tools granted by code) to catch prompt injection, social engineering,
or risky/sensitive content. Fails closed to `draft` on any error or timeout.
"""

import asyncio
import hashlib
import json
import logging
import re
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from typing import Literal

from illo.brain.client import RunRequest
from illo.brain.prompts import PROFILE_TEXTS
from illo.brain.routes import resolve_route, select_payer
from illo.db import one
from illo.gateway.principal import Authority
from illo.ledger import kv, runs
from illo.tools._format import data_text
from illo.tools.checks import Decision
from illo.tools.registry import ToolContext

log = logging.getLogger(__name__)

REVIEW_MARKER = "[illo:comms-review]"
MAX_AUTO_CHARS = 3000
MAX_PER_RUN = 1
MAX_PER_HOUR = 6
DEDUP_MINUTES = 10
REVIEW_TIMEOUT_S = 20.0
REVIEW_SYSTEM_PROMPT = PROFILE_TEXTS["comms_review"]

SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bxapp-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{24,}\b"),
    re.compile(r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^/\s:@]+:[^/\s@]+@"),
)

MASS_PING_RE = re.compile(
    r"<!(?:channel|here|everyone)(?:\|[^>]*)?>|(?<!\w)@(?:channel|here|everyone)\b",
    re.IGNORECASE,
)

HIDDEN_FORMAT_CHARS = frozenset({
    "\u200b", "\u200c", "\u200e", "\u200f", "\ufeff",
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
    "\u2066", "\u2067", "\u2068", "\u2069",
})


def contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def has_hidden_format(text: str) -> bool:
    return any(ch in HIDDEN_FORMAT_CHARS or 0xE0000 <= ord(ch) <= 0xE007F for ch in text)


def has_mass_ping(text: str) -> bool:
    return bool(MASS_PING_RE.search(text))


def _dedup_key(principal: str, tool_name: str, target: str, text: str) -> str:
    payload = json.dumps(
        [principal, tool_name, target, " ".join(text.split())],
        separators=(",", ":"),
    ).encode()
    return "comms:dedup:" + hashlib.sha256(payload).hexdigest()[:32]


async def record_sent(ctx: ToolContext, *, tool_name: str, target: str, text: str) -> None:
    """Record a short-lived hash of a sent agent message to stop accidental repeat loops."""
    if ctx.run.get("profile") != "agent_request":
        return
    key = _dedup_key(ctx.run["principal"], tool_name, target, text)
    expires = (datetime.now(UTC) + timedelta(minutes=DEDUP_MINUTES)).isoformat()
    await kv.set(ctx.conn, key, {"expires_at": expires})


def parse_verdict(raw: str) -> tuple[Literal["allow", "draft", "deny"], str] | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(text)
    except ValueError:
        match = re.search(r"\{[^{}]*\}", text)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    verdict = data.get("verdict")
    if verdict not in {"allow", "draft", "deny"}:
        return None
    reason = str(data.get("reason") or "").strip()
    reason = " ".join(reason.split())[:140]
    return verdict, reason


async def _deterministic_checks(
    ctx: ToolContext,
    *,
    tool_name: str,
    target: str,
    args: dict,
    text: str,
) -> Decision | None:
    if contains_secret(text):
        return Decision("deny", "Message appears to contain a secret or credential")
    if has_hidden_format(text):
        return Decision("draft", "Hidden formatting characters need your approval")
    if has_mass_ping(text):
        return Decision("draft", "Channel-wide pings need your approval")
    if len(text) > MAX_AUTO_CHARS:
        return Decision("draft", "Long agent messages need your approval")

    run_count = await one(
        ctx.conn,
        """SELECT count(*) AS n FROM audit
        WHERE run_id=%s AND tool IN ('slack_post_channel', 'slack_send_dm') AND decision='allow'""",
        (ctx.run["id"],),
    )
    if (run_count or {}).get("n", 0) >= MAX_PER_RUN:
        return Decision("draft", "Multiple Slack messages in one agent request need your approval")

    hour_count = await one(
        ctx.conn,
        """SELECT count(*) AS n FROM audit
        WHERE principal=%s AND tool IN ('slack_post_channel', 'slack_send_dm')
          AND decision='allow' AND created_at > now() - interval '1 hour'""",
        (ctx.run["principal"],),
    )
    if (hour_count or {}).get("n", 0) >= MAX_PER_HOUR:
        return Decision("draft", "Hourly auto-send limit reached; approval needed")

    recent = await kv.get(ctx.conn, _dedup_key(ctx.run["principal"], tool_name, target, text))
    if recent and datetime.fromisoformat(recent["expires_at"]) > datetime.now(UTC):
        return Decision("deny", "Duplicate Slack message was just sent")
    return None


async def _request_context(ctx: ToolContext) -> str:
    event_id = ctx.run.get("event_id")
    if not event_id:
        return ""
    row = await one(ctx.conn, "SELECT payload FROM events WHERE id=%s", (event_id,))
    if not row or not isinstance(row.get("payload"), dict):
        return ""
    return str(row["payload"].get("text") or "")[:1500]


async def _review_with_brain(
    ctx: ToolContext,
    *,
    kind: Literal["channel", "dm"],
    target_label: str,
    text: str,
) -> Decision:
    app = ctx.app
    brain = getattr(app, "brain_client", None)
    if brain is None:
        return Decision("draft", "Auto-review unavailable; approval needed")

    route = resolve_route(app.workspace, "agent_request")
    authority = Authority(
        ctx.run["principal"],
        "agent_request",
        ctx.run.get("person_id"),
        tainted=False,
    )
    try:
        payer = await select_payer(app, route, authority, notify=False)
    except Exception:
        log.warning("Auto-review payer selection failed for run %s", ctx.run["id"])
        return Decision("draft", "Auto-review unavailable; approval needed")
    if payer is None:
        return Decision("draft", "Auto-review unavailable; approval needed")

    original = await _request_context(ctx)
    parts = [
        f"Action: {'post to team Slack channel' if kind == 'channel' else 'send Slack DM to teammate'}",
        f"Destination: {target_label}",
        f"Requester principal: {ctx.run['principal']}",
    ]
    if original:
        parts.append("Original agent submission:\n" + data_text("agent-request", original))
    parts.append("Proposed Slack message:\n" + data_text("proposed-message", text))
    user_message = "\n\n".join(parts)

    review_run_id = None
    try:
        # Use a separate connection so the review run token is committed before brain connects.
        async with app.pool.connection() as review_conn:
            review_run = await runs.create(
                review_conn,
                principal=ctx.run["principal"],
                person_id=ctx.run.get("person_id"),
                profile="comms_review",
                tainted=False,
                provider=payer.provider,
                model=route.model,
                tool_limit=1,
                event_id=ctx.run.get("event_id"),
            )
            review_run_id = review_run["id"]
            token = await runs.mint_token(review_conn, review_run_id, int(REVIEW_TIMEOUT_S) + 10)

        request = RunRequest(
            run_id=review_run_id,
            system_prompt=REVIEW_SYSTEM_PROMPT,
            user_message=user_message,
            mcp_url=f"{app.config.public_url.rstrip('/')}/brain/mcp",
            mcp_token=token,
            model=route.model,
            effort=route.effort,
            max_tool_calls=1,
            timeout_s=REVIEW_TIMEOUT_S,
            max_output_tokens=200,
            **payer.request(),
        )

        final_text = ""
        async with asyncio.timeout(REVIEW_TIMEOUT_S + 2):
            async with aclosing(brain.run(request)) as stream:
                async for item in stream:
                    if item.type == "final":
                        final_text = item.text or ""
                    elif item.type == "error":
                        return Decision("draft", "Auto-review failed; approval needed")
    except Exception:
        log.warning("Auto-review failed for run %s", ctx.run["id"], exc_info=True)
        return Decision("draft", "Auto-review unavailable; approval needed")
    finally:
        if review_run_id is not None:
            try:
                async with app.pool.connection() as review_conn:
                    await runs.finish(review_conn, review_run_id)
            except Exception:
                pass

    parsed = parse_verdict(final_text)
    if parsed is None:
        return Decision("draft", "Auto-review could not confirm safety; approval needed")
    verdict, reason = parsed
    if verdict == "allow":
        return Decision("allow", reason or "Approved by Auto Mode review")
    if verdict == "deny":
        return Decision("deny", reason or "Blocked by Auto Mode safety review")
    return Decision("draft", reason or "Flagged for your approval by Auto Mode review")


async def evaluate(
    ctx: ToolContext,
    *,
    kind: Literal["channel", "dm"],
    tool_name: str,
    target: str,
    target_label: str,
    args: dict,
    text: str,
    policy: Literal["auto", "draft", "off"],
) -> Decision:
    """Apply workspace policy, deterministic safety checks, and quarantined Auto Mode review."""
    if policy == "off":
        return Decision("deny", "Agent Slack messages are disabled for this destination")
    if policy == "draft":
        return Decision("draft", "Agent Slack messages require your approval for this destination")
    hard = await _deterministic_checks(
        ctx, tool_name=tool_name, target=target, args=args, text=text
    )
    if hard is not None:
        return hard
    return await _review_with_brain(ctx, kind=kind, target_label=target_label, text=text)

"""Send drafts only after the requester approves the stored card in a DM."""

import logging
import re
from datetime import UTC, datetime, timedelta

from psycopg.types.json import Jsonb

from illo.asks import cards
from illo.asks.service import post_dm
from illo.db import all_rows, one
from illo.ledger import ask_workflow as ledger
from illo.ledger import audit, drafts, people, runs, sensors
from illo.tools.registry import ToolContext

log = logging.getLogger(__name__)

ASSIGN_CMD_RE = re.compile(
    r"^(?:(?:please|pls)\s+|(?:can|could|would)\s+you\s+(?:please\s+)?)?"
    r"assign"
    r"(?:\s+(?:it|this|the\s+(?:issue|ticket|bug|task)))?"
    r"(?:\s+to\s+(?P<target><@[A-Z0-9_]+>|[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+){0,2}))?$",
    re.IGNORECASE,
)


def parse_assign_command(text: str) -> tuple[bool, str | None]:
    """Match short explicit channel commands like '@Illo can you assign it?' without an LLM."""
    cleaned = re.sub(r"^\s*(?:<@[A-Z0-9_]+>\s*[,:-]?\s*)+", "", text or "")
    cleaned = " ".join(cleaned.split()).rstrip("?!. ")
    cleaned = re.sub(
        r"(?<!\bto)(?:,?\s+<@[A-Z0-9_]+>)+$", "", cleaned, flags=re.IGNORECASE
    ).rstrip("?!., ")
    cleaned = re.sub(r"\s+(?:please|pls)$", "", cleaned, flags=re.IGNORECASE).rstrip(", ")
    match = ASSIGN_CMD_RE.fullmatch(cleaned)
    if not match:
        return False, None
    target = match.group("target")
    if target and re.search(r"\b(?:or|and)\b", target, re.IGNORECASE):
        return False, None
    return True, target.strip() if target else None


async def _sync_channel_card(ctx, conn, draft):
    notice = draft["content"].get("outside_ticket") or {}
    card = notice.get("channel_card")
    if not card or not ctx.slack_client:
        return
    blocks = cards.outside_ticket_channel_blocks(draft, card["text"])
    new_digest = cards.digest(blocks)
    if card.get("digest") == new_digest:
        return
    try:
        await ctx.slack_client.chat_update(
            channel=card["channel"],
            ts=card["ts"],
            text=card["text"],
            blocks=blocks,
        )
    except Exception:
        log.warning("Could not update channel card for draft %s", draft["id"])
        return
    content = dict(draft["content"])
    updated_notice = dict(notice)
    updated_notice["channel_card"] = {**card, "digest": new_digest}
    content["outside_ticket"] = updated_notice
    draft["content"] = content
    await conn.execute("UPDATE drafts SET content=%s WHERE id=%s", (Jsonb(content), draft["id"]))


async def sync_card(ctx, conn, draft):
    if not ctx.slack_client:
        await sensors.error(
            conn, "drafts:slack", "Slack is not connected. Draft cards wait in the ledger."
        )
        return
    from illo.sources.github import WRITE_TOOLS, prepare_draft_card

    if draft["kind"] in WRITE_TOOLS:
        await prepare_draft_card(ctx, conn, draft)
    blocks = cards.draft_card(draft)
    digest = cards.digest(blocks)
    sent = {"channel": draft["slack_channel"], "ts": draft["slack_ts"]}
    if draft["slack_ts"]:
        if draft["card_digest"] != digest:
            await ctx.slack_client.chat_update(
                channel=draft["slack_channel"],
                ts=draft["slack_ts"],
                text=cards.draft_fallback(draft),
                blocks=blocks,
            )
    elif draft["state"] == "pending":
        person = await ledger.draft_owner(conn, draft)
        sent = await post_dm(
            ctx, person, cards.draft_fallback(draft), blocks, "draft", draft["id"]
        )
    else:
        await _sync_channel_card(ctx, conn, draft)
        return
    if sent:
        await ledger.save_draft_card(conn, draft["id"], sent, digest)
        await sensors.ok(conn, "drafts:slack")
    await _sync_channel_card(ctx, conn, draft)
    return sent


async def tick(ctx):
    async with ctx.pool.connection() as conn:
        await ledger.expire(conn)
        ids = await ledger.drafts_to_check(conn)
    for row in ids:
        try:
            async with ctx.pool.connection() as conn:
                draft = await ledger.lock_draft(conn, row["id"])
                await sync_card(ctx, conn, draft)
        except Exception:
            log.error("Could not refresh draft %s", row["id"])
            async with ctx.pool.connection() as conn:
                await sensors.error(
                    conn, "drafts:slack", "A draft card could not be sent or updated."
                )


def can_perform(ctx, draft):
    return (
        draft["kind"] in ctx.draft_performers
        or draft["kind"] == "slack_post"
        or draft["kind"] in ctx.registry.tools
    )


async def perform(ctx, draft):
    """Use the shared performer hook, or the exact registered tool handler."""
    fn = ctx.draft_performers.get(draft["kind"])
    if fn:
        await fn(ctx, draft)
        return
    if draft["kind"] == "slack_post":
        if not ctx.slack_client or not draft["destination"].startswith("channel:"):
            raise ValueError("Slack is not connected or the destination is invalid")
        content = draft["content"]
        await ctx.slack_client.chat_postMessage(
            channel=draft["destination"][8:], text=content["text"], blocks=content.get("blocks")
        )
        return
    # A registry draft stores the validated arguments. Approval authorizes this one write.
    tool = ctx.registry.tools[draft["kind"]]
    args = tool.arguments.model_validate(draft["content"]["arguments"]).model_dump()
    if (
        draft["content"].get("tool") != tool.name
        or tool.destination(args) != draft["destination"]
        or tool.risk not in {"write_internal", "send_outside"}
    ):
        raise ValueError("The draft no longer matches the registered action")
    async with ctx.pool.connection() as conn:
        run = await runs.get(conn, draft["run_id"])
        if run["person_id"] != draft["approver_person_id"]:
            raise ValueError("The requester does not own this draft")
        await tool.handler(ToolContext(conn, run, ctx), args)


async def handle(ctx, person, action):
    parts = action.get("action_id", "").split(":")
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in {"approve", "personal", "reject"}:
        return
    draft_id = int(parts[1])
    approved = False
    async with ctx.pool.connection() as conn:
        draft = await ledger.lock_draft(conn, draft_id)
        if (
            not draft
            or not person["active"]
            or draft["approver_person_id"] != person["id"]
            or not draft["slack_channel"]
            or not draft["slack_channel"].startswith("D")
            or action.get("channel") != draft["slack_channel"]
            or action.get("message_ts") != draft["slack_ts"]
        ):
            return
        if draft["kind"] in {"connect_propose_mcp", "connect_allow_tools", "connect_remove", "connect_data_policy"}:
            from illo.connectors.chat import active_person

            try:
                await active_person(conn, person["id"], owner=True)
            except ValueError:
                return
        if parts[2] == "personal" and not (
            draft["kind"] == "connect_propose_mcp"
            and draft["content"]["arguments"]["auth"] == "oauth"
        ):
            return
        run = await runs.get(conn, draft["run_id"])
        if not run or run["person_id"] != person["id"]:
            return
        if draft["state"] != "pending":
            await sync_card(ctx, conn, draft)
            return
        if draft["created_at"] + timedelta(hours=24) <= datetime.now(UTC):
            await ledger.expire_draft(conn, draft_id)
        elif parts[2] in {"approve", "personal"} and cards.draft_preview(draft) is None:
            return
        elif parts[2] in {"approve", "personal"} and not can_perform(ctx, draft):
            if ctx.slack_client:
                await ctx.slack_client.chat_postMessage(
                    channel=draft["slack_channel"],
                    thread_ts=draft["slack_ts"],
                    text="This action is not connected. The draft stays unsent.",
                )
            return
        else:
            approved = parts[2] in {"approve", "personal"}
            if approved and draft["kind"] == "connect_propose_mcp":
                content = draft["content"]
                content["arguments"]["scope"] = "personal" if parts[2] == "personal" else "team"
                await conn.execute("UPDATE drafts SET content=%s WHERE id=%s", (Jsonb(content), draft_id))
            decided = await drafts.decide(
                conn, draft_id, person_id=person["id"], approve=approved, in_dm=True
            )
            approved = approved and decided
            draft = await ledger.lock_draft(conn, draft_id)
    # Commit the decision before the external action. A second click or a restart cannot send again.
    if approved:
        try:
            if draft["kind"] == "connect_propose_mcp" and draft["content"]["arguments"]["auth"] == "bearer":
                from illo.connectors.chat import open_mcp_form

                await open_mcp_form(ctx, person, draft, action.get("trigger_id", ""))
                return
            await perform(ctx, draft)
        except Exception:
            async with ctx.pool.connection() as conn:
                await ledger.draft_failed(conn, draft_id)
                await sensors.error(
                    conn,
                    f"draft:{draft_id}",
                    "Draft delivery could not be confirmed. Check the destination.",
                )
        else:
            async with ctx.pool.connection() as conn:
                await drafts.mark_sent(conn, draft_id)
    async with ctx.pool.connection() as conn:
        draft = await ledger.lock_draft(conn, draft_id)
        await sync_card(ctx, conn, draft)


async def handle_outside_assign(ctx, person, action):
    """Approve or reject a code-created outside-ticket assignment right from its alert reply."""
    parts = action.get("action_id", "").split(":")
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in {"approve", "reject"}:
        return
    draft_id = int(parts[1])
    approved = False
    async with ctx.pool.connection() as conn:
        draft = await ledger.lock_draft(conn, draft_id)
        notice = (draft["content"].get("outside_ticket") or {}) if draft else {}
        card = notice.get("channel_card") if isinstance(notice, dict) else None
        if (
            not draft
            or not person["active"]
            or draft["kind"] != "github_assign_issue"
            or not notice
            or not card
            or draft["approver_person_id"] != person["id"]
            or action.get("channel") != card.get("channel")
            or action.get("message_ts") != card.get("ts")
        ):
            return
        run = await runs.get(conn, draft["run_id"])
        if not run or run["person_id"] != person["id"] or run.get("provider") != "code":
            return
        if draft["state"] != "pending":
            await sync_card(ctx, conn, draft)
            return
        if draft["created_at"] + timedelta(hours=24) <= datetime.now(UTC):
            await ledger.expire_draft(conn, draft_id)
        elif parts[2] == "approve" and (cards.draft_preview(draft) is None or not can_perform(ctx, draft)):
            return
        else:
            want_approve = parts[2] == "approve"
            decided = await drafts.decide_outside_ticket(
                conn, draft_id, person_id=person["id"], approve=want_approve
            )
            approved = want_approve and decided
            if decided:
                await audit.write(
                    conn,
                    principal=f"person:{person['key']}",
                    tool="github_assign_issue",
                    args=draft["content"]["arguments"],
                    decision="allow" if approved else "deny",
                    reason="Approved from channel ticket card" if approved else "Rejected from channel ticket card",
                    target=f"draft {draft_id}",
                )
            draft = await ledger.lock_draft(conn, draft_id)
    if approved:
        try:
            await perform(ctx, draft)
        except Exception:
            async with ctx.pool.connection() as conn:
                await ledger.draft_failed(conn, draft_id)
                await sensors.error(
                    conn,
                    f"draft:{draft_id}",
                    "Draft delivery could not be confirmed. Check the destination.",
                )
        else:
            async with ctx.pool.connection() as conn:
                await drafts.mark_sent(conn, draft_id)
    async with ctx.pool.connection() as conn:
        draft = await ledger.lock_draft(conn, draft_id)
        await sync_card(ctx, conn, draft)


async def _resolve_assignee(ctx, conn, person, target: str) -> str | None:
    lower = target.strip().lower()
    if lower in {"me", "myself"}:
        return person.get("github_login") or None
    mention = re.fullmatch(r"<@([A-Z0-9_]+)>", target.strip())
    if mention:
        found = await people.by_slack_id(conn, mention.group(1))
        return found.get("github_login") if found and found["active"] else None
    for agent in ctx.workspace.agents:
        if agent.wake.type == "github_issue" and lower in {agent.key.lower(), agent.name.lower()}:
            return agent.wake.assignee
    members = await all_rows(conn, "SELECT key, name, github_login FROM people WHERE active")
    matches = [
        m["github_login"]
        for m in members
        if m.get("github_login")
        and lower in {m["key"].lower(), m["name"].lower(), m["github_login"].lower()}
    ]
    return matches[0] if len(set(matches)) == 1 else None


async def claim_outside_assign(ctx, event, authority) -> str | None:
    """Deterministically assign a thread's outside-ticket issue when the owner says '@Illo assign'."""
    if (
        event.channel != "slack"
        or event.kind != "message"
        or not event.payload.get("mentioned")
        or not event.payload.get("thread_ts")
        or event.payload.get("bot")
        or event.payload.get("webhook")
        or event.payload.get("form")
        or event.payload.get("from_automation")
        or event.payload.get("outside_workspace")
    ):
        return None
    matched, target = parse_assign_command(event.text)
    if not matched:
        return None
    channel_id = event.payload.get("channel_id")
    thread_ts = event.payload.get("thread_ts")
    if not channel_id or not thread_ts:
        return None

    async with ctx.pool.connection() as conn:
        person = await people.by_slack_id(conn, event.sender)
        if not person or not person["active"]:
            return None
        found = await one(
            conn,
            """SELECT d.id FROM drafts d
            LEFT JOIN runs r ON r.id = d.run_id
            LEFT JOIN events e ON e.id = r.event_id
            WHERE d.kind = 'github_assign_issue'
              AND d.content ? 'outside_ticket'
              AND (
                (d.content->'outside_ticket'->'source'->>'channel_id' = %s
                 AND d.content->'outside_ticket'->'source'->>'thread_ts' = %s)
                OR (e.channel = 'slack'
                    AND e.payload->'payload'->>'channel_id' = %s
                    AND COALESCE(NULLIF(e.payload->'payload'->>'thread_ts', ''), e.payload->'payload'->>'message_ts') = %s)
              )
            ORDER BY (d.state = 'pending') DESC, d.id DESC
            LIMIT 1""",
            (channel_id, thread_ts, channel_id, thread_ts),
        )
        if not found:
            return None
        resolved_target = None
        if target is not None:
            resolved_target = await _resolve_assignee(ctx, conn, person, target)
            if not resolved_target:
                return None
        draft = await ledger.lock_draft(conn, found["id"])
        if not draft:
            return None
        run = await runs.get(conn, draft["run_id"])
        if not run or run.get("provider") != "code":
            return None
        notice = draft["content"].get("outside_ticket") or {}
        if person["id"] != draft["approver_person_id"] and person["role"] != "owner":
            owner_name = notice.get("owner") or "the owner"
            return f"Only {owner_name} can assign this alert ticket."

        args = dict(draft["content"]["arguments"])
        repo, number = args["repo"], args["number"]
        desired = resolved_target or args["assignee"]
        issue_url = (draft["content"].get("github_issue") or {}).get("link") or f"https://github.com/{repo}/issues/{number}"
        label = f"{repo.split('/')[-1]}#{number}"
        if draft["state"] == "sent" and args["assignee"].lower() == desired.lower():
            return f"Already assigned: <{cards.escape(issue_url)}|{cards.escape(label)}> (`{cards.escape(args['assignee'])}`)."

        use_pending = (
            draft["state"] == "pending"
            and draft["created_at"] + timedelta(hours=24) > datetime.now(UTC)
            and person["id"] == draft["approver_person_id"]
            and desired.lower() == args["assignee"].lower()
            and can_perform(ctx, draft)
        )
        if use_pending:
            decided = await drafts.decide_outside_ticket(
                conn, draft["id"], person_id=person["id"], approve=True
            )
            if not decided:
                return f"Could not approve assignment for <{cards.escape(issue_url)}|{cards.escape(label)}>."
            await audit.write(
                conn,
                principal=f"person:{person['key']}",
                tool="github_assign_issue",
                args=args,
                decision="allow",
                reason="Approved via thread assign command",
                target=f"draft {draft['id']}",
            )
            draft = await ledger.lock_draft(conn, draft["id"])

    if use_pending:
        try:
            await perform(ctx, draft)
        except Exception:
            async with ctx.pool.connection() as conn:
                await ledger.draft_failed(conn, draft["id"])
                await sensors.error(
                    conn,
                    f"draft:{draft['id']}",
                    "Draft delivery could not be confirmed. Check the destination.",
                )
            async with ctx.pool.connection() as conn:
                draft = await ledger.lock_draft(conn, draft["id"])
                await sync_card(ctx, conn, draft)
            return f"I could not assign <{cards.escape(issue_url)}|{cards.escape(label)}> on GitHub. Check GitHub access or assign it directly."
        async with ctx.pool.connection() as conn:
            await drafts.mark_sent(conn, draft["id"])
            mapping = await one(
                conn,
                "SELECT task_id FROM task_tickets WHERE url=%s",
                (f"https://github.com/{repo}/issues/{number}",),
            )
            draft = await ledger.lock_draft(conn, draft["id"])
            await sync_card(ctx, conn, draft)
        task_note = f" Task #{mapping['task_id']} updated." if mapping else ""
        return f"Assigned <{cards.escape(issue_url)}|{cards.escape(label)}> to `{cards.escape(desired)}`.{task_note}"

    # Fallback path: expired/rejected draft or explicit assignee override by owner on a private repo.
    from illo.connectors.github_tasks import assigned as task_assigned
    from illo.tools.github import repository

    try:
        canonical_repo = repository(ctx, repo)
        facts = await ctx.github_client.get_repository(canonical_repo)
    except Exception:
        return f"I could not verify GitHub repository access for <{cards.escape(issue_url)}|{cards.escape(label)}>."
    if facts.get("private") is not True:
        return f"<{cards.escape(issue_url)}|{cards.escape(label)}> is in a public repository. Approve assignment from your DM card."

    try:
        await ctx.github_client.assign_issue(canonical_repo, number, [desired])
    except Exception:
        return f"I could not assign <{cards.escape(issue_url)}|{cards.escape(label)}> on GitHub. Check GitHub access or assign it directly."

    async with ctx.pool.connection() as conn:
        await audit.write(
            conn,
            principal=f"person:{person['key']}",
            tool="github_assign_issue",
            args={"repo": canonical_repo, "number": number, "assignee": desired},
            decision="allow",
            reason="Assigned via thread assign command",
            target=f"draft {draft['id']}",
        )
        mapping = await one(
            conn,
            "SELECT task_id FROM task_tickets WHERE url=%s",
            (f"https://github.com/{canonical_repo}/issues/{number}",),
        )
        if mapping:
            await task_assigned(ctx, conn, mapping["task_id"], desired)
        locked = await ledger.lock_draft(conn, draft["id"])
        if locked:
            content = dict(locked["content"])
            content["arguments"] = {**content["arguments"], "assignee": desired}
            await conn.execute(
                "UPDATE drafts SET state='sent', decided_at=COALESCE(decided_at, now()), content=%s WHERE id=%s",
                (Jsonb(content), locked["id"]),
            )
            locked = await ledger.lock_draft(conn, draft["id"])
            await sync_card(ctx, conn, locked)
    task_note = f" Task #{mapping['task_id']} updated." if mapping else ""
    return f"Assigned <{cards.escape(issue_url)}|{cards.escape(label)}> to `{cards.escape(desired)}`.{task_note}"

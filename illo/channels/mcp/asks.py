"""Write asks for the asks service. Deliver answers only to their requester."""

from datetime import datetime

from illo.asks import cards, proof
from illo.asks import service as asks_service
from illo.board import doors
from illo.db import one
from illo.ledger import ask_workflow as workflow
from illo.ledger import asks, board, items, lanes, mcp
from illo.tasks import pages
from illo.tasks.pages import outside
from illo.tools._comms_guard import contains_secret, has_hidden_format
from illo.tools.registry import Refusal
from illo.workspace import agent_acts_for_owner

from .lanes import check_visible, owner


def answer(row: dict) -> dict:
    option = next((o for o in row["options"] if o["id"] == row["answer_option"]), None)
    return {
        "ask_id": row["id"],
        "state": row["state"],
        "source_ref": row["source_ref"],
        "question": row["question"],
        "answer_option": option,
        "answer_text": row["answer_text"],
        "answered_by": row.get("answered_by", row["answered_by_person_id"]),
        "answered_via": row["answered_via"],
        "rule_text": row.get("rule_text"),
        "answered_at": row["answered_at"],
    }


def timestamp(value):
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed
    except ValueError:
        raise ValueError("Use an ISO timestamp with a time zone") from None


async def create(ctx, args: dict) -> dict:
    if (args["source_ref"] or "").startswith("board:"):
        raise Refusal("This reply key is reserved for the board.")
    multi = args["owners"] is not None
    if not multi:
        existing = await mcp.existing_ask(ctx.conn, ctx.token["id"], args["source_ref"])
        if existing:
            return await created_result(ctx, existing)
    if args["blocks"] and not pages.enabled(ctx.app):
        raise Refusal("This team has no pages. Put the text in proof.text and the addresses in proof.links.")
    targets = []
    seen_ids = set()
    raw_owners = args["owners"] if multi else [args["owner"]]
    for raw in raw_owners:
        person = await owner(ctx, raw)
        if person["id"] not in seen_ids:
            seen_ids.add(person["id"])
            targets.append(person)
    saved = await proof.saved_items(ctx.conn, args["proof"], ctx.principal)
    for ref, item in saved.items():
        if not item:
            raise ValueError(f"You cannot see {ref}.")
        missing_for_target = False
        for target in targets:
            if not await items.get(ctx.conn, ref, principal="person:" + target["key"]):
                missing_for_target = True
                break
        if missing_for_target:
            if (
                args["share_saved"]
                and item["kind"] == "saved"
                and item["owner_person_id"] == ctx.token["person_id"]
            ):
                combined = f"{item['title']}\n{item['body']}"
                if contains_secret(combined) or has_hidden_format(combined):
                    raise Refusal(
                        "This saved item cannot be shared automatically because it contains "
                        "sensitive credentials or hidden control characters."
                    )
                published = await items.publish(ctx.conn, ref, owner_person_id=ctx.token["person_id"])
                if not published:
                    raise ValueError(f"The person who must answer cannot see {ref}.")
            else:
                raise ValueError(f"The person who must answer cannot see {ref}.")
    files = await outside.prepare(ctx.app, args["blocks"], store_refs=True)
    await mcp.lock_token(ctx.conn, ctx.token["id"], "asks")
    if not multi:
        existing = await mcp.existing_ask(ctx.conn, ctx.token["id"], args["source_ref"])
        if existing:
            return await created_result(ctx, existing)
    lane = None
    if args["lane"]:
        ref = args["lane"]
        await mcp.lock_lane_ref(ctx.conn, ref["source"], ref["external_id"])
        lane = await mcp.lane_by_ref(ctx.conn, **ref)
        check_visible(ctx, lane)
        if not lane:
            if ref["source"] == "github":
                raise ValueError("Only the GitHub poller can create GitHub lanes")
            lane = await lanes.upsert(
                ctx.conn,
                **ref,
                title=ref["external_id"],
                owner_person_id=ctx.token["person_id"],
                machine=ctx.token["label"],
                visibility="private",
            )
    for person in targets:
        if lane and lane["visibility"] == "private" and lane["owner_person_id"] != person["id"]:
            raise ValueError("The lane is not visible to the owner")
    if not pages.enabled(ctx.app) and ctx.token["scopes"] == ["agent"]:
        # The surrounding transaction also rolls back a lane when the card is too large.
        data = await workflow.card_data(ctx.conn, {
            **args, "id": 1, "state": "open", "recommended_option": args["recommended"],
            "owner_person_id": targets[0]["id"], "requester_token_id": ctx.token["id"],
            "lane_id": lane["id"] if lane else None, "rule_id": None,
            "due_at": timestamp(args["due_at"]), "full_proof": True, "saved_items": saved})
        blocks = cards.ask_card(data)
        if len(blocks) > 45 or sum(len(b.get("text", {}).get("text", "")) for b in blocks) > 30000:
            raise ValueError(proof.CARD_LIMIT)
    due = timestamp(args["due_at"])
    expires = timestamp(args["expires_at"])
    results = []
    for person in targets:
        source_ref = (
            f"{args['source_ref']}:{person['key']}"
            if (multi and args["source_ref"])
            else args["source_ref"]
        )
        if multi and source_ref:
            existing = await mcp.existing_ask(ctx.conn, ctx.token["id"], source_ref)
            if existing:
                results.append({**await created_result(ctx, existing), "owner": person["key"]})
                continue
        row = await asks.create(
            ctx.conn,
            owner_person_id=person["id"],
            requester_token_id=ctx.token["id"],
            requester_person_id=ctx.token["person_id"],
            lane_id=lane["id"] if lane else None,
            kind=args["kind"],
            question=args["question"],
            options=args["options"],
            recommended_option=args["recommended"],
            proof=args["proof"],
            blocks=args["blocks"],
            due_at=due,
            expires_at=expires,
            source_ref=source_ref,
        )
        await outside.replace(ctx.conn, files, ask_id=row["id"])
        if doors.coordinator_active(ctx.app.workspace):
            await doors.attach_ask_to_board(
                ctx.conn, ctx.app.workspace, row, token=ctx.token, lane=lane, owner=person
            )
        else:
            await mcp.take_ask_card(ctx.conn, row)
        result = await created_result(ctx, row)
        if multi:
            result = {**result, "owner": person["key"]}
        results.append(result)
    if multi:
        return {"asks": results, "count": len(results)}
    return results[0]


async def created_result(ctx, row):
    data = await workflow.card_data(ctx.conn, row)
    page_url = pages.base_url(ctx.app) + f"/web/a/{row['id']}" if pages.enabled(ctx.app) else None
    notes = outside.notes(row.get("blocks", []))
    return {**answer(row), "page_url": page_url, "card_text": cards.fallback(data),
            **({"notes": notes} if notes else {})}


def _ask_summary(ctx, row: dict) -> dict:
    option = next((o for o in row["options"] if o["id"] == row["answer_option"]), None)
    page_url = pages.base_url(ctx.app) + f"/web/a/{row['id']}" if pages.enabled(ctx.app) else None
    return {
        "ask_id": row["id"],
        "state": row["state"],
        "kind": row["kind"],
        "owner": row["owner"],
        "requester": row["requester"],
        "question": row["question"],
        "options": row["options"],
        "source_ref": row["source_ref"],
        "created_at": row["created_at"],
        "due_at": row["due_at"],
        "page_url": page_url,
        "delivered_to_slack": bool(row["card_sent_at"] or row["slack_ts"]),
        "card_sent_at": row["card_sent_at"],
        "answer_option": option,
        "answer_text": row["answer_text"],
        "answered_by": row["answered_by"],
        "answered_via": row["answered_via"],
        "rule_text": row["rule_text"],
        "answered_at": row["answered_at"],
    }


async def list_asks(ctx, args: dict) -> dict:
    owner_key = (await owner(ctx, args["owner"]))["key"] if args["owner"] else None
    rows = await asks.mcp_list(
        ctx.conn,
        principal=ctx.principal,
        owner=owner_key,
        state=args["state"],
        ask_id=args["ask_id"],
        limit=args["limit"],
    )
    return {"asks": [_ask_summary(ctx, row) for row in rows]}


async def pull(ctx, args: dict) -> dict:
    rows = await mcp.pull_answers(ctx.conn, ctx.token, args["scope"])
    return {
        "answers": [
            {
                **answer(row),
                "lane": {"source": row["lane_source"], "external_id": row["lane_external_id"]}
                if row["lane_source"]
                else None,
            }
            for row in rows
        ]
    }


async def ack(ctx, args: dict) -> dict:
    row = await mcp.ack_answer(ctx.conn, ctx.token, args["ask_id"])
    if not row:
        raise ValueError("The delivered ask was not found")
    return {"ask_id": row["id"], "delivery_state": row["delivery_state"]}


async def cancel(ctx, args: dict) -> dict:
    row = await mcp.cancel_ask(ctx.conn, ctx.token, args["ask_id"], args["source_ref"])
    if not row:
        raise ValueError("The ask was not found")
    return {"ask_id": row["id"], "state": row["state"]}


def _delegation_info(workspace, token: dict) -> tuple[dict, str]:
    agent_key = token.get("agent_key")
    agent = (
        next((a for a in workspace.agents if a.key == agent_key), None) if agent_key else None
    )
    info = {
        "via": "personal_agent",
        "token_id": token["id"],
        "token_label": token["label"],
        **({"agent_key": agent_key} if agent_key else {}),
        **({"agent_name": agent.name} if agent else {}),
    }
    label = agent.name if agent else token["label"]
    return info, label


async def _check_not_self_answer(conn, ask: dict, token: dict) -> None:
    if ask.get("requester_token_id") == token["id"]:
        raise Refusal("An agent cannot answer an ask that it created itself.")
    agent_key = token.get("agent_key")
    if not agent_key:
        return
    if ask.get("requester_token_id"):
        req_token = await one(
            conn, "SELECT agent_key FROM tokens WHERE id=%s", (ask["requester_token_id"],)
        )
        if req_token and req_token["agent_key"] == agent_key:
            raise Refusal("An agent cannot answer an ask created by the same agent.")
    same_actor = await one(
        conn,
        "SELECT 1 FROM board_posts WHERE ask_id=%s AND actor=%s LIMIT 1",
        (ask["id"], f"agent:{agent_key}"),
    )
    if same_actor:
        raise Refusal("An agent cannot answer an ask created by the same agent.")


async def answer_ask(ctx, args: dict) -> dict:
    if not agent_acts_for_owner(ctx.app.workspace, ctx.token.get("agent_key")):
        raise Refusal(
            "Only a personal interactive agent can answer asks on its owner's behalf "
            "(autonomous workers cannot self-approve)."
        )
    text = args.get("text")
    if text and (contains_secret(text) or has_hidden_format(text)):
        raise Refusal("Answer text cannot contain secrets or hidden control characters.")
    delegated_by, agent_label = _delegation_info(ctx.app.workspace, ctx.token)
    async with ctx.conn.transaction():
        ask = await board.lock_for_ask(ctx.conn, args["ask_id"])
        if not ask or not await asks_service.is_live(ctx.conn, ask):
            raise ValueError("The ask is no longer open.")
        if ask["owner_person_id"] != ctx.token["person_id"]:
            raise Refusal("You can only answer asks assigned to your person.")
        await _check_not_self_answer(ctx.conn, ask, ctx.token)
        option = args.get("option")
        if option is not None and not any(o["id"] == option for o in ask["options"]):
            raise ValueError("Choose one of the ask's option IDs.")
        answered = await asks_service.answer(
            ctx.conn,
            ask["id"],
            ctx.token["person_id"],
            option=option,
            text=text,
            workspace=ctx.app.workspace,
            delegated_by=delegated_by,
        )
        if not answered:
            raise ValueError("The ask cannot accept this answer.")
    await ctx.conn.commit()
    await asks_service.notify_agent_answer(ctx.app, ctx.conn, answered, agent_label)
    result = {**answer(answered), "answered": True}
    if "board_post_id" in answered:
        result["board_post_id"] = answered["board_post_id"]
    return result


async def reassign_ask(ctx, args: dict) -> dict:
    if not agent_acts_for_owner(ctx.app.workspace, ctx.token.get("agent_key")):
        raise Refusal(
            "Only a personal interactive agent can reassign asks on its owner's behalf."
        )
    new_owner = await owner(ctx, args["owner"])
    board_info = None
    old_channel = None
    old_ts = None
    is_board = False
    async with ctx.conn.transaction():
        ask = await board.lock_for_ask(ctx.conn, args["ask_id"])
        if not ask or not await asks_service.is_live(ctx.conn, ask):
            raise ValueError("The ask is no longer open.")
        if ctx.token["person_id"] not in {ask["owner_person_id"], ask["requester_person_id"]}:
            raise Refusal("Only the current owner or requester can reassign this ask.")
        caller = await one(
            ctx.conn, "SELECT * FROM people WHERE id=%s AND active", (ctx.token["person_id"],)
        )
        if not caller:
            raise Refusal("Caller is inactive.")
        if new_owner["id"] == ask["owner_person_id"]:
            return {
                "reassigned": False,
                "ask_id": ask["id"],
                "owner": new_owner["key"],
                "reason": "already_owner",
            }
        if ask["lane_id"]:
            lane = await lanes.get(ctx.conn, ask["lane_id"], principal=ctx.principal)
            if not lane or (
                lane["visibility"] == "private" and lane["owner_person_id"] != new_owner["id"]
            ):
                raise ValueError("The lane is not visible to the new owner.")
        saved = await proof.saved_items(ctx.conn, ask.get("proof") or {}, ctx.principal)
        for ref, item in saved.items():
            if not item or not await items.get(
                ctx.conn, ref, principal="person:" + new_owner["key"]
            ):
                raise ValueError(f"The new owner cannot see {ref}.")
        old_owner_id = ask["owner_person_id"]
        old_channel, old_ts = ask.get("slack_channel"), ask.get("slack_ts")
        is_board = await asks.is_board_ask(ctx.conn, ask)
        if is_board:
            board_info = await doors.reassign_board_ask(
                ctx.conn,
                ctx.app.workspace,
                ask,
                old_owner_id=old_owner_id,
                new_owner=new_owner,
                caller=caller,
                slack_connected=bool(ctx.app.slack_client),
            )
        updated = await asks.reassign(ctx.conn, ask["id"], new_owner_id=new_owner["id"])
        if not updated:
            raise ValueError("The ask is no longer open.")
    await ctx.conn.commit()
    if ctx.app.slack_client and old_channel and old_ts:
        notice = f"Reassigned to {new_owner['name']}."
        try:
            await ctx.app.slack_client.chat_update(
                channel=old_channel,
                ts=old_ts,
                text=notice,
                blocks=[cards.section(f"Reassigned to *{cards.escape(new_owner['name'])}*.")],
            )
        except Exception:
            pass
    if not is_board and ctx.app.slack_client:
        try:
            async with ctx.conn.transaction():
                await asks_service.sync_card(ctx.app, ctx.conn, updated)
            await ctx.conn.commit()
        except Exception:
            pass
    result = {
        "reassigned": True,
        "ask_id": updated["id"],
        "owner": new_owner["key"],
    }
    if board_info:
        result["thread_id"] = board_info["thread_id"]
    return result

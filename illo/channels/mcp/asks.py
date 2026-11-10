"""Write asks for the asks service. Deliver answers only to their requester."""

from datetime import datetime

from illo.ledger import asks, lanes, mcp

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
    existing = await mcp.existing_ask(ctx.conn, ctx.token["id"], args["source_ref"])
    if existing:
        return answer(existing)
    person = await owner(ctx, args["owner"])
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
    if lane and lane["visibility"] == "private" and lane["owner_person_id"] != person["id"]:
        raise ValueError("The lane is not visible to the owner")
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
        due_at=timestamp(args["due_at"]),
        expires_at=timestamp(args["expires_at"]),
        source_ref=args["source_ref"],
    )
    return answer(row)


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

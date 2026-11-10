"""Accept lane snapshots and collector liveness without a model run."""

import json
from datetime import UTC, datetime

from illo.ledger import items, lanes, mcp, people, sensors


async def owner(ctx, value: str | None) -> dict:
    if not value:
        return {"id": ctx.token["person_id"], "key": ctx.token["person_key"]}
    person = (
        await people.get(ctx.conn, value)
        or await people.by_github_login(ctx.conn, value)
        or await people.by_email(ctx.conn, value)
        or await people.by_slack_id(ctx.conn, value)
    )
    if not person:
        raise ValueError("The owner was not found")
    return person


def check_visible(ctx, row):
    if row and row["visibility"] == "private" and row["owner_person_id"] != ctx.token["person_id"]:
        raise ValueError("The lane was not found")


async def report(ctx, args: dict) -> dict:
    batch = args.pop("lanes", None)
    collector = args.pop("collector", None)
    if collector:
        await sensors.ok(ctx.conn, "collector:" + collector["label"], json.dumps(collector))
    if batch is not None:
        return {"lanes": [await report_one(ctx, lane) for lane in batch]}
    return await report_one(ctx, args)


async def report_one(ctx, args: dict) -> dict:
    if args["source"] == "github":
        raise ValueError("Only the GitHub poller can report GitHub lanes")
    await mcp.lock_lane_ref(ctx.conn, args["source"], args["external_id"])
    previous = await mcp.lane_by_ref(ctx.conn, args["source"], args["external_id"], lock=True)
    if previous and previous["owner_person_id"] != ctx.token["person_id"]:
        raise ValueError("The lane belongs to another person")
    values = {key: value for key, value in args.items() if key not in {"owner", "event"}}
    # An omitted field keeps the stored snapshot, including its private visibility.
    base = (
        {
            key: previous[key]
            for key in (
                "source",
                "external_id",
                "title",
                "state",
                "summary",
                "links",
                "repo",
                "machine",
                "visibility",
                "end_summary",
                "owner_person_id",
            )
        }
        if previous
        else {}
    )
    base.update(values)
    base["title"] = base.get("title") or args["external_id"]
    if args.get("owner") or not previous:
        base["owner_person_id"] = (await owner(ctx, args.get("owner")))["id"]
    if base.get("visibility") == "private" and base["owner_person_id"] != ctx.token["person_id"]:
        raise ValueError("A private lane must belong to the calling person")
    lane = await lanes.upsert(ctx.conn, **base)
    await mcp.sync_lane_summary_visibility(ctx.conn, lane)
    changed = previous is None or previous["state"] != lane["state"]
    if args.get("event") or changed:
        await lanes.add_event(
            ctx.conn,
            lane["id"],
            "state" if changed else "report",
            args.get("event") or f"The lane state is {lane['state']}.",
            {
                "previous_state": previous["state"] if previous else None,
                "state": lane["state"],
                "token_id": ctx.token["id"],
                "token_label": ctx.token["label"],
            },
        )
    if lane["end_summary"] and lane["state"] in {"done", "failed"}:
        await items.upsert(
            ctx.conn,
            source_ref=f"lane:{lane['id']}:end",
            kind="lane_summary",
            title=lane["title"],
            body=lane["end_summary"],
            summary=lane["end_summary"][:500],
            owner_person_id=lane["owner_person_id"],
            visibility=lane["visibility"],
            links=lane["links"],
            meta={"lane_id": lane["id"]},
            source_at=datetime.now(UTC),
        )
    return {"lane_id": lane["id"], "state": lane["state"]}


async def search(ctx, args: dict) -> dict:
    if args["owner"]:
        args["owner"] = (await owner(ctx, args["owner"]))["key"]
    limits = ctx.app.workspace.config.stale_minutes.model_dump(by_alias=True)
    if args["state"] == "stale":
        rows = await mcp.stale_lanes(
            ctx.conn,
            principal=ctx.principal,
            query=args["query"],
            owner=args["owner"],
            limit=args["limit"],
            stale_minutes=limits,
        )
    else:
        rows = await lanes.search(ctx.conn, principal=ctx.principal, stale_minutes=limits, **args)
    return {"lanes": rows}

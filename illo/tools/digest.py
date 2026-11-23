"""Read the snapshot of the authenticated person, with no owner argument."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import Field

from illo.db import one
from illo.ledger import ask_workflow, briefs, digest_snapshot, people
from illo.tools._format import data_text, json_text
from illo.tools.registry import Arguments, Refusal, Tool


async def digest_snapshot_read(ctx, args):
    person = await people.get(ctx.conn, ctx.principal[7:])
    if not person or person["id"] != ctx.run["person_id"]:
        raise ValueError("The snapshot needs the person of the run")
    event = await one(
        ctx.conn, "SELECT channel,payload FROM events WHERE id=%s", (ctx.run.get("event_id"),)
    )
    if event and event["channel"] in {"digest", "deep"} and "digest_snapshot" in event["payload"].get("payload", {}):
        snapshot = event["payload"]["payload"]["digest_snapshot"]
    else:
        snapshot = await digest_snapshot.build(
            ctx.conn, person, datetime.now(UTC),
            ctx.app.workspace,
        )
    return data_text("snapshot", json_text(snapshot))



async def team_snapshot_read(ctx, args):
    event = await one(
        ctx.conn, "SELECT channel,payload FROM events WHERE id=%s", (ctx.run.get("event_id"),)
    )
    if event and event["channel"] in {"digest", "team_digest", "deep"} and "team_snapshot" in event["payload"].get("payload", {}):
        snapshot = event["payload"]["payload"]["team_snapshot"]
    else:
        snapshot = await digest_snapshot.team_snapshot(
            ctx.conn, ctx.app.workspace, datetime.now(UTC)
        )
    return data_text("team-snapshot", json_text(snapshot))


class BriefSettings(Arguments):
    action: Literal["get", "set"] = "get"
    enabled: bool | None = None
    time: str | None = Field(default=None, pattern=r"^([01][0-9]|2[0-3]):[0-5][05]$")


async def brief_settings(ctx, args):
    person = await people.get(ctx.conn, ctx.principal[7:])
    if not person or person["id"] != ctx.run["person_id"]:
        raise Refusal("Brief settings need the person of the run")
    event = await one(
        ctx.conn, "SELECT channel,kind FROM events WHERE id=%s", (ctx.run.get("event_id"),)
    )
    if not event or event["channel"] != "slack" or event["kind"] not in {"dm", "message.im"}:
        raise Refusal("Change brief settings in your own Slack DM")
    await ask_workflow.lock_digest(ctx.conn, person["key"])
    prefs = await briefs.settings(ctx.conn, person)
    if args["action"] == "set":
        if args.get("enabled") is None and args.get("time") is None:
            raise Refusal("Set enabled or time")
        prefs = await one(
            ctx.conn,
            """UPDATE brief_preferences SET enabled=COALESCE(%s,enabled),
            local_time=COALESCE(%s,local_time) WHERE person_id=%s RETURNING *""",
            (args.get("enabled"), args.get("time"), person["id"]),
        )
    elif args.get("enabled") is not None or args.get("time") is not None:
        raise Refusal("Use set to change the brief")
    return {
        "enabled": prefs["enabled"],
        "time": prefs["local_time"],
        "schedule": briefs.schedule(ctx.app.workspace.config, prefs),
        "timezone": person["timezone"],
    }


def register(registry):
    registry.add(
        Tool(
            "digest_snapshot",
            "digest",
            "read",
            lambda args: "ledger",
            True,
            digest_snapshot_read,
            "Read this person's task brief: Needs you, Your agents, Done since the last brief, and Due. It also keeps open asks and owner service faults. It counts as a personal read.",
            Arguments,
            profiles=frozenset({"person_dm"}),
        )
    )

    registry.add(
        Tool(
            "team_snapshot",
            "digest",
            "read",
            lambda args: "ledger",
            False,
            team_snapshot_read,
            "Read the team task brief: Done, Waiting on people, Stuck, and In work. It holds team tasks only. It never reads private tasks or personal asks.",
            Arguments,
            profiles=frozenset({"routine", "team_channel", "person_dm"}),
        )
    )
    registry.add(
        Tool(
            "brief_settings",
            "digest",
            "write_internal",
            lambda args: "ledger",
            True,
            brief_settings,
            "Read or change this person's own morning brief: on or off, and the local time when it arrives. Change it only when the person asks, then confirm the new setting in one line. It cannot change the brief of another person.",
            BriefSettings,
            profiles=frozenset({"person_dm"}),
        )
    )

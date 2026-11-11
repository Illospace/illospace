"""Read the snapshot of the authenticated person, with no owner argument."""

from datetime import UTC, datetime

from illo.db import one
from illo.ledger import digest_snapshot, people
from illo.tools.registry import Arguments, Tool


async def digest_snapshot_read(ctx, args):
    person = await people.get(ctx.conn, ctx.principal[7:])
    if not person or person["id"] != ctx.run["person_id"]:
        raise ValueError("The snapshot needs the person of the run")
    event = await one(
        ctx.conn, "SELECT channel,payload FROM events WHERE id=%s", (ctx.run.get("event_id"),)
    )
    if event and event["channel"] == "digest":
        return event["payload"]["payload"]["digest_snapshot"]
    return await digest_snapshot.build(
        ctx.conn,
        person,
        datetime.now(UTC),
        ctx.app.workspace.config.stale_minutes.model_dump(by_alias=True),
    )


def register(registry):
    registry.add(
        Tool(
            "digest_snapshot",
            "digest",
            "read",
            lambda args: "ledger",
            True,
            digest_snapshot_read,
            "Read what needs you, stuck work, finished work, and service faults",
            Arguments,
            profiles=frozenset({"person_dm"}),
        )
    )

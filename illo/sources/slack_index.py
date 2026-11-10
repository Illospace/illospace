"""Read verified team-channel events when the derived Slack index is missing."""

import re

from illo.channels.slack.access import channel_private
from illo.db import all_rows, one

KINDS = ("message", "channel")
VECTOR = (
    "to_tsvector('simple',coalesce(e.payload->>'text','')) || "
    "to_tsvector('english',coalesce(e.payload->>'text',''))"
)
VISIBLE = """e.channel='slack' AND e.principal='team' AND e.kind=ANY(%s)
    AND e.payload #>> '{payload,channel_id}' = ANY(%s)"""


async def channels(ctx, conn):
    """Configuration and the Slack API must both say that the channel is public."""
    allowed = {}
    for channel in ctx.workspace.config.slack.channels:
        if channel.visibility == "team" and channel.mode != "off":
            if not await channel_private(ctx, channel.id, conn=conn):
                allowed[channel.id] = channel.name
    return allowed


def detail(row, allowed):
    return {
        "source_ref": f"slack-event:{row['id']}", "kind": "slack", "visibility": "team",
        "title": "#" + allowed[row["payload"]["payload"]["channel_id"]],
        "body": row["payload"].get("text", ""), "source_at": row["created_at"], "links": [],
    }


async def search(ctx, conn, query, kinds, limit):
    from illo.sources.search import RELAXED_QUERY, STRICT_QUERY, Hit

    if kinds is not None and "slack" not in kinds:
        return []
    allowed = await channels(ctx, conn)
    if not allowed:
        return []

    async def find(tsquery):
        return await all_rows(conn, f"""
            WITH q AS (SELECT {tsquery} AS query)
            SELECT e.*,ts_rank(({VECTOR}),q.query) AS score,
              ts_headline('simple',coalesce(e.payload->>'text',''),q.query,
                'MaxWords=45,MinWords=15,StartSel="",StopSel=""') AS snippet
            FROM events e,q WHERE {VISIBLE} AND ({VECTOR}) @@ q.query
              AND NOT EXISTS (SELECT 1 FROM items i WHERE i.source_ref='slack:'||e.external_id)
            ORDER BY score DESC,e.created_at DESC,e.id DESC LIMIT %s
        """, (query, query, list(KINDS), list(allowed), limit))

    rows = [(row, "strict") for row in await find(STRICT_QUERY)]
    if len(rows) < limit and not re.search(r'(?:^|\s)-\S|"|(?:^|\s)(?:OR|or)(?=\s|$)', query):
        seen = {row["id"] for row, _ in rows}
        rows += [(row, "fallback") for row in await find(RELAXED_QUERY) if row["id"] not in seen]
    return [Hit(f"slack-event:{row['id']}", "slack", "slack", detail(row, allowed)["title"],
                row["snippet"], at=row["created_at"], match_tier=tier)
            for row, tier in rows[:limit]]


async def get(ctx, conn, source_ref):
    if not re.fullmatch(r"slack-event:[1-9][0-9]*", source_ref):
        return None
    try:
        allowed = await channels(ctx, conn)
    except Exception:
        return None
    row = await one(conn, f"SELECT e.* FROM events e WHERE {VISIBLE} AND e.id=%s",
                    (list(KINDS), list(allowed), int(source_ref.split(":")[1])))
    return detail(row, allowed) if row else None


async def setup(ctx) -> None:
    """Search reads events directly. No backfill or new grant is needed."""

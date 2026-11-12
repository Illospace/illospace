"""Read verified team-channel events when the derived Slack index is missing."""

import re

from illo.channels.slack.access import channel_private
from illo.channels.slack.channels import member_channels
from illo.db import all_rows, one

KINDS = ("message", "channel")
VECTOR = "illo_search_vector('','',coalesce(e.payload->>'text',''))"
VISIBLE = """e.channel='slack' AND e.principal='team' AND e.kind=ANY(%s)
    AND e.payload #>> '{payload,channel_id}' = ANY(%s)"""


async def channels(ctx, conn):
    """Configuration and Slack membership must permit team access."""
    # Read IDs only here. Text stays behind the current channel proof.
    rows = await all_rows(conn, """
        SELECT DISTINCT payload #>> '{payload,channel_id}' AS id FROM events WHERE channel='slack'
        UNION SELECT split_part(source_ref,':',2) AS id FROM items WHERE kind='slack'
    """)
    ids = {c.id for c in ctx.workspace.config.slack.channels}
    ids.update(r["id"] for r in rows if r["id"] and r["id"].startswith(("C", "G")))
    try:
        names = await member_channels(ctx, conn=conn)
    except ValueError:
        names = {}
    allowed = {}
    for channel_id in sorted(ids):
        channel = ctx.workspace.config.slack.channel(channel_id)
        if channel.visibility == "team" and channel.mode != "off":
            if not await channel_private(ctx, channel.id, conn=conn):
                allowed[channel.id] = names.get(channel.id, channel.name)
    return allowed


def detail(row, allowed):
    return {
        "source_ref": f"slack-event:{row['id']}", "kind": "slack", "visibility": "team",
        "title": "#" + allowed[row["payload"]["payload"]["channel_id"]],
        "body": row["payload"].get("text", ""), "source_at": row["created_at"], "links": [],
    }


async def search(ctx, conn, query, kinds, limit, now):
    from illo.sources.search import RANK_COLUMNS, RANK_ORDER, Hit, query_sql

    if kinds is not None and "slack" not in kinds:
        return []
    allowed = await channels(ctx, conn)
    if not allowed:
        return []
    q, params = query_sql(query)
    rows = await all_rows(conn, f"""
        WITH q AS ({q}), texts AS (
          SELECT e.*,e.created_at AS at,({VECTOR}) AS vector
          FROM events e WHERE {VISIBLE}
            AND NOT EXISTS (SELECT 1 FROM items i WHERE i.source_ref='slack:'||e.external_id)
        ), ranked AS (
          SELECT t.*, {RANK_COLUMNS},
            ts_headline('simple',coalesce(t.payload->>'text',''),
              CASE WHEN vector @@ q.strict THEN q.strict ELSE q.relaxed END,
              'MaxWords=45,MinWords=15,StartSel="",StopSel=""') AS snippet
          FROM texts t,q WHERE vector @@ q.strict OR vector @@ q.relaxed
        ) SELECT * FROM ranked ORDER BY {RANK_ORDER} DESC,at DESC,id DESC LIMIT %s
    """, (*params, list(KINDS), list(allowed), now, limit))
    return [Hit(f"slack-event:{row['id']}", "slack", "slack", detail(row, allowed)["title"],
                row["snippet"], at=row["created_at"], match_tier=row["match_tier"],
                lexical=float(row["lexical"]), recency=float(row["recency"])) for row in rows]


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

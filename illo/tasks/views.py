"""Keep outside text inside data markers in task results."""

import json

from illo.db import all_rows, one
from illo.ledger import tasks
from illo.tools._format import data_text


def compact(row):
    return {key: (data_text("task-title", row[key]) if key == "title" else row.get(key))
            for key in ("id", "title", "list_key", "holder", "state", "waits_for", "due_at", "open_pieces", "visibility", "silent_hours")}


async def full(conn, workspace, row, viewer, *, limit=20, ctx=None):
    result = {key: value for key, value in row.items() if key != "tsv"}
    latest = await one(conn, """SELECT text FROM task_events WHERE task_id=%s
        AND kind IN ('woken','wake_failed') ORDER BY id DESC LIMIT 1""", (row["id"],))
    result["wake"] = data_text("task-wake", latest["text"]) if latest else None
    result["title"] = data_text("task-title", row["title"])
    if not row["created_by"].startswith("person:"):
        result["brief"] = data_text("task-brief", row["brief"])
    result["result"] = data_text("task-result", row["result"])
    result["links"] = [{**link, "title": data_text("task-link", link.get("title", ""))} for link in row["links"]]
    result["pieces"] = [compact(piece) for piece in await tasks.search(conn, viewer=viewer, parent_id=row["id"], limit=100)]
    result["events"] = []
    for event in await tasks.events(conn, row["id"], viewer=viewer, limit=limit):
        result["events"].append({**event, "text": data_text("task-event", event["text"]),
                                  "data": data_text("task-event-data", json.dumps(event["data"]))})
    result["checks"] = [{**check, "text": data_text("task-check", check["text"])}
                        for check in await tasks.proof(conn, workspace, row, ctx)]
    return result


async def by_thread(conn, channel, ts):
    return await all_rows(conn, "SELECT id FROM tasks WHERE slack_channel=%s AND slack_ts=%s", (channel, ts))

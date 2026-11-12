"""Keep GitHub run evidence in the ledger."""

import re
from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from illo.db import Connection, one
from illo.ledger import lanes, people


async def lock_repository(conn: Connection, repo: str) -> None:
    """Serialize creation and polling for one repository across processes."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"github:{repo}",))


async def sync_item(conn: Connection, repo: str, item: dict, label_map: dict[str, str]) -> dict:
    external_id = f"{repo}#{item['number']}"
    old = await one(
        conn,
        "SELECT * FROM lanes WHERE source='github' AND external_id=%s FOR UPDATE",
        (external_id,),
    )
    owner = None
    for candidate in [*item.get("assignees", []), item.get("user", {})]:
        if isinstance(candidate, dict) and candidate.get("login"):
            owner = await people.by_github_login(conn, candidate["login"])
            if owner:
                break
    state = item_state(item, label_map)
    comments = item.get("comments_data", [])
    last_line = ""
    if comments:
        lines = (comments[-1].get("body") or "").strip().splitlines()
        last_line = lines[-1] if lines else ""
    summary = "\n".join((item.get("body") or "").splitlines()[:4])
    if last_line:
        summary += "\n" + last_line
    links = [
        {
            "url": item.get("html_url", f"https://github.com/{repo}/issues/{item['number']}"),
            "kind": "github",
        }
    ]
    # GitHub accepts these closing keywords for issues in the same repository.
    for number in re.findall(
        r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b", item.get("body") or "", re.I
    ):
        if "pull_request" in item or "merged" in item:
            linked = await one(
                conn,
                "SELECT id FROM lanes WHERE source='github' AND external_id=%s",
                (f"{repo}#{number}",),
            )
            links.append(
                {
                    "kind": "closes",
                    "url": f"https://github.com/{repo}/issues/{number}",
                    "external_id": f"{repo}#{number}",
                    "lane_id": linked["id"] if linked else None,
                }
            )
    row = await lanes.upsert(
        conn,
        source="github",
        external_id=external_id,
        title=item["title"],
        state=state,
        owner_person_id=owner["id"] if owner else None,
        summary=summary.strip(),
        links=links,
        repo=repo,
    )
    if item.get("closed_at"):
        closed_at = datetime.fromisoformat(item["closed_at"].replace("Z", "+00:00"))
        await conn.execute("UPDATE lanes SET closed_at=%s WHERE id=%s", (closed_at, row["id"]))
    elif old and state in {"done", "failed"} and old["closed_at"]:
        await conn.execute(
            "UPDATE lanes SET closed_at=%s WHERE id=%s", (old["closed_at"], row["id"])
        )
    if old is None or old["state"] != state:
        await lanes.add_event(
            conn,
            row["id"],
            "state",
            f"State: {state}",
            {"from": old["state"] if old else None, "to": state},
        )
    if old and owner and old["owner_person_id"] != owner["id"]:
        # Keep the owner change as source evidence.
        await lanes.add_event(conn, row["id"], "owner", f"Owner: {owner['key']}")
    # Resolve links even when the pull request arrived before its issue.
    await conn.execute(
        """UPDATE lanes l SET links=(SELECT jsonb_agg(
        CASE WHEN v->>'external_id'=%s THEN v || %s::jsonb ELSE v END)
        FROM jsonb_array_elements(l.links) v) WHERE l.source='github'
        AND EXISTS (SELECT 1 FROM jsonb_array_elements(l.links) v WHERE v->>'external_id'=%s)""",
        (external_id, Jsonb({"lane_id": row["id"]}), external_id),
    )
    return await one(conn, "SELECT * FROM lanes WHERE id=%s", (row["id"],))


def item_state(item: dict, label_map: dict[str, str]) -> str:
    if item.get("state") == "closed":
        if item.get("state_reason") == "not_planned":
            return "failed"
        if ("pull_request" in item or "merged" in item) and not item.get("merged"):
            return "failed"
        return "done"
    labels = {v["name"] if isinstance(v, dict) else v for v in item.get("labels", [])}
    for label, state in label_map.items():
        if label in labels:
            return state
    if item.get("requested_reviewers") or item.get("requested_teams"):
        return "review"
    return "open" if item.get("state") == "open" else "unknown"


async def latest_run(conn: Connection) -> dict | None:
    return await one(conn, "SELECT id FROM runs ORDER BY id DESC LIMIT 1")


async def slack_healthy(conn: Connection, max_age: float) -> bool:
    row = await one(conn, "SELECT * FROM sensors WHERE key='slack'")
    return bool(
        row
        and row["last_ok_at"]
        and (not row["last_error_at"] or row["last_ok_at"] > row["last_error_at"])
        and (datetime.now(UTC) - row["last_ok_at"]).total_seconds() < max_age
    )

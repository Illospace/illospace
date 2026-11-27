"""Assemble the untainted reflection snapshot for the nightly dream routine."""

from datetime import timedelta

from illo.db import all_rows, one
from illo.ledger.digest_snapshot import recent_board_threads

MAX_WINDOW = timedelta(hours=72)
DEFAULT_WINDOW = timedelta(hours=24)
PROTECTED_ROUTINES = frozenset({"dream.md"})
PROTECTED_RULE_PROFILES = frozenset({"tainted", "outsider"})


async def dream_window(conn, now):
    """Return the start of the reflection window, clamped to at most 72 hours."""
    row = await one(conn, "SELECT window_end FROM dream_reflections ORDER BY id DESC LIMIT 1")
    if not row or not row["window_end"]:
        return now - DEFAULT_WINDOW
    since = row["window_end"]
    if since > now:
        return now - DEFAULT_WINDOW
    return max(since, now - MAX_WINDOW)


async def recent_reflections(conn, *, limit: int = 5):
    """Read the most recent dream reflections so self-improvement stays continuous."""
    rows = await all_rows(
        conn,
        """SELECT id, created_at, window_start, window_end, summary, scores, changes
        FROM dream_reflections ORDER BY id DESC LIMIT %s""",
        (limit,),
    )
    return [
        {
            "id": row["id"],
            "at": row["created_at"].isoformat(),
            "window_start": row["window_start"].isoformat(),
            "window_end": row["window_end"].isoformat(),
            "summary": row["summary"],
            "scores": row["scores"] or {},
            "changes": row["changes"] or [],
        }
        for row in rows
    ]


async def team_interactions(conn, workspace, since, *, limit: int = 25):
    """Read untainted team-channel turns with humans (never private DMs or tainted text)."""
    private_ids = {c.id for c in workspace.config.slack.channels if c.visibility != "team"}
    names = {c.id: c.name for c in workspace.config.slack.channels}
    rows = await all_rows(
        conn,
        """SELECT created_at, status,
            payload->>'task_actor' AS actor,
            payload->'payload'->>'channel_id' AS channel_id,
            payload->'payload'->>'channel_type' AS channel_type,
            payload->'payload'->>'channel_class' AS channel_class,
            payload->'payload'->>'thread_ts' AS thread_ts,
            left(payload->>'text', 600) AS user_text,
            left(payload->>'final_answer', 900) AS illo_reply
        FROM events
        WHERE channel='slack' AND principal='team' AND tainted=false
          AND status IN ('handled', 'failed') AND created_at >= %s
        ORDER BY id DESC LIMIT %s""",
        (since, limit * 2),
    )
    items = []
    for row in rows:
        cid = row["channel_id"] or ""
        if (
            cid in private_ids
            or cid.startswith(("D", "G"))
            or row["channel_type"] in {"im", "mpim", "group"}
            or row["channel_class"] == "private"
        ):
            continue
        items.append(
            {
                "at": row["created_at"].isoformat(),
                "channel": f"#{names[cid]}" if cid in names else cid,
                "thread_ts": row["thread_ts"],
                "actor": row["actor"],
                "status": row["status"],
                "user_text": row["user_text"] or "",
                "illo_reply": row["illo_reply"] or "",
            }
        )
        if len(items) >= limit:
            break
    return items


async def recent_team_briefs(conn, since, *, limit: int = 5):
    """Read team briefs posted during this reflection window."""
    rows = await all_rows(
        conn,
        """SELECT created_at, payload->'payload'->>'channel_id' AS channel_id,
            left(payload->>'final_answer', 1500) AS text
        FROM events
        WHERE channel='team_digest' AND person_id IS NULL AND tainted=false
          AND status='handled' AND created_at >= %s
          AND jsonb_typeof(payload->'final_answer')='string' AND payload->>'final_answer'<>''
        ORDER BY id DESC LIMIT %s""",
        (since, limit),
    )
    return [
        {
            "at": row["created_at"].isoformat(),
            "channel_id": row["channel_id"],
            "text": row["text"],
        }
        for row in rows
    ]


async def metrics(conn, since):
    """Aggregate Chief-of-Staff metrics without exposing private message content."""
    ev = await one(
        conn,
        """SELECT
            count(*) FILTER (WHERE channel='slack' AND principal='team' AND tainted=false AND status='handled') AS team_channel_turns,
            count(*) FILTER (WHERE channel='slack' AND kind IN ('dm','message.im') AND tainted=false AND status='handled') AS dm_turns,
            count(*) FILTER (WHERE channel='team_digest' AND person_id IS NULL AND status='handled') AS team_briefs_posted
        FROM events WHERE created_at >= %s""",
        (since,),
    )
    ask = await one(
        conn,
        """SELECT
            count(*) FILTER (WHERE created_at >= %s) AS asks_opened,
            count(*) FILTER (WHERE answered_at >= %s) AS asks_answered,
            count(*) FILTER (WHERE state='open' AND created_at < now() - interval '24 hours') AS asks_open_over_24h,
            percentile_cont(0.5) WITHIN GROUP (
                ORDER BY extract(epoch FROM (answered_at - created_at)) / 60.0
            ) FILTER (WHERE answered_at >= %s) AS median_answer_min
        FROM asks""",
        (since, since, since),
    )
    draft = await one(
        conn,
        """SELECT
            count(*) FILTER (WHERE state='approved' AND decided_at >= %s) AS drafts_approved,
            count(*) FILTER (WHERE state='rejected' AND decided_at >= %s) AS drafts_rejected,
            count(*) FILTER (WHERE state='pending') AS drafts_pending
        FROM drafts""",
        (since, since),
    )
    mem = await one(
        conn,
        """SELECT
            count(*) FILTER (WHERE state='saved') AS memory_saves,
            count(*) FILTER (WHERE state='corrected') AS memory_corrections,
            count(*) FILTER (WHERE state='forgotten') AS memory_forgets
        FROM memory_changes WHERE created_at >= %s""",
        (since,),
    )
    board = await one(
        conn,
        """SELECT
            count(*) FILTER (WHERE outcome='completed' AND action IS DISTINCT FROM 'noop') AS board_actions,
            count(*) FILTER (WHERE outcome IN ('failed','error')) AS board_errors
        FROM board_coordinator_runs WHERE created_at >= %s""",
        (since,),
    )
    run_err = await one(
        conn,
        """SELECT count(*) AS run_errors
        FROM runs WHERE created_at >= %s AND status IN ('failed', 'timeout')""",
        (since,),
    )
    median = ask["median_answer_min"] if ask else None
    return {
        "team_channel_turns": ev["team_channel_turns"] if ev else 0,
        "dm_turns": ev["dm_turns"] if ev else 0,
        "team_briefs_posted": ev["team_briefs_posted"] if ev else 0,
        "asks_opened": ask["asks_opened"] if ask else 0,
        "asks_answered": ask["asks_answered"] if ask else 0,
        "asks_open_over_24h": ask["asks_open_over_24h"] if ask else 0,
        "median_ask_answer_minutes": round(float(median), 1) if median is not None else None,
        "drafts_approved": draft["drafts_approved"] if draft else 0,
        "drafts_rejected": draft["drafts_rejected"] if draft else 0,
        "drafts_pending": draft["drafts_pending"] if draft else 0,
        "memory_saves": mem["memory_saves"] if mem else 0,
        "memory_corrections": mem["memory_corrections"] if mem else 0,
        "memory_forgets": mem["memory_forgets"] if mem else 0,
        "board_actions": board["board_actions"] if board else 0,
        "board_errors": board["board_errors"] if board else 0,
        "run_errors": run_err["run_errors"] if run_err else 0,
    }


async def tool_denials(conn, since, *, limit: int = 15):
    """Summarize recent tool denials and refusals without private arguments."""
    rows = await all_rows(
        conn,
        """SELECT tool, left(COALESCE(reason, ''), 200) AS reason, count(*) AS count
        FROM audit
        WHERE decision='deny' AND created_at >= %s
        GROUP BY tool, left(COALESCE(reason, ''), 200)
        ORDER BY count DESC, tool
        LIMIT %s""",
        (since, limit),
    )
    return [{"tool": r["tool"], "reason": r["reason"], "count": r["count"]} for r in rows]


async def team_memory_changes(conn, workspace, since, *, limit: int = 20):
    """Read recent team memory saves, corrections, and forgets (excluding private lists)."""
    private_lists = {entry.key for entry in workspace.lists if entry.owner}
    rows = await all_rows(
        conn,
        """SELECT created_at, kind, target, title, state
        FROM memory_changes
        WHERE created_at >= %s
        ORDER BY id DESC LIMIT %s""",
        (since, limit * 2),
    )
    items = []
    for row in rows:
        if row["kind"] == "list" and row["target"] in private_lists:
            continue
        items.append(
            {
                "at": row["created_at"].isoformat(),
                "kind": row["kind"],
                "target": row["target"],
                "title": row["title"],
                "state": row["state"],
            }
        )
        if len(items) >= limit:
            break
    return items


def is_rule_protected(rule) -> bool:
    return bool(PROTECTED_RULE_PROFILES & set(rule.profiles)) or "outsider" in set(rule.principals)


def workspace_file_index(workspace):
    """List workspace notes, rules, routines, and skills along with editability."""
    writable = set(workspace.config.dream.writable)
    entries = []
    for note in workspace.notes:
        entries.append(
            {
                "path": note.path,
                "folder": "notes",
                "title": note.title,
                "about": note.about,
                "chars": len(note.body),
                "editable": "notes" in writable,
            }
        )
    for rule in workspace.rules:
        protected = is_rule_protected(rule)
        entries.append(
            {
                "path": rule.path,
                "folder": "rules",
                "name": rule.name,
                "description": rule.description,
                "profiles": list(rule.profiles),
                "chars": len(rule.body),
                "editable": "rules" in writable and not protected,
            }
        )
    routines_dir = workspace.root / "routines"
    if routines_dir.is_dir():
        for path in sorted(routines_dir.glob("*.md")):
            if path.is_symlink():
                continue
            rel = str(path.relative_to(workspace.root))
            entries.append(
                {
                    "path": rel,
                    "folder": "routines",
                    "name": path.stem,
                    "chars": path.stat().st_size,
                    "editable": "routines" in writable and path.name not in PROTECTED_ROUTINES,
                }
            )
    for skill in workspace.skills:
        entries.append(
            {
                "path": skill.path,
                "folder": "skills",
                "name": skill.name,
                "description": skill.description,
                "chars": len(skill.body),
                "editable": "skills" in writable,
            }
        )
    return entries


async def build(conn, workspace, now):
    """Build the full untainted reflection snapshot for a dream run."""
    since = await dream_window(conn, now)
    return {
        "since": since.isoformat(),
        "now": now.isoformat(),
        "max_changes": workspace.config.dream.max_changes,
        "writable": list(workspace.config.dream.writable),
        "metrics": await metrics(conn, since),
        "interactions": await team_interactions(conn, workspace, since),
        "recent_team_briefs": await recent_team_briefs(conn, since),
        "recent_board_threads": await recent_board_threads(conn, since),
        "tool_denials": await tool_denials(conn, since),
        "memory_changes": await team_memory_changes(conn, workspace, since),
        "recent_reflections": await recent_reflections(conn),
        "workspace_files": workspace_file_index(workspace),
    }

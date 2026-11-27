"""Nightly dream reflection tools: read untainted telemetry and silently refine workspace knowledge."""

import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb
from pydantic import Field

from illo import notes
from illo.db import one
from illo.ledger import audit, dream_snapshot
from illo.memory import files as memory_files
from illo.notes import gitstore
from illo.tools._format import data_text, json_text
from illo.tools.registry import Arguments, Refusal, Tool
from illo.workspace import WorkspaceError, load_workspace

NOTE_PATH = re.compile(r"^notes/([a-zA-Z0-9][a-zA-Z0-9_.-]{0,80}\.md)$")
RULE_PATH = re.compile(r"^rules/([a-zA-Z0-9][a-zA-Z0-9_.-]{0,80}\.md)$")
ROUTINE_PATH = re.compile(r"^routines/([a-zA-Z0-9][a-zA-Z0-9_.-]{0,80}\.md)$")
SKILL_PATH = re.compile(r"^skills/([a-z0-9][a-z0-9_-]{0,60})/SKILL\.md$")
LIST_PATH = re.compile(r"^lists/([a-z][a-z0-9_-]{0,80}\.md)$")
CREATABLE_ROUTINES = frozenset({"digest.md", "team-digest.md"})


class DreamReadArgs(Arguments):
    path: str = Field(min_length=1, max_length=200)


class DreamWriteArgs(Arguments):
    path: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=20000)
    reason: str = Field(min_length=3, max_length=200)


class DreamDeleteNoteArgs(Arguments):
    path: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=3, max_length=200)


class DreamReflectionArgs(Arguments):
    summary: str = Field(min_length=10, max_length=2000)
    scores: dict[str, Any] = Field(default_factory=dict)


class DreamLogArgs(Arguments):
    limit: int = Field(default=5, ge=1, le=10)


def _require_dream(ctx):
    run = ctx.run
    if run.get("deep_kind") != "dream" or run.get("tainted") or run.get("principal") != "team":
        raise Refusal("This tool is only available inside an untainted dream run.")
    if not run.get("deep_run_id"):
        raise Refusal("Dream run context is missing.")


def _parse_path(rel_path: str, *, allow_lists: bool = False) -> tuple[str, str, str]:
    """Return (top_folder, validate_folder, file_name) or raise Refusal."""
    if "\\" in rel_path or ".." in rel_path or rel_path.startswith("/"):
        raise Refusal("Use a clean relative workspace path.")
    if m := NOTE_PATH.fullmatch(rel_path):
        return "notes", "notes", m.group(1)
    if m := RULE_PATH.fullmatch(rel_path):
        return "rules", "rules", m.group(1)
    if m := ROUTINE_PATH.fullmatch(rel_path):
        return "routines", "routines", m.group(1)
    if m := SKILL_PATH.fullmatch(rel_path):
        return "skills", f"skills/{m.group(1)}", "SKILL.md"
    if allow_lists and (m := LIST_PATH.fullmatch(rel_path)):
        return "lists", "lists", m.group(1)
    raise Refusal(
        "Use a valid path under notes/<name>.md, rules/<name>.md, "
        "routines/<name>.md, or skills/<name>/SKILL.md."
    )


def _check_no_symlinks(root: Path, folder: str, file_name: str) -> Path:
    current = root
    for part in Path(folder).parts:
        current = current / part
        if current.is_symlink():
            raise Refusal("Workspace links are not writable.")
    target = current / file_name
    if target.is_symlink():
        raise Refusal("Workspace links are not writable.")
    if target.resolve().parent != current.resolve():
        raise Refusal("The workspace path is invalid.")
    return target


def _write_atomic(root: Path, folder: str, file_name: str, text: str) -> Path:
    target = _check_no_symlinks(root, folder, file_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".illo-dream-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return target


async def _dream_context(conn, deep_run_id: int) -> dict:
    row = await one(conn, "SELECT context FROM deep_runs WHERE id=%s", (deep_run_id,))
    return dict(row["context"] or {}) if row else {}


async def _record_dream_change(conn, deep_run_id: int, entry: dict) -> list[dict]:
    row = await one(conn, "SELECT context FROM deep_runs WHERE id=%s FOR UPDATE", (deep_run_id,))
    context = dict(row["context"] or {}) if row else {}
    changes = list(context.get("dream_changes") or [])
    changes.append(entry)
    context["dream_changes"] = changes
    await conn.execute("UPDATE deep_runs SET context=%s WHERE id=%s", (Jsonb(context), deep_run_id))
    return changes


async def _check_change_budget(ctx) -> None:
    deep_run_id = ctx.run["deep_run_id"]
    context = await _dream_context(ctx.conn, deep_run_id)
    changes = context.get("dream_changes") or []
    limit = ctx.app.workspace.config.dream.max_changes
    if len(changes) >= limit:
        raise Refusal(f"This dream reached its limit of {limit} workspace changes.")


async def snapshot_read(ctx, args):
    _require_dream(ctx)
    deep_run_id = ctx.run["deep_run_id"]
    context = await _dream_context(ctx.conn, deep_run_id)
    if "dream_snapshot" in context:
        data = context["dream_snapshot"]
    else:
        data = await dream_snapshot.build(ctx.conn, ctx.app.workspace, datetime.now(UTC))
    return data_text("dream-snapshot", json_text(data))


async def read_file(ctx, args):
    _require_dream(ctx)
    top, folder, name = _parse_path(args["path"], allow_lists=True)
    ws = ctx.app.workspace
    if top == "lists":
        stem = Path(name).stem
        entry = next((e for e in ws.lists if e.key == stem), None)
        if entry and entry.owner:
            raise Refusal("Private lists are not readable during a team dream.")
    target = _check_no_symlinks(ws.root, folder, name)
    if not target.is_file():
        raise Refusal(f"File not found: {args['path']}")
    text = target.read_text(encoding="utf-8")
    return data_text("workspace-file", json_text({"path": args["path"].strip(), "content": text}))


def _enforce_write_policy(workspace, rel_path: str, top: str, name: str, content: str) -> None:
    if top not in set(workspace.config.dream.writable):
        raise Refusal(f"Dream is not configured to write to {top}/.")
    if top == "routines":
        if name in dream_snapshot.PROTECTED_ROUTINES:
            raise Refusal("Dream cannot modify its own routine file.")
        target = workspace.root / "routines" / name
        if not target.exists() and name not in CREATABLE_ROUTINES:
            raise Refusal("Dream can only update existing routines or digest instructions.")
    if top == "rules":
        existing = next((r for r in workspace.rules if r.path == rel_path), None)
        if existing is None:
            raise Refusal("Dream can refine existing team rules, not create new rule files.")
        if dream_snapshot.is_rule_protected(existing):
            raise Refusal("Dream cannot modify rules scoped to tainted or outsider runs.")
        try:
            front, _ = memory_files.split(content)
        except Exception:
            raise Refusal("Rule files must keep valid YAML front matter.") from None
        if not isinstance(front, dict):
            raise Refusal("Rule files must keep valid YAML front matter.")
        for field_name in ("channels", "profiles", "principals"):
            new_val = list(front.get(field_name) or [])
            old_val = list(getattr(existing, field_name) or [])
            if new_val != old_val:
                raise Refusal(f"Dream cannot change rule scope field `{field_name}`.")
        if front.get("name") != existing.name:
            raise Refusal("Dream cannot rename an existing rule.")


async def write_file(ctx, args):
    _require_dream(ctx)
    await _check_change_budget(ctx)
    rel_path = args["path"].strip()
    content = args["content"]
    reason = args["reason"].strip()
    top, folder, name = _parse_path(rel_path, allow_lists=False)
    ws = ctx.app.workspace
    _enforce_write_policy(ws, rel_path, top, name, content)
    _check_no_symlinks(ws.root, folder, name)
    try:
        memory_files.validate(ws.root, folder, name, content)
    except (WorkspaceError, ValueError) as exc:
        raise Refusal(str(exc)) from None
    path = _write_atomic(ws.root, folder, name, content)
    ctx.app.workspace = load_workspace(ws.root)
    if top == "notes":
        await notes.sync(ctx.app, ctx.conn)
    committed = await gitstore.store(
        ctx.app, ctx.conn, path, rel_path, commit=True, message=f"dream: {reason}"
    )
    entry = {"action": "write", "path": rel_path, "reason": reason}
    changes = await _record_dream_change(ctx.conn, ctx.run["deep_run_id"], entry)
    await audit.write(
        ctx.conn,
        principal="team",
        tool="dream_write_file",
        args={"path": rel_path, "reason": reason},
        decision="allow",
        reason="Dream updated workspace file.",
        run_id=ctx.run["id"],
        target=f"workspace:{rel_path}",
    )
    return {
        "saved": rel_path,
        "committed": committed,
        "changes_used": len(changes),
        "max_changes": ctx.app.workspace.config.dream.max_changes,
    }


async def delete_note(ctx, args):
    _require_dream(ctx)
    await _check_change_budget(ctx)
    rel_path = args["path"].strip()
    reason = args["reason"].strip()
    top, folder, name = _parse_path(rel_path, allow_lists=False)
    if top != "notes":
        raise Refusal("Dream can only delete files under notes/.")
    ws = ctx.app.workspace
    if "notes" not in set(ws.config.dream.writable):
        raise Refusal("Dream is not configured to modify notes/.")
    target = _check_no_symlinks(ws.root, folder, name)
    if not target.is_file():
        raise Refusal(f"Note not found: {rel_path}")
    try:
        path = memory_files.replace(ws.root, "notes", name, None)
    except (WorkspaceError, ValueError) as exc:
        raise Refusal(str(exc)) from None
    ctx.app.workspace = load_workspace(ws.root)
    await notes.sync(ctx.app, ctx.conn)
    committed = await gitstore.store(
        ctx.app, ctx.conn, path, rel_path, commit=True, message=f"dream: delete {rel_path} — {reason}"
    )
    entry = {"action": "delete", "path": rel_path, "reason": reason}
    changes = await _record_dream_change(ctx.conn, ctx.run["deep_run_id"], entry)
    await audit.write(
        ctx.conn,
        principal="team",
        tool="dream_delete_note",
        args={"path": rel_path, "reason": reason},
        decision="allow",
        reason="Dream pruned stale note.",
        run_id=ctx.run["id"],
        target=f"workspace:{rel_path}",
    )
    return {
        "deleted": rel_path,
        "committed": committed,
        "changes_used": len(changes),
        "max_changes": ctx.app.workspace.config.dream.max_changes,
    }


async def record_reflection(ctx, args):
    _require_dream(ctx)
    deep_run_id = ctx.run["deep_run_id"]
    context = await _dream_context(ctx.conn, deep_run_id)
    snap = context.get("dream_snapshot") or {}
    now = datetime.now(UTC)
    window_start = (
        datetime.fromisoformat(snap["since"])
        if snap.get("since")
        else await dream_snapshot.dream_window(ctx.conn, now)
    )
    window_end = datetime.fromisoformat(snap["now"]) if snap.get("now") else now
    changes = list(context.get("dream_changes") or [])
    row = await one(
        ctx.conn,
        """INSERT INTO dream_reflections (deep_run_id, window_start, window_end, summary, scores, changes)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (deep_run_id) DO UPDATE
        SET summary=EXCLUDED.summary, scores=EXCLUDED.scores, changes=EXCLUDED.changes
        RETURNING id""",
        (
            deep_run_id,
            window_start,
            window_end,
            args["summary"].strip(),
            Jsonb(args.get("scores") or {}),
            Jsonb(changes),
        ),
    )
    return {"reflection_id": row["id"], "changes_recorded": len(changes)}


async def read_dream_log(ctx, args):
    rows = await dream_snapshot.recent_reflections(ctx.conn, limit=args["limit"])
    return data_text("dream-reflections", json_text(rows))


def register(registry):
    registry.add(
        Tool(
            "dream_snapshot",
            "dream",
            "read",
            lambda _: "ledger",
            False,
            snapshot_read,
            "Read the untainted reflection snapshot for this dream: metrics, team interactions, "
            "team briefs, board threads, tool denials, memory changes, recent reflections, and workspace files.",
            Arguments,
            profiles=frozenset(),
        )
    )
    registry.add(
        Tool(
            "dream_read_file",
            "dream",
            "read",
            lambda _: "ledger",
            False,
            read_file,
            "Read a workspace file (notes/*.md, rules/*.md, routines/*.md, skills/*/SKILL.md, or team lists/*.md).",
            DreamReadArgs,
            profiles=frozenset(),
        )
    )
    registry.add(
        Tool(
            "dream_write_file",
            "dream",
            "write_internal",
            lambda _: "ledger",
            False,
            write_file,
            "Create or update a workspace file (notes/*.md, existing non-security rules/*.md, "
            "existing/digest routines/*.md except dream.md, or skills/*/SKILL.md). "
            "Validates the full workspace and commits to git automatically.",
            DreamWriteArgs,
            profiles=frozenset(),
            team_visible=True,
        )
    )
    registry.add(
        Tool(
            "dream_delete_note",
            "dream",
            "write_internal",
            lambda _: "ledger",
            False,
            delete_note,
            "Delete a stale, duplicate, or superseded note under notes/*.md and commit to git.",
            DreamDeleteNoteArgs,
            profiles=frozenset(),
            team_visible=True,
        )
    )
    registry.add(
        Tool(
            "dream_record_reflection",
            "dream",
            "write_internal",
            lambda _: "ledger",
            False,
            record_reflection,
            "Save tonight's reflection summary and Chief-of-Staff rubric scores to the reflection log.",
            DreamReflectionArgs,
            profiles=frozenset(),
        )
    )
    registry.add(
        Tool(
            "dream_log",
            "dream",
            "read",
            lambda _: "ledger",
            False,
            read_dream_log,
            "Read Illo's recent nightly dream reflections, scores, and autonomous workspace improvements.",
            DreamLogArgs,
            profiles=frozenset({"person_dm", "team_channel"}),
        )
    )

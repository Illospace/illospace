#!/usr/bin/env python3
"""Illo Brain — Agent Coordination Layer.

Provides shared awareness between concurrent child agents to prevent:
- Git branch conflicts
- Duplicate work
- Resource contention (files, APIs)

The system is advisory, not enforced — awareness is enough.

Usage:
    agent_coordination.py register --session-key "abc" --task "fix bug" --files "a.py,b.py" --branch "fix/bug"
    agent_coordination.py active
    agent_coordination.py conflicts --files "a.py" --branch "fix/bug"
    agent_coordination.py release --session-key "abc"
    agent_coordination.py context
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))  # repo root

from sqlalchemy import text

from brain.platform.db.repositories.unit_of_work import UnitOfWork

# ============================================================
# Core functions
# ============================================================

async def register_agent(
    session_key: str,
    task_description: str,
    files_touched: list[str] | None = None,
    git_branch: str | None = None,
    resources_locked: list[str] | None = None,
) -> int:
    """Register a child agent and its claimed resources.

    Returns the agent_coordination row id.
    """
    async with UnitOfWork() as uow:
        result = await uow.session.execute(text("""
            INSERT INTO agent_coordination (
                session_key, task_description, files_touched,
                git_branch, resources_locked, status, started_at
            ) VALUES (:session_key, :task, :files, :branch, :resources, 'running', NOW())
            ON CONFLICT (session_key) WHERE status = 'running'
            DO UPDATE SET
                task_description = EXCLUDED.task_description,
                files_touched = EXCLUDED.files_touched,
                git_branch = EXCLUDED.git_branch,
                resources_locked = EXCLUDED.resources_locked,
                started_at = NOW()
            RETURNING id
        """), {
            "session_key": session_key,
            "task": task_description,
            "files": files_touched or [],
            "branch": git_branch,
            "resources": resources_locked or [],
        })
        row = result.mappings().first()
        return row["id"]


async def get_active_agents(exclude_session: str | None = None) -> list[dict]:
    """Get all currently running agents and their resources.

    Agents older than 2 hours are auto-expired (stale detection).
    """
    async with UnitOfWork() as uow:
        # Auto-expire stale agents (running > 2 hours)
        await uow.session.execute(text("""
            UPDATE agent_coordination
            SET status = 'expired'
            WHERE status = 'running'
              AND started_at < NOW() - INTERVAL '2 hours'
        """))

        if exclude_session:
            result = await uow.session.execute(text("""
                SELECT id, session_key, task_description, files_touched,
                       git_branch, resources_locked, status, started_at
                FROM agent_coordination
                WHERE status = 'running' AND session_key != :exclude
                ORDER BY started_at DESC
            """), {"exclude": exclude_session})
        else:
            result = await uow.session.execute(text("""
                SELECT id, session_key, task_description, files_touched,
                       git_branch, resources_locked, status, started_at
                FROM agent_coordination
                WHERE status = 'running'
                ORDER BY started_at DESC
            """))
        rows = result.mappings().all()
        return [dict(r) for r in rows]


async def release_agent(session_key: str, status: str = "done") -> bool:
    """Release an agent's resource claims. Returns True if found."""
    if status not in ("done", "failed"):
        raise ValueError(f"Invalid status: {status}. Use 'done' or 'failed'.")
    async with UnitOfWork() as uow:
        result = await uow.session.execute(text("""
            UPDATE agent_coordination
            SET status = :status, completed_at = NOW()
            WHERE session_key = :session_key AND status = 'running'
            RETURNING id
        """), {"status": status, "session_key": session_key})
        row = result.mappings().first()
        return row is not None


async def check_conflicts(
    files: list[str] | None = None,
    branch: str | None = None,
    exclude_session: str | None = None,
) -> list[dict]:
    """Check for resource conflicts with active agents.

    Returns list of conflicting agents with details about what conflicts.
    """
    active = await get_active_agents(exclude_session=exclude_session)
    conflicts = []

    for agent in active:
        conflict_details = []

        # Check file overlaps
        if files and agent["files_touched"]:
            overlapping = set(files) & set(agent["files_touched"])
            if overlapping:
                conflict_details.append({
                    "type": "file",
                    "resources": list(overlapping),
                })

        # Check branch conflicts
        if branch and agent["git_branch"] and branch == agent["git_branch"]:
            conflict_details.append({
                "type": "branch",
                "resources": [branch],
            })

        # Check resource locks
        if files and agent["resources_locked"]:
            file_resources = {f"file:{f}" for f in files}
            locked = set(agent["resources_locked"])
            overlapping_locks = file_resources & locked
            if overlapping_locks:
                conflict_details.append({
                    "type": "resource_lock",
                    "resources": list(overlapping_locks),
                })

        if conflict_details:
            conflicts.append({
                "agent_session": agent["session_key"],
                "agent_task": agent["task_description"],
                "agent_branch": agent["git_branch"],
                "conflicts": conflict_details,
            })

    return conflicts


async def build_awareness_context(exclude_session: str | None = None) -> str:
    """Build a formatted context string for injection into child agent prompts.

    Returns empty string if no other agents are active.
    """
    active = await get_active_agents(exclude_session=exclude_session)
    if not active:
        return ""

    lines = ["## Active Agent Awareness", ""]
    lines.append(f"There are {len(active)} other agent(s) currently working:")
    lines.append("")

    all_locked_files = set()
    all_locked_branches = set()

    for agent in active:
        started = agent["started_at"].strftime("%H:%M") if agent["started_at"] else "?"
        lines.append(f"- **{agent['session_key'][:20]}...** (since {started})")
        lines.append(f"  Task: {agent['task_description'][:120]}")

        if agent["files_touched"]:
            files_str = ", ".join(agent["files_touched"][:10])
            lines.append(f"  Files: {files_str}")
            all_locked_files.update(agent["files_touched"])

        if agent["git_branch"]:
            lines.append(f"  Branch: {agent['git_branch']}")
            all_locked_branches.add(agent["git_branch"])

        if agent["resources_locked"]:
            lines.append(f"  Locks: {', '.join(agent['resources_locked'][:5])}")

        lines.append("")

    if all_locked_files or all_locked_branches:
        lines.append("### Conflict Avoidance")
        if all_locked_files:
            lines.append(f"- **Do not modify:** {', '.join(sorted(all_locked_files))}")
        if all_locked_branches:
            lines.append(f"- **Branches in use:** {', '.join(sorted(all_locked_branches))}")
        lines.append("")

    return "\n".join(lines)


# ============================================================
# CLI
# ============================================================

async def cmd_register(args):
    files = [f.strip() for f in args.files.split(",")] if args.files else None
    resources = [r.strip() for r in args.resources.split(",")] if args.resources else None
    row_id = await register_agent(args.session_key, args.task, files, args.branch, resources)
    print(json.dumps({"id": row_id, "session_key": args.session_key, "status": "registered"}))


async def cmd_active(args):
    agents = await get_active_agents()
    print(json.dumps(agents, indent=2, default=str))


async def cmd_conflicts(args):
    files = [f.strip() for f in args.files.split(",")] if args.files else None
    conflicts = await check_conflicts(files, args.branch, args.exclude)
    print(json.dumps(conflicts, indent=2, default=str))


async def cmd_release(args):
    released = await release_agent(args.session_key, args.status or "done")
    print(json.dumps({"session_key": args.session_key, "released": released}))


async def cmd_context(args):
    ctx = await build_awareness_context(args.exclude)
    if ctx:
        print(ctx)
    else:
        print("No active agents.")


def main():
    parser = argparse.ArgumentParser(description="Illo Brain — Agent Coordination")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("register")
    p.add_argument("--session-key", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--files", default=None)
    p.add_argument("--branch", default=None)
    p.add_argument("--resources", default=None)

    p = sub.add_parser("active")

    p = sub.add_parser("conflicts")
    p.add_argument("--files", default=None)
    p.add_argument("--branch", default=None)
    p.add_argument("--exclude", default=None)

    p = sub.add_parser("release")
    p.add_argument("--session-key", required=True)
    p.add_argument("--status", default="done", choices=["done", "failed"])

    p = sub.add_parser("context")
    p.add_argument("--exclude", default=None)

    args = parser.parse_args()
    command = {"register": cmd_register, "active": cmd_active, "conflicts": cmd_conflicts,
               "release": cmd_release, "context": cmd_context}[args.command]
    asyncio.run(command(args))


if __name__ == "__main__":
    main()

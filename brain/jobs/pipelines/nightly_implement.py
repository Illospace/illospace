#!/usr/bin/env python3
"""Nightly self-improvement proposal review.

Reads improvement memories and PENDING_REFLECTION.json, classifies proposals,
and records processing state. Direct write and PR automation are intentionally
disabled here.

Usage:
    python3 -m brain.jobs.pipelines.nightly_implement [--date 2026-03-04] [--dry-run]
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from subprocess import TimeoutExpired

from sqlalchemy import text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))
import brain.kernel.config as config
from brain.platform.async_io import (
    ensure_dir,
    path_exists,
    read_text as read_text_async,
    rename_path,
    run_blocking,
    run_subprocess,
    write_text as write_text_async,
)
from brain.platform.db.repositories.unit_of_work import UnitOfWork

PROJECT_ROOT = str(config.BRAIN_DIR)
REPO = os.environ.get("ILLO_GITHUB_REPO", "").strip()
LOG_DIR = str(config.BRAIN_LOG_DIR)
PENDING_PATH = str(config.PRIVATE_HOME / "PENDING_REFLECTION.json")
PROCESSING_LOG = os.path.join(LOG_DIR, "implement_processing_log.json")


def _log(msg: str, log_lines: list):
    """Print and collect log line."""
    print(msg)
    log_lines.append(f"[{datetime.now().isoformat()}] {msg}")


async def _run(cmd: str | list, cwd: str | None = None, timeout: int = 60) -> tuple[bool, str]:
    """Run a command, return (success, output).

    Accepts a list (safe) or a string. Strings with shell metacharacters
    are run via ``bash -c``; simple strings are split with shlex.
    """
    import shlex
    try:
        if isinstance(cmd, list):
            args = cmd
        elif any(ch in cmd for ch in ('&&', '|', ';', '>', '<')):
            args = ["bash", "-c", cmd]
        else:
            args = shlex.split(cmd)
        r = await run_subprocess(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or PROJECT_ROOT,
        )
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except TimeoutExpired:
        return False, f"TIMEOUT after {timeout}s"


def _is_safe_path(filepath: str) -> bool:
    """Validate that a file path is within the source project root."""
    resolved = os.path.realpath(os.path.join(PROJECT_ROOT, filepath))
    return resolved.startswith(os.path.realpath(PROJECT_ROOT))


async def _get_processed_ids() -> set:
    """Load set of already-processed memory IDs."""
    if await path_exists(PROCESSING_LOG):
        try:
            data = json.loads(await read_text_async(PROCESSING_LOG))
            return set(data.get("processed_memory_ids", []))
        except (json.JSONDecodeError, KeyError):
            pass
    return set()


async def _save_processed_ids(ids: set):
    """Save processed memory IDs."""
    await ensure_dir(LOG_DIR)
    existing = {}
    if await path_exists(PROCESSING_LOG):
        try:
            existing = json.loads(await read_text_async(PROCESSING_LOG))
        except (json.JSONDecodeError, KeyError):
            existing = {}
    existing["processed_memory_ids"] = sorted(ids)
    existing["last_updated"] = datetime.now().isoformat()
    await write_text_async(PROCESSING_LOG, json.dumps(existing, indent=2))


async def gather_improvement_memories(target_date: date, processed_ids: set) -> list[dict]:
    """Query unprocessed improvement memories."""
    async with UnitOfWork() as uow:
        result = await uow.session.execute(text("""
            SELECT id, content, salience, tags, created_at
            FROM memories
            WHERE memory_type = 'improvement'
              AND NOT archived
              AND id NOT IN (SELECT unnest(CAST(:processed_ids AS int[])))
            ORDER BY salience DESC, created_at DESC
            LIMIT 20
        """), {"processed_ids": list(processed_ids) or [0]})
        return [dict(r) for r in result.mappings().all()]


async def load_pending_reflection() -> list[dict]:
    """Load proposals from PENDING_REFLECTION.json."""
    if not await path_exists(PENDING_PATH):
        return []
    try:
        data = json.loads(await read_text_async(PENDING_PATH))
        # Could be a single dict or a list
        if isinstance(data, dict):
            return [data] if data else []
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def classify_proposal(content: str) -> dict | None:
    """Parse a proposal/improvement memory into an actionable item.

    Returns dict with keys: action, target_file, description, content_patch
    or None if not actionable.

    Supports formats:
    - "FILE: path/to/file ACTION: append/replace/create CONTENT: ..."
    - "Add pitfall to skill X: ..."
    - "Update config: ..."
    - Free-form (logged but skipped for auto-apply)
    """
    lines = content.strip().split("\n")
    first = lines[0].strip()

    # Structured format: FILE: ... ACTION: ... CONTENT: ...
    if "FILE:" in content.upper() and "ACTION:" in content.upper():
        parts = {}
        current_key = None
        for line in lines:
            upper = line.strip().upper()
            for key in ("FILE:", "ACTION:", "CONTENT:"):
                if upper.startswith(key):
                    current_key = key.rstrip(":").lower()
                    parts[current_key] = line.strip()[len(key):].strip()
                    break
            else:
                if current_key == "content":
                    parts["content"] = parts.get("content", "") + "\n" + line

        target = parts.get("file", "")
        if not target or not _is_safe_path(target):
            return None
        return {
            "action": parts.get("action", "append").lower(),
            "target_file": target,
            "description": first,
            "content_patch": parts.get("content", ""),
        }

    # DB-only proposals (skill updates, config changes) — log but don't auto-apply files
    return {
        "action": "log_only",
        "target_file": None,
        "description": first[:200],
        "content_patch": content,
    }


def apply_proposal(proposal: dict, dry_run: bool, log_lines: list) -> bool:
    """Compatibility shim for the removed direct-write lane.

    Nightly implementation now only reviews proposals. It never mutates files
    directly, even when a proposal contains a safe path.
    """
    action = str(proposal.get("action") or "").strip().lower()
    target_file = proposal.get("target_file")
    if action == "log_only":
        _log("  ⏸️ Proposal logged; direct write lane is disabled", log_lines)
        return True
    if target_file and not _is_safe_path(str(target_file)):
        _log("  ❌ Unsafe proposal path rejected", log_lines)
        return False
    _log("  ⏸️ Direct write lane disabled; proposal reviewed only", log_lines)
    return False


def mirror_implement_proposal(proposal: dict, *, source: str | None = None):
    """Compatibility no-op for the removed proposal mirroring lane."""
    return None, None


async def run_tests(log_lines: list) -> bool:
    """Run the test suite. Returns True if all pass."""
    _log("  🧪 Running tests...", log_lines)
    ok, output = await _run(
        "source venv/bin/activate && python3 -m pytest tests/ -v --tb=short -q",
        timeout=120,
    )
    # Log just the summary
    summary_lines = [l for l in output.split("\n") if l.strip()][-5:]
    for line in summary_lines:
        _log(f"    {line}", log_lines)
    return ok


async def fetch_nightly_issues(log_lines: list) -> list[dict]:
    """Fetch configured GitHub issues labeled 'nightly'."""
    if not REPO:
        _log("  ⚠️ ILLO_GITHUB_REPO not configured; skipping nightly issue fetch", log_lines)
        return []
    ok, out = await _run(
        f'gh issue list --repo {REPO} --label nightly --state open '
        f'--json number,title,body --limit 10',
        timeout=30,
    )
    if not ok:
        _log(f"  ⚠️ Failed to fetch nightly issues: {out}", log_lines)
        return []
    try:
        issues = json.loads(out)
        _log(f"  📋 Found {len(issues)} nightly issue(s)", log_lines)
        return issues
    except json.JSONDecodeError:
        _log(f"  ⚠️ Could not parse issues JSON: {out[:100]}", log_lines)
        return []


async def _async_main(args) -> None:
    target_date = date.fromisoformat(args.date) if args.date else date.today()
    dry_run = args.dry_run
    log_lines = []

    _log(f"{'='*60}", log_lines)
    _log(f"NIGHTLY SELF-IMPROVEMENT — {target_date} {'[DRY RUN]' if dry_run else ''}", log_lines)
    _log(f"{'='*60}", log_lines)
    _log("Direct writes and nightly PR automation are disabled; proposal mirroring is removed.", log_lines)

    # Gather items to process
    processed_ids = await _get_processed_ids()
    improvements = await gather_improvement_memories(target_date, processed_ids)
    pending = await load_pending_reflection()

    total = len(improvements) + len(pending)
    if total == 0:
        _log("No improvement items to process.", log_lines)
        await _write_log(target_date, log_lines)
        return

    _log(f"Found {len(improvements)} improvement memories, {len(pending)} pending proposals", log_lines)

    preview_count = 0
    skipped_count = 0
    new_processed_ids = set(processed_ids)

    # Process improvement memories
    for mem in improvements:
        _log(f"\n📌 Memory #{mem['id']}: {mem['content'][:80]}...", log_lines)
        proposal = classify_proposal(mem["content"])
        if proposal is None:
            _log("  ⚠️ Could not parse into actionable proposal, skipping", log_lines)
            new_processed_ids.add(mem["id"])
            skipped_count += 1
            continue

        if dry_run:
            _log("  🔍 [DRY RUN] Would mark proposal reviewed; execution lane is disabled", log_lines)
            preview_count += 1
        else:
            _log("  ⏸️ Proposal reviewed; execution lane is disabled", log_lines)
            skipped_count += 1
        new_processed_ids.add(mem["id"])

    # Process pending reflection proposals
    for i, prop in enumerate(pending):
        desc = prop.get("proposal", prop.get("description", str(prop)))[:80]
        _log(f"\n📌 Pending proposal {i+1}: {desc}...", log_lines)

        # Pending reflection can contain system proposals
        if isinstance(prop, dict) and "prompt_path" in prop:
            _log("  ℹ️ This is a deferred LLM reflection — needs main agent processing", log_lines)
            continue

        content = prop.get("proposal", prop.get("change", json.dumps(prop)))
        proposal = classify_proposal(content)
        if proposal:
            mirror_implement_proposal(proposal, source="pending_reflection")
            if dry_run:
                _log("  🔍 [DRY RUN] Would mark pending proposal reviewed; execution lane is disabled", log_lines)
                preview_count += 1
            else:
                _log("  ⏸️ Pending proposal reviewed; execution lane is disabled", log_lines)
                skipped_count += 1

    # Process GitHub issues labeled 'nightly'
    _log("\n--- GITHUB ISSUES (label: nightly) ---", log_lines)
    nightly_issues = await fetch_nightly_issues(log_lines)
    for issue in nightly_issues:
        issue_num = issue["number"]
        issue_title = issue["title"]
        issue_body = issue.get("body", "")
        _log(f"\n📌 Issue #{issue_num}: {issue_title}", log_lines)

        # Classify the issue body as a proposal
        proposal = classify_proposal(issue_body) if issue_body else None
        if not proposal:
            _log(f"  ℹ️ Issue #{issue_num} could not be classified; skipping", log_lines)
            continue

        if dry_run:
            _log("  🔍 [DRY RUN] Would mark issue proposal reviewed; execution lane is disabled", log_lines)
            preview_count += 1
        else:
            _log("  ⏸️ Issue proposal reviewed; execution lane is disabled", log_lines)
            skipped_count += 1

    # Save processed IDs
    if not dry_run:
        await _save_processed_ids(new_processed_ids)

    # Clean up PENDING_REFLECTION.json if we processed it
    if pending and not dry_run and await path_exists(PENDING_PATH):
        await rename_path(PENDING_PATH, PENDING_PATH + f".done-{target_date}")

    _log(f"\n{'='*60}", log_lines)
    _log(
        f"Complete. Previewed: {preview_count}, Reviewed/Skipped: {skipped_count}",
        log_lines,
    )
    _log(f"{'='*60}", log_lines)

    await _write_log(target_date, log_lines)


def main():
    parser = argparse.ArgumentParser(description="Nightly self-improvement")
    parser.add_argument("--date", help="Target date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    args = parser.parse_args()
    asyncio.run(_async_main(args))


async def _write_log(target_date: date, log_lines: list):
    """Append to nightly log."""
    await ensure_dir(LOG_DIR)
    log_path = os.path.join(LOG_DIR, f"nightly-{target_date}.log")
    await run_blocking(_append_log, log_path, log_lines)


def _append_log(log_path: str, log_lines: list) -> None:
    with open(log_path, "a") as f:
        f.write("\n--- SELF-IMPROVEMENT ---\n")
        for line in log_lines:
            f.write(line + "\n")


if __name__ == "__main__":
    main()

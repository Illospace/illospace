#!/usr/bin/env python3
"""Detach before network work so a hook never delays an agent."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def lane_from_hook(source: str, payload: dict, repo: str | None = None) -> dict | None:
    from collectors.common import clean

    folder = Path(payload.get("cwd") or ".").name or "Agent session"
    summary = ""
    if source == "claude-code":
        session_id = payload.get("session_id")
        event = payload.get("hook_event_name")
        state = {
            "SessionStart": "working",
            "UserPromptSubmit": "working",
            "Stop": "unknown",
            "SessionEnd": "done",
        }.get(event)
        if event == "Notification":
            if payload.get("notification_type") not in {
                "permission_prompt",
                "idle_prompt",
            }:
                return None
            state = "waiting_person"
            summary = payload.get("message", "")
        if event == "Stop":
            summary = payload.get("last_assistant_message", "")
        title = payload.get("prompt") or folder
    else:
        if payload.get("type") != "agent-turn-complete":
            return None
        session_id = payload.get("thread-id")
        state = "unknown"
        summary = payload.get("last-assistant-message", "")
        title = next(iter(payload.get("input-messages", [])), folder)
    if not session_id or not state:
        return None
    return {
        "source": source,
        "external_id": str(session_id),
        "title": clean(title, 200),
        "state": state,
        "summary": clean(summary, 500),
        "repo": repo,
        "machine": socket.gethostname(),
        "links": [],
    }


def repository(cwd: str) -> str | None:
    """Read only the remote name. Never read files or a session transcript."""
    import re

    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"}
    }
    result = subprocess.run(
        ["git", "-C", cwd, "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        timeout=2,
        env=environment,
        check=False,
    )
    remote = result.stdout.strip()
    match = re.search(r"(?:github\.com[:/])([^/\s]+/[^/\s]+?)(?:\.git)?$", remote)
    return match[1] if match else None


def worker(source: str, payload: dict, config_path: Path) -> None:
    import fcntl
    import time

    from collectors.common import MCPClient, atomic_json, clean, read_token

    config = json.loads(config_path.read_text())
    if source == "codex" and config.get("previous_notify"):
        try:
            subprocess.Popen(
                [*config["previous_notify"], payload["_illo_notify_json"]],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except OSError:
            # A removed notify program must not prevent this bundle's report.
            pass
    token = read_token(Path(config["token_file"]))
    lane = lane_from_hook(source, payload, repository(payload.get("cwd") or "."))
    if lane:
        for key in ("title", "summary"):
            lane[key] = clean(lane[key], 500 if key == "summary" else 200, (token,))
        # Serialize local state so later hooks do not replace a session's first title.
        state_path = config_path.parent / "hook-state.json"
        with state_path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
            key = f"{source}:{lane['external_id']}"
            entry = state.get(key, {})
            if entry.get("at", 0) > payload.get("_illo_received_at", 0):
                return
            lane["title"] = entry.get("title", lane["title"])
            if payload.get("hook_event_name") == "UserPromptSubmit" and not entry.get(
                "prompt_title"
            ):
                lane["title"] = (
                    clean(payload.get("prompt"), 200, (token,)) or lane["title"]
                )
                entry["prompt_title"] = True
            MCPClient(config["mcp_url"], token).call("lane_report", lane)
            state[key] = {
                "title": lane["title"],
                "prompt_title": entry.get("prompt_title", source == "codex"),
                "at": payload.get("_illo_received_at", time.time()),
            }
            atomic_json(state_path, state)


def main() -> int:
    # Catch every failure at the boundary. An optional report must never fail a hook.
    try:
        import time

        parser = argparse.ArgumentParser()
        parser.add_argument("--source", choices=["claude-code", "codex"], required=True)
        parser.add_argument("--config", type=Path, required=True)
        parser.add_argument("--worker", action="store_true")
        parser.add_argument("payload", nargs="?")
        args = parser.parse_args()
        if args.worker:
            worker(args.source, json.load(sys.stdin), args.config)
            return 0
        raw = args.payload if args.payload is not None else sys.stdin.read(1_048_577)
        if len(raw) > 1_048_576:
            return 0
        payload = json.loads(raw)
        # Pass only hook fields. Do not copy transcripts, tool output, or attachments.
        allowed = {
            "cwd",
            "session_id",
            "hook_event_name",
            "notification_type",
            "message",
            "last_assistant_message",
            "prompt",
            "type",
            "thread-id",
            "turn-id",
            "input-messages",
            "last-assistant-message",
        }
        payload = {key: value for key, value in payload.items() if key in allowed}
        for key, value in list(payload.items()):
            if isinstance(value, str):
                payload[key] = value[:2000]
        if "input-messages" in payload:
            payload["input-messages"] = [
                str(value)[:200] for value in payload["input-messages"][:1]
            ]
        if args.source == "codex":
            # The old program gets its original local payload. Illo never gets this field.
            payload["_illo_notify_json"] = raw
        payload["_illo_received_at"] = time.time()
        child = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--source",
                args.source,
                "--config",
                str(args.config),
                "--worker",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        # A worker reads its small payload before it does network work.
        child.stdin.write(json.dumps(payload).encode())
        child.stdin.close()
    except (Exception, SystemExit):  # noqa: BLE001 -- An optional hook must always succeed.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

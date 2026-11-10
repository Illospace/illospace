#!/usr/bin/env python3
"""Install only this bundle's entries. Preserve all other settings."""

from __future__ import annotations

import argparse
import copy
import getpass
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
import tomllib
from pathlib import Path

MARKER = "# illo-collector-notify"
EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "Notification", "SessionEnd")


def merge_claude(settings: dict, command: str, remove: bool = False) -> dict:
    result = copy.deepcopy(settings)
    hooks = result.setdefault("hooks", {})
    for event in EVENTS:
        entries = []
        for entry in hooks.get(event, []):
            kept = [
                hook
                for hook in entry.get("hooks", [])
                if hook.get("command") != command
            ]
            if kept:
                entries.append({**entry, "hooks": kept})
            elif not entry.get("hooks"):
                entries.append(entry)
        if not remove:
            entries.append({"hooks": [{"type": "command", "command": command}]})
        if entries:
            hooks[event] = entries
        else:
            hooks.pop(event, None)
    if not hooks:
        result.pop("hooks", None)
    return result


def notify_span(text: str) -> tuple[int, int] | None:
    """Find one top-level TOML value without rewriting the other settings."""
    import re

    lines = text.splitlines(keepends=True)
    for start, line in enumerate(lines):
        if line.lstrip().startswith("["):
            break
        if re.match(r"\s*notify\s*=", line):
            for end in range(start + 1, len(lines) + 1):
                try:
                    tomllib.loads("".join(lines[start:end]))
                    return start, end
                except tomllib.TOMLDecodeError:
                    continue
            raise ValueError("The notify value is invalid.")
    return None


def merge_codex(
    text: str, command: list[str], remove: bool = False, previous: str = ""
) -> str:
    """Keep an existing notify program in the bundle's local config."""
    parsed = tomllib.loads(text)
    lines = text.splitlines(keepends=True)
    span = notify_span(text)
    owned = span and lines[span[0]].rstrip().endswith(MARKER)
    if remove:
        if not owned:
            return text
        result = "".join(lines[: span[0]]) + previous + "".join(lines[span[1] :])
    else:
        if parsed.get("notify") is not None and not isinstance(parsed["notify"], list):
            raise ValueError("The notify value must be a list.")
        entry = "notify = " + json.dumps(command) + " " + MARKER + "\n"
        result = (
            "".join(lines[: span[0]]) + entry + "".join(lines[span[1] :])
            if span
            else entry + text
        )
    tomllib.loads(result)
    return result


def write(path: Path, text: str, backup: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if backup and path.exists():
        backup_path = path.with_name(path.name + f".illo-{time.time_ns()}.bak")
        shutil.copy2(path, backup_path)
        backup_path.chmod(0o600)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".illo-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--url")
    parser.add_argument("--home", type=Path, default=Path.home())
    args = parser.parse_args()
    home = args.home.expanduser().resolve()
    config = home / ".config/illo/hooks.json"
    claude = (
        Path(os.environ.get("CLAUDE_CONFIG_DIR", str(home / ".claude")))
        / "settings.json"
    )
    codex = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))) / "config.toml"
    script = Path(__file__).resolve().with_name("report.py")
    command = shlex.join(
        [
            sys.executable,
            str(script),
            "--source",
            "claude-code",
            "--config",
            str(config),
        ]
    )
    codex_command = [
        sys.executable,
        str(script),
        "--source",
        "codex",
        "--config",
        str(config),
    ]
    try:
        manifest = home / ".config/illo/hook-install.json"
        installed = json.loads(manifest.read_text()) if manifest.exists() else {}
        if args.uninstall and installed:
            command = installed["claude_command"]
            claude, codex = (
                Path(installed["claude_path"]),
                Path(installed["codex_path"]),
            )
        current = json.loads(claude.read_text()) if claude.exists() else {}
        previous_command_line = installed.get("claude_command")
        if (
            not args.uninstall
            and previous_command_line
            and previous_command_line != command
        ):
            current = merge_claude(current, previous_command_line, remove=True)
        updated = merge_claude(current, command, args.uninstall)
        codex_text = codex.read_text() if codex.exists() else ""
        span = notify_span(codex_text)
        lines = codex_text.splitlines(keepends=True)
        owned = span and lines[span[0]].rstrip().endswith(MARKER)
        previous = (
            installed.get("previous_notify_text", "")
            if owned
            else ("".join(lines[span[0] : span[1]]) if span else "")
        )
        previous_command = (
            installed.get("previous_notify", [])
            if owned
            else tomllib.loads(codex_text).get("notify", [])
        )
        codex_updated = merge_codex(codex_text, codex_command, args.uninstall, previous)
        if not args.uninstall:
            url = args.url
            if not url:
                with open("/dev/tty") as terminal:
                    print("Illo MCP URL: ", end="", file=sys.stderr, flush=True)
                    url = terminal.readline().strip()
            if not url or not url.startswith(("http://", "https://")):
                raise ValueError("Set a valid Illo MCP URL.")
            token = (
                getpass.getpass("Illo token: ")
                if sys.stdin.isatty()
                else sys.stdin.readline().strip()
            )
            if not token:
                raise ValueError("The token is empty.")
            token_path = home / ".config/illo/token"
            if any(
                (parent / ".git").exists()
                for parent in [token_path.parent, *token_path.parents]
            ):
                raise ValueError("The token file must stay outside every checkout.")
            # A token has no backup. A backup would keep a revoked credential alive on disk.
            write(token_path, token + "\n", backup=False)
            write(
                config,
                json.dumps(
                    {
                        "mcp_url": url,
                        "token_file": str(token_path),
                        "previous_notify": previous_command,
                    }
                )
                + "\n",
            )
            write(
                manifest,
                json.dumps(
                    {
                        "claude_command": command,
                        "claude_path": str(claude),
                        "codex_path": str(codex),
                        "previous_notify": previous_command,
                        "previous_notify_text": previous,
                    }
                )
                + "\n",
            )
        if updated != current:
            write(claude, json.dumps(updated, indent=2) + "\n")
        if codex_updated != codex_text:
            write(codex, codex_updated)
        print(
            "Illo hooks removed. The token stays in its file."
            if args.uninstall
            else "Illo hooks installed."
        )
        return 0
    except (OSError, ValueError, KeyError):
        print("The hook setup failed. Check the URL and settings.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

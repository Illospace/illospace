import copy
import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from collectors.hooks.report import lane_from_hook, repository
from collectors.hooks.settings import EVENTS, merge_claude, merge_codex

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "event,state",
    [
        ("SessionStart", "working"),
        ("UserPromptSubmit", "working"),
        ("Stop", "unknown"),
        ("SessionEnd", "done"),
    ],
)
def test_claude_states(event, state):
    lane = lane_from_hook(
        "claude-code",
        {
            "session_id": "acme-session",
            "cwd": "/tmp/acme",
            "hook_event_name": event,
            "transcript_path": "PRIVATE",
        },
    )
    assert lane["state"] == state
    assert lane["title"] == "acme"
    assert "transcript_path" not in lane


@pytest.mark.parametrize("kind", ["permission_prompt", "idle_prompt"])
def test_claude_notification_waits(kind):
    lane = lane_from_hook(
        "claude-code",
        {
            "session_id": "acme-session",
            "hook_event_name": "Notification",
            "notification_type": kind,
            "message": "Please answer.",
        },
    )
    assert lane["state"] == "waiting_person"
    assert lane["summary"] == "Please answer."


def test_auth_notification_is_not_a_person_question():
    assert (
        lane_from_hook(
            "claude-code",
            {
                "session_id": "acme",
                "hook_event_name": "Notification",
                "notification_type": "auth_success",
            },
        )
        is None
    )


def test_codex_notify_does_not_invent_session_end_or_permission_events():
    payload = {
        "type": "agent-turn-complete",
        "thread-id": "acme",
        "cwd": "/tmp/acme",
        "input-messages": ["Check the build."],
        "last-assistant-message": "The check passes.",
    }
    lane = lane_from_hook("codex", payload, "acme/app")
    assert lane["state"] == "unknown"
    assert lane["title"] == "Check the build."
    assert lane["summary"] == "The check passes."
    assert lane["repo"] == "acme/app"
    assert set(lane) == {
        "source",
        "external_id",
        "title",
        "state",
        "summary",
        "repo",
        "machine",
        "links",
    }
    payload["type"] = "approval-requested"
    assert lane_from_hook("codex", payload) is None


def test_claude_merge_keeps_existing_hooks_and_removes_only_our_command():
    original = {
        "theme": "dark",
        "hooks": {
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "acme-old-hook"}],
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "acme-check"}],
                }
            ],
        },
    }
    before = copy.deepcopy(original)
    merged = merge_claude(original, "acme-report")
    assert original == before
    assert merged["hooks"]["Stop"][0] == original["hooks"]["Stop"][0]
    assert merged["hooks"]["PreToolUse"] == original["hooks"]["PreToolUse"]
    assert all(event in merged["hooks"] for event in EVENTS)
    assert merge_claude(merged, "acme-report") == merged
    assert merge_claude(merged, "acme-report", remove=True) == original


def test_codex_merge_round_trip_preserves_multiline_notify_and_other_settings():
    original = '# Local settings\nnotify = [\n "python3",\n "acme-notify.py",\n]\nmodel = "acme-model"\n[projects]\n'
    old_value = 'notify = [\n "python3",\n "acme-notify.py",\n]\n'
    installed = merge_codex(original, ["acme-report"])
    assert tomllib.loads(installed)["notify"] == ["acme-report"]
    assert 'model = "acme-model"' in installed
    assert merge_codex(installed, ["acme-report"]) == installed
    assert merge_codex(installed, [], remove=True, previous=old_value) == original


def test_codex_uninstall_keeps_a_notify_program_that_the_person_changed():
    text = 'notify = ["new-program"]\n'
    assert (
        merge_codex(text, [], remove=True, previous='notify = ["old-program"]\n')
        == text
    )


def test_install_and_uninstall_make_backup_keep_hooks_and_secure_token(tmp_path):
    home = tmp_path / "home"
    claude = home / ".claude/settings.json"
    codex = home / ".codex/config.toml"
    claude.parent.mkdir(parents=True)
    codex.parent.mkdir(parents=True)
    original = {
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "acme-existing"}]}]
        }
    }
    claude.write_text(json.dumps(original))
    codex_text = 'notify = ["acme-notify"]\nmodel = "acme-model"\n'
    codex.write_text(codex_text)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"CODEX_HOME", "CLAUDE_CONFIG_DIR"}
    }
    command = [
        sys.executable,
        str(ROOT / "collectors/hooks/settings.py"),
        "--home",
        str(home),
    ]
    for _ in range(2):
        result = subprocess.run(
            [*command, "--url", "https://illo.example/mcp"],
            input="acme-secret\n",
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    token = home / ".config/illo/token"
    assert token.stat().st_mode & 0o777 == 0o600
    assert "acme-secret" not in result.stdout + result.stderr
    assert json.loads((home / ".config/illo/hooks.json").read_text())[
        "previous_notify"
    ] == ["acme-notify"]
    assert len(json.loads(claude.read_text())["hooks"]["Stop"]) == 2
    assert list(claude.parent.glob("settings.json.illo-*.bak"))
    result = subprocess.run(
        [*command, "--uninstall"],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(claude.read_text()) == original
    assert codex.read_text() == codex_text
    assert token.exists()


def test_hook_invalid_input_always_succeeds_within_budget(tmp_path):
    started = time.perf_counter()
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "collectors/hooks/report.py"),
            "--source",
            "claude-code",
            "--config",
            str(tmp_path / "missing"),
        ],
        input="not json",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert time.perf_counter() - started < 0.3
    assert result.stdout == result.stderr == ""


def test_valid_hook_returns_before_its_worker_reads_missing_config(tmp_path):
    started = time.perf_counter()
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "collectors/hooks/report.py"),
            "--source",
            "claude-code",
            "--config",
            str(tmp_path / "missing"),
        ],
        input=json.dumps({"session_id": "acme", "hook_event_name": "SessionStart"}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert time.perf_counter() - started < 0.3
    assert result.stdout == result.stderr == ""


def test_worker_keeps_existing_notify_payload_and_filters_illo_payload(
    monkeypatch, tmp_path
):
    from collectors.common import MCPClient
    from collectors.hooks import report

    token = tmp_path / "token"
    token.write_text("acme-token")
    token.chmod(0o600)
    config = tmp_path / "hooks.json"
    config.write_text(
        json.dumps(
            {
                "token_file": str(token),
                "mcp_url": "https://illo.example/mcp",
                "previous_notify": ["acme-notify"],
            }
        )
    )
    original_payload = {
        "type": "agent-turn-complete",
        "thread-id": "acme",
        "cwd": "/tmp/acme",
        "input-messages": ["Check the task."],
        "private": "PRIVATE",
    }
    payload = {
        **original_payload,
        "_illo_notify_json": json.dumps(original_payload),
        "_illo_received_at": 1,
    }
    notified = []
    reported = []
    monkeypatch.setattr(
        subprocess, "Popen", lambda command, **kwargs: notified.append(command)
    )
    monkeypatch.setattr(report, "repository", lambda cwd: "acme/app")
    monkeypatch.setattr(
        MCPClient, "call", lambda self, name, args: reported.append((name, args))
    )
    report.worker("codex", payload, config)
    assert json.loads(notified[0][-1]) == original_payload
    assert "PRIVATE" not in json.dumps(reported)
    assert reported[0][0] == "lane_report"


def test_hook_detaches_before_network_work(monkeypatch, tmp_path):
    from collectors.hooks import report

    class Pipe:
        def write(self, value):
            self.value = value

        def close(self):
            pass

    class Child:
        stdin = Pipe()

    calls = []

    def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return Child()

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "report.py",
            "--source",
            "codex",
            "--config",
            str(tmp_path / "config"),
            json.dumps(
                {
                    "type": "agent-turn-complete",
                    "thread-id": "acme",
                    "private": "PRIVATE",
                }
            ),
        ],
    )
    started = time.perf_counter()
    assert report.main() == 0
    assert time.perf_counter() - started < 0.3
    assert calls[0][1]["start_new_session"] is True
    local = json.loads(Child.stdin.value)
    assert "private" not in local
    assert json.loads(local["_illo_notify_json"])["private"] == "PRIVATE"


def test_git_remote_read_clears_repository_environment(monkeypatch):
    seen = []
    monkeypatch.setenv("GIT_DIR", "/tmp/acme-repo/.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/tmp/acme-repo")

    def run(*args, **kwargs):
        seen.append(kwargs)
        return subprocess.CompletedProcess(args, 0, "git@github.com:acme/app.git\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert repository("/tmp/acme") == "acme/app"
    assert "GIT_DIR" not in seen[0]["env"]
    assert "GIT_WORK_TREE" not in seen[0]["env"]

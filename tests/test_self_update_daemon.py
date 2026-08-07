from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]
DAEMON = ROOT / "deploy" / "scripts" / "self-update-daemon.sh"


def _fake_git_tools(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    timeout = bin_dir / "timeout"
    timeout.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\n' "$*" >> "$FAKE_TIMEOUT_CALLS"
            shift
            exec "$@"
            """
        )
    )
    git = bin_dir / "git"
    git.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\n' "$*" >> "$FAKE_GIT_CALLS"
            case "$*" in
              *" fetch origin main")
                [ "${FAKE_FETCH_FAIL:-0}" != "1" ]
                ;;
              *" rev-parse refs/heads/main")
                printf '%s\n' "$FAKE_LOCAL_MAIN_SHA"
                ;;
              *" rev-parse refs/remotes/origin/main")
                printf '%s\n' "$FAKE_ORIGIN_MAIN_SHA"
                ;;
              *" merge-base --is-ancestor "*)
                [ "${FAKE_REMOTE_AHEAD:-1}" = "1" ]
                ;;
              *)
                printf 'unexpected git invocation: %s\n' "$*" >&2
                exit 2
                ;;
            esac
            """
        )
    )
    timeout.chmod(0o755)
    git.chmod(0o755)
    return bin_dir


def _daemon_env(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    request_file = tmp_path / "self-update" / "request.json"
    status_file = tmp_path / "self-update" / "status.json"
    log_path = tmp_path / "logs" / "self-update.log"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{_fake_git_tools(tmp_path)}:{env['PATH']}",
            "ILLO_SELF_UPDATE_REPO": str(tmp_path / "repo"),
            "ILLO_SELF_UPDATE_REQUEST_FILE": str(request_file),
            "ILLO_SELF_UPDATE_STATUS_FILE": str(status_file),
            "ILLO_SELF_UPDATE_LOG_PATH": str(log_path),
            "ILLO_AUTO_UPDATE_ENABLED": "1",
            "ILLO_AUTO_UPDATE_POLL_SECONDS": "300",
            "FAKE_LOCAL_MAIN_SHA": "1111111",
            "FAKE_ORIGIN_MAIN_SHA": "2222222",
            "FAKE_REMOTE_AHEAD": "1",
            "FAKE_GIT_CALLS": str(tmp_path / "git-calls.log"),
            "FAKE_TIMEOUT_CALLS": str(tmp_path / "timeout-calls.log"),
        }
    )
    return env, request_file, status_file, log_path


def _run_daemon_function(env: dict[str, str], body: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "{DAEMON}"\n{body}'],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )


def test_auto_update_queues_once_when_origin_main_is_ahead(tmp_path):
    env, request_file, status_file, _log_path = _daemon_env(tmp_path)

    result = _run_daemon_function(
        env,
        "maybe_queue_auto_update\nmaybe_queue_auto_update",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(request_file.read_text())
    assert payload.pop("requested_at")
    assert payload == {
        "detail": "origin/main advanced from 1111111 to 2222222.",
        "requested_by": "auto-update",
    }
    assert json.loads(status_file.read_text())["status"] == "queued"
    git_calls = Path(env["FAKE_GIT_CALLS"]).read_text().splitlines()
    assert sum("fetch origin main" in call for call in git_calls) == 1
    assert Path(env["FAKE_TIMEOUT_CALLS"]).read_text().splitlines() == [
        f"60 git -C {tmp_path / 'repo'} fetch origin main"
    ]


def test_auto_update_skips_fetch_while_update_is_running(tmp_path):
    env, request_file, _status_file, _log_path = _daemon_env(tmp_path)
    running_file = Path(f"{request_file}.running")
    running_file.parent.mkdir(parents=True)
    running_file.write_text("{}")

    result = _run_daemon_function(env, "maybe_queue_auto_update")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not request_file.exists()
    assert not Path(env["FAKE_GIT_CALLS"]).exists()
    assert not Path(env["FAKE_TIMEOUT_CALLS"]).exists()


def test_auto_update_retries_after_a_bounded_fetch_failure(tmp_path):
    env, request_file, _status_file, log_path = _daemon_env(tmp_path)

    result = _run_daemon_function(
        env,
        "export FAKE_FETCH_FAIL=1\n"
        "maybe_queue_auto_update\n"
        "export FAKE_FETCH_FAIL=0\n"
        "maybe_queue_auto_update",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(request_file.read_text())["requested_by"] == "auto-update"
    git_calls = Path(env["FAKE_GIT_CALLS"]).read_text().splitlines()
    assert sum("fetch origin main" in call for call in git_calls) == 2
    assert "Auto-update fetch failed" in log_path.read_text()
    timeout_calls = Path(env["FAKE_TIMEOUT_CALLS"]).read_text().splitlines()
    assert timeout_calls == [
        f"60 git -C {tmp_path / 'repo'} fetch origin main",
        f"60 git -C {tmp_path / 'repo'} fetch origin main",
    ]


def test_auto_update_poll_waits_for_interval_and_survives_failure(tmp_path):
    env, _request_file, _status_file, log_path = _daemon_env(tmp_path)
    log_path.parent.mkdir(parents=True)

    result = _run_daemon_function(
        env,
        "calls=0\n"
        "maybe_queue_auto_update() { calls=$((calls + 1)); [ \"$calls\" -gt 1 ]; }\n"
        "initialize_auto_update_poll 100\n"
        "poll_auto_update_if_due 399\n"
        "poll_auto_update_if_due 400\n"
        "poll_auto_update_if_due 699\n"
        "poll_auto_update_if_due 700\n"
        "printf '%s %s\\n' \"$calls\" \"$AUTO_UPDATE_NEXT_POLL_AT\"",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "2 1000"
    assert "failed safely with exit code 1" in log_path.read_text()


def test_auto_update_respects_python_writer_start_lock(tmp_path):
    env, request_file, _status_file, _log_path = _daemon_env(tmp_path)
    start_lock = request_file.with_name(f".{request_file.name}.starting")
    start_lock.parent.mkdir(parents=True)
    start_lock.write_text("python-writer")

    blocked = _run_daemon_function(env, "maybe_queue_auto_update")

    assert blocked.returncode == 0, blocked.stdout + blocked.stderr
    assert not request_file.exists()
    assert start_lock.read_text() == "python-writer"

    start_lock.unlink()
    queued = _run_daemon_function(env, "maybe_queue_auto_update")

    assert queued.returncode == 0, queued.stdout + queued.stderr
    assert json.loads(request_file.read_text())["requested_by"] == "auto-update"


@pytest.mark.parametrize(
    ("origin_sha", "remote_ahead"),
    (("1111111", "1"), ("2222222", "0")),
)
def test_auto_update_does_not_queue_equal_or_divergent_main(
    tmp_path,
    origin_sha,
    remote_ahead,
):
    env, request_file, status_file, _log_path = _daemon_env(tmp_path)
    env["FAKE_ORIGIN_MAIN_SHA"] = origin_sha
    env["FAKE_REMOTE_AHEAD"] = remote_ahead

    result = _run_daemon_function(env, "maybe_queue_auto_update")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not request_file.exists()
    assert not status_file.exists()

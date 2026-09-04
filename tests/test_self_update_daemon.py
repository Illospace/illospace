from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import textwrap
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
DAEMON = ROOT / "deploy" / "scripts" / "self-update-daemon.sh"
HEALTHCHECK = ROOT / "deploy" / "scripts" / "self-update-healthcheck.sh"


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
        timeout=15,
    )


def _heartbeat_env(tmp_path: Path) -> tuple[dict[str, str], list[Path]]:
    env, *_ = _daemon_env(tmp_path)
    paths = []
    for controller in ("SELF_UPDATE", "RUNTIME_SERVICES", "WORKSPACE_TOOLS"):
        path = tmp_path / f"{controller.lower()}-heartbeat.json"
        env[f"ILLO_{controller}_HEARTBEAT_FILE"] = str(path)
        paths.append(path)
    env["ILLO_SELF_UPDATE_HEARTBEAT_MAX_AGE_SECONDS"] = "1"
    env["ILLO_SELF_UPDATE_HEARTBEAT_INTERVAL_SECONDS"] = "0.05"
    return env, paths


def test_blocking_build_keeps_all_heartbeats_healthy_and_reaps_keeper(tmp_path):
    env, paths = _heartbeat_env(tmp_path)
    result = _run_daemon_function(
        env,
        f'''
        blocking_build() {{
          echo "$HEARTBEAT_KEEPER_PID" > "$HEARTBEAT_FILE.keeper"
          for path in "$HEARTBEAT_FILE" "$RUNTIME_SERVICES_HEARTBEAT_FILE" "$WORKSPACE_TOOLS_HEARTBEAT_FILE"; do
            for attempt in {{1..100}}; do
              [ ! -f "$path" ] || break
              sleep 0.01
            done
            cp "$path" "$path.before"
          done
          sleep 2.2
          for path in "$HEARTBEAT_FILE" "$RUNTIME_SERVICES_HEARTBEAT_FILE" "$WORKSPACE_TOOLS_HEARTBEAT_FILE"; do
            ILLO_SELF_UPDATE_HEARTBEAT_FILE="$path" bash "{HEALTHCHECK}" || return 1
          done
        }}
        run_with_heartbeats blocking_build
        [ -z "$HEARTBEAT_KEEPER_PID" ]
        ! kill -0 "$(cat "$HEARTBEAT_FILE.keeper")" 2>/dev/null
        ''',
    )

    assert result.returncode == 0, result.stdout + result.stderr
    for path in paths:
        before = json.loads(Path(f"{path}.before").read_text())["updated_at"]
        assert json.loads(path.read_text())["updated_at"] > before


def test_heartbeats_expire_after_keeper_cleanup(tmp_path):
    env, paths = _heartbeat_env(tmp_path)
    result = _run_daemon_function(env, "run_with_heartbeats sleep 0.2")
    assert result.returncode == 0, result.stdout + result.stderr
    snapshots = [path.read_bytes() for path in paths]
    time.sleep(2.1)

    for path, snapshot in zip(paths, snapshots):
        assert path.read_bytes() == snapshot
        check = subprocess.run(
            ["bash", str(HEALTHCHECK)],
            env={**env, "ILLO_SELF_UPDATE_HEARTBEAT_FILE": str(path)},
            capture_output=True,
            timeout=5,
        )
        assert check.returncode != 0


def test_keeper_preserves_failed_operation_status_across_repeated_calls(tmp_path):
    env, _paths = _heartbeat_env(tmp_path)
    result = _run_daemon_function(
        env,
        '''
        fail_build() { sleep 0.15; return 7; }
        for attempt in 1 2 3; do
          set +e
          run_with_heartbeats fail_build
          result=$?
          set -e
          [ "$result" -eq 7 ]
          [ -z "$HEARTBEAT_KEEPER_PID" ]
        done
        ''',
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT, signal.SIGKILL, signal.SIGSTOP])
def test_keeper_stops_when_controller_stops(tmp_path, stop_signal):
    env, paths = _heartbeat_env(tmp_path)
    pid_path = tmp_path / "keeper.pid"
    build_pid_path = tmp_path / "build.pid"
    body = f'''
    blocking_build() {{
      echo "$HEARTBEAT_KEEPER_PID" > "{pid_path}"
      sleep 10 &
      echo $! > "{build_pid_path}"
      wait $!
    }}
    run_with_heartbeats blocking_build
    '''
    daemon = subprocess.Popen(
        ["bash", "-c", f'source "{DAEMON}"\n{body}'],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not (pid_path.exists() and all(path.exists() for path in paths)):
            assert time.monotonic() < deadline, "keeper did not start"
            time.sleep(0.01)
        keeper_pid = int(pid_path.read_text())
        build_pid = int(build_pid_path.read_text())
        daemon.send_signal(stop_signal)
        if stop_signal == signal.SIGSTOP:
            # The timer may still signal, but only the stopped controller writes.
            time.sleep(0.1)
            snapshots = [path.read_bytes() for path in paths]
            time.sleep(0.2)
            assert [path.read_bytes() for path in paths] == snapshots
            daemon.kill()
        daemon.wait(timeout=5)
        deadline = time.monotonic() + 5
        while True:
            try:
                os.kill(keeper_pid, 0)
            except ProcessLookupError:
                break
            assert time.monotonic() < deadline, "keeper survived its controller"
            time.sleep(0.02)
        snapshots = [path.read_bytes() for path in paths]
        time.sleep(0.1)
        assert [path.read_bytes() for path in paths] == snapshots
        with pytest.raises(ProcessLookupError):
            os.kill(build_pid, 0)
    finally:
        # Also remove the simulated build after SIGKILL/SIGSTOP.
        try:
            os.killpg(daemon.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        daemon.wait(timeout=5)


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

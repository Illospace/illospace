"""The Compose worker-swap state machine, driven against a container simulator.

These tests exist because this deploy path has now produced the same class of
outage twice in opposite directions -- #486 left two workers claiming, and the
cleanup it added left zero -- while `docker ps` stayed green both times. So the
harness models what the deploy can actually observe and mutate: which containers
are running, changed by the same `docker` verbs the script issues. A stub that
answers "fine" to everything hides exactly the bugs worth testing for.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_LIB = ROOT / "deploy" / "scripts" / "compose-runtime-lib.sh"


def _simulator(
    state: Path,
    *,
    body: str,
    handoff_starts: bool = True,
    recreate_succeeds: bool = True,
    survives_kill: bool = False,
) -> str:
    """Build a bash script whose `docker`/`compose` mutate simulated containers.

    A container is running while `$state/<id>.running` exists. A hard kill stops
    it, `docker rm` removes it, and `compose up --force-recreate` swaps the
    service container for a new id. Handoff ids carry Compose's one-off label.
    """

    state.mkdir(parents=True, exist_ok=True)
    (state / "worker-1.running").write_text("1")

    return f'''
export STATE="{state}"
export SURVIVES_KILL={"1" if survives_kill else "0"}
COMPOSE_RUNTIME_HANDOFF_REAP_TIMEOUT_SECONDS=0
COMPOSE_RUNTIME_WORKER_DRAIN_TIMEOUT_SECONDS=1
source "{RUNTIME_LIB}"

log() {{ printf '%s\\n' "$*" >> "$STATE/calls.log"; }}

worker_swap_snapshot() {{ printf '2896:running\\n'; }}
worker_swap_snapshot_count() {{ printf '1\\n'; }}
worker_swap_snapshot_report() {{ printf 'Worker pre-swap check'; }}
worker_swap_snapshot_details() {{ printf '2896:running\\n'; }}
worker_swap_snapshot_run_ids() {{ printf '2896\\n'; }}

docker() {{
  log "docker $*"
  case "$1" in
    inspect)
      case "$*" in
        *State.Running*)
          [ -f "$STATE/${{!#}}.running" ] && printf 'true\\n' || printf 'false\\n'
          ;;
        *oneoff*)
          case "${{!#}}" in
            handoff-*) printf 'True\\n' ;;
            *) printf 'False\\n' ;;
          esac
          ;;
        *RestartPolicy*) printf 'unless-stopped\\n' ;;
      esac
      ;;
    kill)
      local id="${{!#}}"
      if [ "$2" = "-s" ]; then
        [ -f "$STATE/$id.running" ] || return 1
      else
        [ "$SURVIVES_KILL" = "1" ] && return 0
        rm -f "$STATE/$id.running"
      fi
      ;;
    rm) rm -f "$STATE/${{!#}}.running" ;;
    update) : ;;
  esac
  return 0
}}

compose() {{
  log "compose $*"
  case "$*" in
    "ps -q worker"|"ps --all -q worker"|"ps --status running -q worker")
      for f in "$STATE"/*.running; do
        [ -e "$f" ] || continue
        basename "$f" .running
      done
      ;;
    "up -d --force-recreate --no-deps worker")
      {"" if recreate_succeeds else "return 1"}
      for f in "$STATE"/worker-*.running; do
        [ -e "$f" ] || continue
        rm -f "$f"
      done
      : > "$STATE/worker-2.running"
      ;;
  esac
  return 0
}}

start_worker_handoff() {{
  {': > "$STATE/handoff-1.running"; printf "handoff-1\\n"' if handoff_starts else 'printf "\\n"'}
}}

# The drain never completes: the worker is wedged on an in-flight run, which is
# what run 2896 did on illo-dev for 65 minutes.
wait_for_worker_exit() {{ [ ! -f "$STATE/$1.running" ]; }}

{body}
echo "STATUS=$?"
echo "RUNNING=$(compose ps --status running -q worker 2>/dev/null | sort | paste -sd, -)"
'''


def _run(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def _calls(state: Path) -> str:
    log = state / "calls.log"
    return log.read_text() if log.exists() else ""


def test_the_drain_timeout_ends_on_a_worker_that_can_still_claim(tmp_path):
    """Regression for the illo-dev deploy of 1548c606.

    The drain timed out, the handoff worker was deleted, the already-draining
    worker was kept and announced as "the intended sole worker container", and
    nothing claimed an AgentRun for the next hour behind a green `docker ps`.
    """

    state = tmp_path / "s"
    result = _run(_simulator(state, body="update_worker_after_drain 2896:running"))

    assert "STATUS=0" in result.stdout
    # The wedged worker is replaced, not retained...
    assert result.stdout.split("RUNNING=")[1].strip() == "worker-2"
    calls = _calls(state)
    assert "kill worker-1" in calls
    # ...and the handoff is only removed after the replacement is up.
    assert calls.index("up -d --force-recreate --no-deps worker") < calls.index("rm -f handoff-1")
    assert "FORCED WORKER SWAP" in result.stderr
    assert "will never claim another AgentRun" in result.stderr


def test_the_handoff_is_kept_when_no_replacement_is_running(tmp_path):
    state = tmp_path / "s"
    result = _run(
        _simulator(state, body="update_worker_after_drain 2896:running", recreate_succeeds=False)
    )

    assert "STATUS=1" in result.stdout
    assert "rm -f handoff-1" not in _calls(state)
    assert "handoff-1" in result.stdout.split("RUNNING=")[1]
    assert "only container claiming AgentRuns" in result.stderr


def test_the_worker_is_not_drained_when_no_handoff_worker_came_up(tmp_path):
    """Signalling the only worker to drain with nothing to cover for it is the outage."""

    state = tmp_path / "s"
    result = _run(
        _simulator(state, body="update_worker_after_drain 2896:running", handoff_starts=False)
    )

    assert "STATUS=1" in result.stdout
    assert "kill -s TERM worker-1" not in _calls(state)
    assert "refusing to drain worker worker-1" in result.stderr
    assert "still claiming; nothing was interrupted" in result.stderr
    assert (state / "worker-1.running").exists()


def test_a_worker_that_survives_sigkill_stops_the_swap(tmp_path):
    """It may still be holding runs, so the deploy must not continue over it."""

    state = tmp_path / "s"
    result = _run(
        _simulator(state, body="update_worker_after_drain 2896:running", survives_kill=True)
    )

    assert "STATUS=1" in result.stdout
    assert "survived SIGKILL" in result.stderr
    # No replacement was created on top of a worker that may still own runs.
    assert "worker-2" not in result.stdout.split("RUNNING=")[1]


def test_worker_container_id_ignores_leftover_handoff_containers(tmp_path):
    """`compose ps -q worker` also lists one-off handoff containers.

    Two ids on one line made every downstream `docker kill "$worker_id"` address
    neither.
    """

    state = tmp_path / "s"
    state.mkdir(parents=True)
    (state / "handoff-1.running").write_text("1")
    script = _simulator(state, body='echo "SERVICE=$(worker_container_id)"')
    result = _run(script)

    assert "SERVICE=worker-1" in result.stdout


def test_the_idle_path_fails_loudly_when_the_replacement_cannot_start(tmp_path):
    """No handoff exists on this path, so a failed recreate means nothing claims."""

    state = tmp_path / "s"
    result = _run(
        _simulator(
            state,
            body="worker_swap_snapshot_decision() { printf 'replace\\n'; }\nreplace_idle_worker",
            recreate_succeeds=False,
        )
    )

    assert "STATUS=1" in result.stdout
    assert "NOTHING is claiming AgentRuns" in result.stderr

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
    handoff_phase: str | None = "claiming",
    recreate_succeeds: bool = True,
    replacement_phase: str | None = "claiming",
    survives_kill: bool = False,
    stub_drain_wait: bool = True,
    handoff_dies_after_ticks: int | None = None,
    handoff_state_unknown: bool = False,
    drain_timeout_seconds: int = 1,
    idle_exit_timeout_seconds: int = 120,
    claiming_timeout_seconds: int = 0,
) -> str:
    """Build a bash script whose `docker`/`compose` mutate simulated containers.

    A container is running while `$state/<id>.running` exists. A hard kill stops
    it, `docker rm` removes it, and `compose up --force-recreate` swaps the
    service container for a new id. Handoff ids carry Compose's one-off label.

    With `stub_drain_wait=False` the real `wait_for_worker_exit` runs, with
    `sleep` replaced by a tick counter so the loop is instantaneous. The worker
    it waits on never drains, so the only way the wait can end is the condition
    under test.
    """

    state.mkdir(parents=True, exist_ok=True)
    (state / "worker-1.running").write_text("1")
    (state / "worker-1.generation").write_text("worker-generation-1")

    return f'''
export STATE="{state}"
export SURVIVES_KILL={"1" if survives_kill else "0"}
export HANDOFF_DIES_AFTER_TICKS={-1 if handoff_dies_after_ticks is None else handoff_dies_after_ticks}
export HANDOFF_STATE_UNKNOWN={"1" if handoff_state_unknown else "0"}
COMPOSE_RUNTIME_HANDOFF_REAP_TIMEOUT_SECONDS=0
COMPOSE_RUNTIME_WORKER_DRAIN_TIMEOUT_SECONDS={drain_timeout_seconds}
COMPOSE_RUNTIME_WORKER_IDLE_EXIT_TIMEOUT_SECONDS={idle_exit_timeout_seconds}
COMPOSE_RUNTIME_WORKER_CLAIMING_TIMEOUT_SECONDS={claiming_timeout_seconds}
source "{RUNTIME_LIB}"

ticks() {{ cat "$STATE/ticks" 2>/dev/null || printf '0\\n'; }}

{'' if stub_drain_wait else '''
sleep() {
  local n
  n=$(( $(ticks) + 1 ))
  printf '%s\\n' "$n" > "$STATE/ticks"
  if [ "$HANDOFF_DIES_AFTER_TICKS" -ge 0 ] && [ "$n" -ge "$HANDOFF_DIES_AFTER_TICKS" ]; then
    rm -f "$STATE/handoff-1.running"
  fi
  return 0
}
'''}

log() {{ printf '%s\\n' "$*" >> "$STATE/calls.log"; }}

publish_phase() {{
  local id="$1"
  local phase="$2"
  printf '%s\\n' "$phase" > "$STATE/$id.phase"
  cp "$STATE/$id.generation" "$STATE/$id.phase-generation"
}}

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
          # A snap dockerd restart answers nothing at all, which is not the same
          # answer as "not running".
          case "${{!#}}" in
            handoff-*) [ "$HANDOFF_STATE_UNKNOWN" = "1" ] && return 1 ;;
          esac
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
    exec)
      local id="$2"
      [ -f "$STATE/$id.phase" ] || return 1
      [ -f "$STATE/$id.generation" ] || return 1
      if ! cmp -s "$STATE/$id.generation" "$STATE/$id.phase-generation"; then
        printf 'unknown\\n'
        return 0
      fi
      cat "$STATE/$id.phase"
      ;;
    kill)
      local id="${{!#}}"
      if [ "$2" = "-s" ]; then
        [ -f "$STATE/$id.running" ] || return 1
      else
        [ "$SURVIVES_KILL" = "1" ] && return 0
        rm -f "$STATE/$id.running"
        rm -f "$STATE/$id.phase" "$STATE/$id.phase-generation"
      fi
      ;;
    rm)
      rm -f "$STATE/${{!#}}.running" "$STATE/${{!#}}.stopped"
      rm -f "$STATE/${{!#}}.phase" "$STATE/${{!#}}.phase-generation"
      rm -f "$STATE/${{!#}}.generation"
      ;;
    update) : ;;
  esac
  return 0
}}

compose() {{
  log "compose $*"
  case "$*" in
    "ps -q worker"|"ps --status running -q worker")
      for f in "$STATE"/*.running; do
        [ -e "$f" ] || continue
        basename "$f" .running
      done
      ;;
    # `--all` also lists the exited handoff containers an interrupted deploy left
    # behind -- the whole reason `worker_container_id` has to filter one-offs.
    "ps --all -q worker")
      for f in "$STATE"/*.running "$STATE"/*.stopped; do
        [ -e "$f" ] || continue
        name="$(basename "$f")"
        printf '%s\\n' "${{name%.*}}"
      done
      ;;
    "up -d --force-recreate --no-deps worker")
      {"" if recreate_succeeds else "return 1"}
      for f in "$STATE"/worker-*.running; do
        [ -e "$f" ] || continue
        rm -f "$f"
      done
      : > "$STATE/worker-2.running"
      printf 'worker-generation-2\\n' > "$STATE/worker-2.generation"
      {"publish_phase worker-2 " + repr(replacement_phase) if replacement_phase is not None else ":"}
      ;;
  esac
  return 0
}}

start_worker_handoff() {{
  {': > "$STATE/handoff-1.running"; printf "handoff-generation-1\\n" > "$STATE/handoff-1.generation"; ' + ("publish_phase handoff-1 " + repr(handoff_phase) + "; " if handoff_phase is not None else "") + 'printf "handoff-1\\n"' if handoff_starts else 'printf "\\n"'}
}}

# The drain never completes: the worker is wedged on an in-flight run, which is
# what run 2896 did on illo-dev for 65 minutes.
{'wait_for_worker_exit() { [ ! -f "$STATE/$1.running" ]; }' if stub_drain_wait else ''}

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


def test_a_starting_handoff_does_not_authorize_draining_the_last_claimer(tmp_path):
    state = tmp_path / "s"
    result = _run(
        _simulator(
            state,
            body="update_worker_after_drain 2896:running",
            handoff_phase="starting",
        )
    )

    assert "STATUS=1" in result.stdout
    assert "kill -s TERM worker-1" not in _calls(state)
    assert "not confirmed claiming" in result.stderr
    assert (state / "worker-1.running").exists()


def test_an_unknown_handoff_phase_does_not_authorize_drain_or_removal(tmp_path):
    state = tmp_path / "s"
    result = _run(
        _simulator(
            state,
            body="update_worker_after_drain 2896:running",
            handoff_phase=None,
        )
    )

    calls = _calls(state)
    assert "STATUS=1" in result.stdout
    assert "kill -s TERM worker-1" not in calls
    assert "rm -f handoff-1" not in calls
    assert "last cover observation: pending" in result.stderr
    assert (state / "worker-1.running").exists()
    assert (state / "handoff-1.running").exists()


def test_a_starting_replacement_does_not_authorize_handoff_removal(tmp_path):
    state = tmp_path / "s"
    result = _run(
        _simulator(
            state,
            body="update_worker_after_drain 2896:running",
            replacement_phase="starting",
        )
    )

    calls = _calls(state)
    assert "STATUS=1" in result.stdout
    assert "kill -s TERM worker-1" in calls
    assert "kill -s TERM handoff-1" not in calls
    assert "rm -f handoff-1" not in calls
    assert "replacement worker worker-2 is not confirmed claiming" in result.stderr
    assert (state / "handoff-1.running").exists()


def test_an_unknown_replacement_phase_never_authorizes_handoff_removal(tmp_path):
    state = tmp_path / "s"
    result = _run(
        _simulator(
            state,
            body="update_worker_after_drain 2896:running",
            replacement_phase=None,
        )
    )

    calls = _calls(state)
    assert "STATUS=1" in result.stdout
    assert "kill -s TERM handoff-1" not in calls
    assert "rm -f handoff-1" not in calls
    assert "last cover observation: pending" in result.stderr
    assert (state / "handoff-1.running").exists()


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


def test_the_drain_wait_ends_when_the_handoff_worker_dies(tmp_path):
    """Regression for the illo-dev outage of 2026-07-28 (6caa36d, i.e. #544 itself).

    The handoff came up, passed the one-shot liveness check, claimed runs for 14
    minutes, then exited 1 on its own `queue stalled; exiting for restart`
    supervisor -- a contract a `compose run` container (restart=no) can never
    honour. The drain wait watched only the clock, whose deadline was 24h away,
    so nothing claimed an AgentRun for 2h16m.
    """

    state = tmp_path / "s"
    script = _simulator(
        state,
        body=': > "$STATE/handoff-1.running"\nwait_for_worker_exit worker-1 handoff-1',
        stub_drain_wait=False,
        handoff_dies_after_ticks=1,
        # A deadline far beyond any tick this test performs: the wait must end on
        # the lost cover, never because the clock happened to run out.
        drain_timeout_seconds=86400,
    )
    result = _run(script)

    assert "STATUS=2" in result.stdout
    assert "stopped claiming after" in result.stderr
    assert "NOTHING is claiming AgentRuns" in result.stderr
    assert "restart=no" in result.stderr
    # The worker it was draining is untouched by the wait itself.
    assert (state / "worker-1.running").exists()


def test_idle_worker_that_ignores_sigterm_is_replaced_at_idle_deadline(tmp_path):
    """Regression for the 2026-08-08 deploy that waited on an idle worker for 24h."""

    state = tmp_path / "s"
    script = _simulator(
        state,
        body=(
            "worker_swap_snapshot_decision() { printf 'replace\\n'; }\n"
            "worker_swap_snapshot_count() { printf '0\\n'; }\n"
            'sleep() { local n; n=$(( $(ticks) + 1 )); printf "%s\\n" "$n" > "$STATE/ticks"; '
            "SECONDS=$((SECONDS + $1)); return 0; }\n"
            "replace_idle_worker"
        ),
        stub_drain_wait=False,
        drain_timeout_seconds=86400,
        idle_exit_timeout_seconds=10,
    )
    result = _run(script)

    assert "STATUS=0" in result.stdout
    assert result.stdout.split("RUNNING=")[1].strip() == "worker-2"
    assert (state / "ticks").read_text().strip() == "14"
    assert "idle exit deadline of 10s" in result.stderr
    assert "86400s" not in result.stderr
    assert "No interactive AgentRuns are affected" in result.stderr
    calls = _calls(state)
    assert "kill -s TERM worker-1" in calls
    assert "kill worker-1" in calls


def test_idle_deadline_is_abandoned_and_rearmed_when_active_runs_reappear(tmp_path):
    state = tmp_path / "s"
    script = _simulator(
        state,
        body=(
            "worker_swap_snapshot_count() { "
            'case "$(ticks)" in 18) printf \'1\\n\' ;; *) printf \'0\\n\' ;; esac; }\n'
            'sleep() { local n; n=$(( $(ticks) + 1 )); printf "%s\\n" "$n" > "$STATE/ticks"; '
            "SECONDS=$((SECONDS + $1)); return 0; }\n"
            "wait_for_worker_exit worker-1 ''"
        ),
        stub_drain_wait=False,
        drain_timeout_seconds=300,
        idle_exit_timeout_seconds=40,
    )
    result = _run(script)

    assert "STATUS=3" in result.stdout
    # Zero at 30s and 60s arms a deadline at 100s. The active run at 90s
    # abandons it. Zero at 120s and 150s re-arms a new deadline at 190s.
    assert (state / "ticks").read_text().strip() == "38"
    assert "active AgentRuns reappeared" in result.stderr
    assert "idle exit deadline of 40s" in result.stderr
    assert "did not drain within 300s" not in result.stderr


def test_active_run_at_idle_deadline_requires_two_fresh_zero_snapshots(tmp_path):
    state = tmp_path / "s"
    script = _simulator(
        state,
        body=(
            "worker_swap_snapshot_count() { "
            'case "$(ticks)" in 19) printf \'1\\n\' ;; *) printf \'0\\n\' ;; esac; }\n'
            'sleep() { local n; n=$(( $(ticks) + 1 )); printf "%s\\n" "$n" > "$STATE/ticks"; '
            "SECONDS=$((SECONDS + $1)); return 0; }\n"
            "wait_for_worker_exit worker-1 ''"
        ),
        stub_drain_wait=False,
        drain_timeout_seconds=300,
        idle_exit_timeout_seconds=35,
    )
    result = _run(script)

    assert "STATUS=3" in result.stdout
    # Zero at 30s and 60s arms a deadline at 95s. The active run at that
    # deadline abandons it. Zero at 120s and 150s re-arms a fresh deadline.
    assert (state / "ticks").read_text().strip() == "37"
    assert result.stderr.count("active AgentRuns reappeared") == 1
    assert "idle exit deadline of 35s" in result.stderr
    assert "did not drain within 300s" not in result.stderr


def test_an_unanswered_handoff_inspect_does_not_end_the_drain_wait(tmp_path):
    """dockerd here is a snap that restarts itself, taking every inspect with it.

    "docker did not answer" must never be read as "the handoff is gone", or a
    60-second daemon refresh force-replaces a perfectly healthy worker.
    """

    state = tmp_path / "s"
    script = _simulator(
        state,
        # The handoff stays up; only the answers stop coming. The worker drains
        # normally after a few ticks, which is the only way out of this wait.
        body=(
            ': > "$STATE/handoff-1.running"\n'
            'sleep() { local n; n=$(( $(ticks) + 1 )); printf "%s\\n" "$n" > "$STATE/ticks"; '
            '[ "$n" -ge 4 ] && rm -f "$STATE/worker-1.running"; return 0; }\n'
            "wait_for_worker_exit worker-1 handoff-1"
        ),
        stub_drain_wait=False,
        handoff_state_unknown=True,
        drain_timeout_seconds=86400,
    )
    result = _run(script)

    assert "STATUS=0" in result.stdout
    assert "NOTHING is claiming AgentRuns" not in result.stderr


def test_claiming_wait_observes_unknown_then_starting_then_claiming(tmp_path):
    state = tmp_path / "s"
    script = _simulator(
        state,
        body=(
            ': > "$STATE/handoff-1.running"\n'
            'printf "handoff-generation-1\\n" > "$STATE/handoff-1.generation"\n'
            'sleep() { local n; n=$(( $(ticks) + 1 )); '
            'printf "%s\\n" "$n" > "$STATE/ticks"; '
            'case "$n" in '
            '1) publish_phase handoff-1 starting ;; '
            '2) publish_phase handoff-1 claiming ;; '
            "esac; return 0; }\n"
            "wait_for_worker_claiming handoff-1"
        ),
        claiming_timeout_seconds=10,
    )
    result = _run(script)

    assert "STATUS=0" in result.stdout
    assert (state / "ticks").read_text().strip() == "2"


def test_drain_wait_loses_cover_when_claiming_becomes_draining(tmp_path):
    state = tmp_path / "s"
    script = _simulator(
        state,
        body=(
            ': > "$STATE/handoff-1.running"\n'
            'printf "handoff-generation-1\\n" > "$STATE/handoff-1.generation"\n'
            "publish_phase handoff-1 claiming\n"
            'sleep() { printf "1\\n" > "$STATE/ticks"; '
            "publish_phase handoff-1 draining; return 0; }\n"
            "wait_for_worker_exit worker-1 handoff-1"
        ),
        stub_drain_wait=False,
        drain_timeout_seconds=86400,
    )
    result = _run(script)

    assert "STATUS=2" in result.stdout
    assert "stopped claiming" in result.stderr
    assert (state / "handoff-1.running").exists()


def test_restarted_container_cannot_reuse_a_stale_claiming_record(tmp_path):
    state = tmp_path / "s"
    script = _simulator(
        state,
        body=(
            ': > "$STATE/handoff-1.running"\n'
            'printf "old-generation\\n" > "$STATE/handoff-1.generation"\n'
            "publish_phase handoff-1 claiming\n"
            'printf "new-generation\\n" > "$STATE/handoff-1.generation"\n'
            "wait_for_worker_claiming handoff-1"
        ),
    )
    result = _run(script)

    assert "STATUS=1" in result.stdout
    assert "last cover observation: pending" in result.stderr


def test_draining_worker_fails_the_shared_capacity_assertion(tmp_path):
    state = tmp_path / "s"
    script = _simulator(
        state,
        body=(
            "publish_phase worker-1 draining\n"
            "assert_worker_not_drained"
        ),
    )
    result = _run(script)

    assert "STATUS=5" in result.stdout
    assert "worker-1" in result.stderr
    assert "draining" in result.stderr
    assert "cannot claim new AgentRuns" in result.stderr


def test_claiming_worker_passes_the_shared_capacity_assertion(tmp_path):
    state = tmp_path / "s"
    script = _simulator(
        state,
        body=(
            "publish_phase worker-1 claiming\n"
            "assert_worker_not_drained"
        ),
    )
    result = _run(script)

    assert "STATUS=0" in result.stdout
    assert result.stderr == ""


def test_stale_draining_record_is_not_accepted_as_the_live_worker_phase(tmp_path):
    state = tmp_path / "s"
    script = _simulator(
        state,
        body=(
            "publish_phase worker-1 draining\n"
            'printf "worker-generation-2\\n" > "$STATE/worker-1.generation"\n'
            "assert_worker_not_drained"
        ),
    )
    result = _run(script)

    assert "STATUS=0" in result.stdout
    assert "DRAINED" not in result.stderr
    assert "draining" not in result.stderr


def test_a_confirmed_handoff_exit_ends_the_drain_wait(tmp_path):
    """A definite Docker exit is lost cover; only an unanswered inspect is pending."""

    state = tmp_path / "s"
    script = _simulator(
        state,
        body=(
            ': > "$STATE/handoff-1.running"\n'
            'sleep() { local n; n=$(( $(ticks) + 1 )); printf "%s\\n" "$n" > "$STATE/ticks"; '
            'case "$n" in 1) rm -f "$STATE/handoff-1.running" ;; esac; return 0; }\n'
            "wait_for_worker_exit worker-1 handoff-1"
        ),
        stub_drain_wait=False,
        drain_timeout_seconds=86400,
    )
    result = _run(script)

    assert "STATUS=2" in result.stdout
    assert "NOTHING is claiming AgentRuns" in result.stderr


def test_a_lost_handoff_force_replaces_the_drained_worker(tmp_path):
    """End to end: losing the cover escalates instead of waiting out the deadline."""

    state = tmp_path / "s"
    result = _run(
        _simulator(
            state,
            body="update_worker_after_drain 2896:running",
            stub_drain_wait=False,
            handoff_dies_after_ticks=1,
            drain_timeout_seconds=86400,
        )
    )

    assert "STATUS=0" in result.stdout
    assert result.stdout.split("RUNNING=")[1].strip() == "worker-2"
    assert "FORCED WORKER SWAP" in result.stderr
    assert "handoff worker handoff-1 lost claiming capacity" in result.stderr
    # The banner must not promise cover that is exactly what died.
    assert "keeps claiming new AgentRuns" not in result.stderr


def test_stopped_handoff_containers_are_removed_before_a_new_one_starts(tmp_path):
    """illo-dev still carried the Exited(1) handoff from the outage.

    A running handoff is another claimer and is deliberately left alone --
    deleting a claimer is the #486 outage.
    """

    state = tmp_path / "s"
    state.mkdir(parents=True)
    (state / "handoff-old.stopped").write_text("1")
    (state / "handoff-live.running").write_text("1")
    result = _run(_simulator(state, body="reap_stopped_worker_handoffs"))

    calls = _calls(state)
    assert "rm -f handoff-old" in calls
    assert "rm -f handoff-live" not in calls
    # The real service container is never a reaping candidate.
    assert "rm -f worker-1" not in calls


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

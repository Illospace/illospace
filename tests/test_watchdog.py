"""Tests for the host-level Compose watchdog."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
WATCHDOG = ROOT / "deploy" / "scripts" / "watchdog-check.sh"
INSTALLER = ROOT / "deploy" / "scripts" / "install-watchdog-unit.sh"


@pytest.fixture
def watchdog_env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$WATCHDOG_DOCKER_LOG"
if [ "$1" = "compose" ]; then
  case "$*" in
    *" ps -q updater") printf 'updater-id\\n'; exit 0 ;;
    *" ps --services --status running")
      printf 'postgres\\napi\\nweb\\nworker\\nscheduler\\nupdater\\n'
      exit 0
      ;;
    *" ps -q "*) printf '%s-id\\n' "${*: -1}"; exit 0 ;;
    *" up -d"|*" restart "*) exit 0 ;;
  esac
fi
if [ "$1" = "inspect" ]; then
  case "$3" in
    *oneoff*) printf 'false\\n' ;;
    *State.Health*)
      if [ "$4" = "scheduler-id" ] && [ "${WATCHDOG_UNHEALTHY_SCHEDULER:-0}" = "1" ]; then
        printf 'unhealthy\\n'
      else
        printf 'healthy\\n'
      fi
      ;;
  esac
  exit 0
fi
if [ "$1" = "exec" ]; then
  if [ -n "${WATCHDOG_UPDATE_RUNNING_AFTER:-}" ]; then
    count=0
    [ ! -f "$WATCHDOG_UPDATE_COUNTER" ] || count="$(cat "$WATCHDOG_UPDATE_COUNTER")"
    count=$((count + 1))
    printf '%s\\n' "$count" > "$WATCHDOG_UPDATE_COUNTER"
  fi
  if [ "${WATCHDOG_UPDATE_RUNNING:-0}" = "1" ] \
    || { [ -n "${WATCHDOG_UPDATE_RUNNING_AFTER:-}" ] \
      && [ "$count" -ge "$WATCHDOG_UPDATE_RUNNING_AFTER" ]; }; then
    printf 'running\\n'
  else
    printf 'idle\\n'
  fi
  exit 0
fi
exit 1
"""
    )
    docker.chmod(0o755)

    inert = tmp_path / "inert-check.sh"
    inert.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'called\\n' >> \"$WATCHDOG_INERT_LOG\"\n"
        "exit \"${WATCHDOG_INERT_STATUS:-0}\"\n"
    )
    inert.chmod(0o755)

    env_file = tmp_path / ".env"
    env_file.write_text("")
    docker_log = tmp_path / "docker.log"
    inert_log = tmp_path / "inert.log"
    update_counter = tmp_path / "update-counter"

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "ILLO_COMPOSE_ENV_FILE": str(env_file),
            "ILLO_WATCHDOG_INERT_CHECK": str(inert),
            "WATCHDOG_DOCKER_LOG": str(docker_log),
            "WATCHDOG_INERT_LOG": str(inert_log),
            "WATCHDOG_UPDATE_COUNTER": str(update_counter),
        }
    )
    return env, docker_log, inert_log


def _run_watchdog(env):
    return subprocess.run(
        [str(WATCHDOG)],
        capture_output=True,
        text=True,
        env=env,
    )


def _mutation_lines(docker_log: Path) -> list[str]:
    return [
        line
        for line in docker_log.read_text().splitlines()
        if line.endswith(" up -d") or " restart " in line
    ]


def test_watchdog_is_noop_on_healthy_stack(watchdog_env):
    env, docker_log, inert_log = watchdog_env

    result = _run_watchdog(env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert inert_log.read_text() == "called\n"
    assert _mutation_lines(docker_log) == []


def test_watchdog_skips_every_check_during_update(watchdog_env):
    env, docker_log, inert_log = watchdog_env
    env["WATCHDOG_UPDATE_RUNNING"] = "1"

    result = _run_watchdog(env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "self-update is in flight" in result.stdout
    assert not inert_log.exists()
    assert _mutation_lines(docker_log) == []


def test_watchdog_rechecks_update_marker_before_reconcile(watchdog_env):
    env, docker_log, _inert_log = watchdog_env
    env["WATCHDOG_INERT_STATUS"] = "3"
    env["WATCHDOG_UPDATE_RUNNING_AFTER"] = "2"

    result = _run_watchdog(env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "self-update is in flight" in result.stdout
    assert _mutation_lines(docker_log) == []


@pytest.mark.parametrize("inert_status", (3, 4))
def test_watchdog_reconciles_inert_or_down_stack_without_recreate(
    watchdog_env,
    inert_status,
):
    env, docker_log, _inert_log = watchdog_env
    env["WATCHDOG_INERT_STATUS"] = str(inert_status)

    result = _run_watchdog(env)

    assert result.returncode == 0, result.stdout + result.stderr
    mutations = _mutation_lines(docker_log)
    assert len(mutations) == 1
    assert mutations[0].endswith(" up -d")
    assert "--force-recreate" not in mutations[0]


def test_watchdog_restarts_unhealthy_scheduler_once(watchdog_env):
    env, docker_log, _inert_log = watchdog_env
    env["WATCHDOG_UNHEALTHY_SCHEDULER"] = "1"

    result = _run_watchdog(env)

    assert result.returncode == 0, result.stdout + result.stderr
    mutations = _mutation_lines(docker_log)
    assert len(mutations) == 1
    assert mutations[0].endswith(" restart scheduler")


def test_watchdog_does_not_recover_after_unknown_inert_failure(watchdog_env):
    env, docker_log, _inert_log = watchdog_env
    env["WATCHDOG_INERT_STATUS"] = "1"

    result = _run_watchdog(env)

    assert result.returncode == 1
    assert "no recovery action taken" in result.stderr
    assert _mutation_lines(docker_log) == []


def test_watchdog_installer_renders_safe_system_service_and_timer(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text("#!/usr/bin/env bash\nexit 0\n")
    docker.chmod(0o755)
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = list-unit-files ] && [ \"$2\" = docker.service ]; then\n"
        "  printf 'docker.service enabled\\n'\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    systemctl.chmod(0o755)
    env_file = tmp_path / ".env"
    env_file.write_text("")
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "ILLO_COMPOSE_ENV_FILE": str(env_file),
        }
    )

    result = subprocess.run(
        [str(INSTALLER), "--system", "--print"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    rendered = result.stdout
    assert "# illospace-watchdog.service" in rendered
    assert "Requires=docker.service" in rendered
    assert "Type=oneshot" in rendered
    assert f"ExecStart={WATCHDOG}" in rendered
    assert "RemainAfterExit" not in rendered
    assert "ExecStop=" not in rendered
    assert "# illospace-watchdog.timer" in rendered
    assert "OnBootSec=5min" in rendered
    assert "OnUnitActiveSec=5min" in rendered
    assert "WantedBy=timers.target" in rendered


def test_watchdog_installer_user_scope_waits_for_docker(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("docker", "systemctl"):
        executable = bin_dir / name
        executable.write_text("#!/usr/bin/env bash\nexit 1\n")
        executable.chmod(0o755)
    env_file = tmp_path / ".env"
    env_file.write_text("")
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "ILLO_COMPOSE_ENV_FILE": str(env_file),
        }
    )

    result = subprocess.run(
        [str(INSTALLER), "--user", "--print"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Requires=" not in result.stdout
    assert "ConditionUser=!root" in result.stdout
    assert "ExecStartPre=" in result.stdout
    assert "docker info" in result.stdout
    assert "TimeoutStartSec=900" in result.stdout


def test_compose_scheduler_healthcheck_uses_overdue_probe_only():
    compose = (ROOT / "deploy" / "compose" / "docker-compose.yml").read_text()
    scheduler_block = compose.split("\n  scheduler:\n", 1)[1].split(
        "\n  slack-connector:\n",
        1,
    )[0]
    worker_block = compose.split("\n  worker:\n", 1)[1].split(
        "\n  scheduler:\n",
        1,
    )[0]

    assert '["CMD", "python", "-m", "brain.app.cli.scheduler", "healthcheck"]' in scheduler_block
    assert "interval: 60s" in scheduler_block
    assert "retries: 3" in scheduler_block
    assert "healthcheck:" not in worker_block

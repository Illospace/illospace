"""Behavioral tests for the deploy-facing AgentRun queue health check."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]
QUEUE_STARVATION_DIAGNOSTIC = (
    "AgentRun queue starvation: fake queued backlog has no recent claims."
)


def _deploy_env_file(tmp_path: Path) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "ILLO_PUBLIC_URL=http://illospace.local",
                "SECRET_KEY=secret-key",
                "VAULT_MASTER_KEY=MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
                "DB_NAME=illospace",
                "DB_USER=illo",
                "DB_PASSWORD=db-password",
                "ILLO_AGENT_RUNNER_CONCURRENCY=8",
                "ILLO_AGENT_RUN_QUEUED_DOCTOR_SECONDS=725",
                "ILLO_DOCTOR_SKIP_LOCAL_HTTP_PROBES=1",
            ]
        )
        + "\n"
    )
    return env_file


def _fake_docker(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail

            args="$*"
            if [ "${{1:-}}" = "info" ]; then
              exit 0
            fi
            if [ "${{1:-}}" = "inspect" ]; then
              echo healthy
              exit 0
            fi
            if [ "${{1:-}}" = "exec" ] && [[ "$args" == *"brain.contracts.worker_lifecycle read"* ]]; then
              echo "${{FAKE_WORKER_PHASE:-claiming}}"
              exit 0
            fi
            if [ "${{1:-}}" = "compose" ]; then
              case "$args" in
                *" config")
                  exit 0
                  ;;
                *" ps --services --status running")
                  printf '%s\\n' postgres api web worker scheduler updater
                  exit 0
                  ;;
                *" ps -q "*)
                  echo container-id
                  exit 0
                  ;;
                *"brain.app.cli.agent_run_queue_health"*"--stale-after-seconds 725"*"--runner-concurrency 8")
                  if [ "${{FAKE_QUEUE_STARVED:-1}}" = "1" ]; then
                    echo "{QUEUE_STARVATION_DIAGNOSTIC}" >&2
                    exit 1
                  fi
                  echo "AgentRun queue healthy: no stale queued backlog."
                  exit 0
                  ;;
                *"pg_extension"*)
                  echo vector
                  exit 0
                  ;;
                *"org_api_keys"*"vault_config"*)
                  echo 1
                  exit 0
                  ;;
              esac
            fi

            echo "unexpected docker invocation: $*" >&2
            exit 2
            """
        )
    )
    docker.chmod(0o755)
    return bin_dir


def _entrypoint_env(
    tmp_path: Path,
    *,
    worker_phase: str = "claiming",
    queue_starved: bool = True,
) -> dict[str, str]:
    env = os.environ.copy()
    env["ILLO_COMPOSE_ENV_FILE"] = str(_deploy_env_file(tmp_path))
    env["PATH"] = f"{_fake_docker(tmp_path)}:{env['PATH']}"
    env["FAKE_WORKER_PHASE"] = worker_phase
    env["FAKE_QUEUE_STARVED"] = "1" if queue_starved else "0"
    return env


@pytest.mark.parametrize(
    "entrypoint",
    [
        "deploy/scripts/doctor.sh",
        "deploy/scripts/inert-stack-check.sh",
    ],
)
def test_deploy_entrypoint_fails_when_agent_run_queue_is_starved(
    entrypoint: str,
    tmp_path: Path,
):
    result = subprocess.run(
        [str(ROOT / entrypoint)],
        cwd=ROOT,
        env=_entrypoint_env(tmp_path),
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert QUEUE_STARVATION_DIAGNOSTIC in result.stderr


@pytest.mark.parametrize(
    ("entrypoint", "expected_exit_code"),
    [
        ("deploy/scripts/doctor.sh", 1),
        ("deploy/scripts/inert-stack-check.sh", 5),
    ],
)
def test_deploy_entrypoint_fails_when_worker_is_draining(
    entrypoint: str,
    expected_exit_code: int,
    tmp_path: Path,
):
    result = subprocess.run(
        [str(ROOT / entrypoint)],
        cwd=ROOT,
        env=_entrypoint_env(
            tmp_path,
            worker_phase="draining",
            queue_starved=False,
        ),
        text=True,
        capture_output=True,
    )

    assert result.returncode == expected_exit_code, result.stdout + result.stderr
    assert "container-id" in result.stderr
    assert "draining" in result.stderr
    assert "cannot claim new AgentRuns" in result.stderr


def test_drained_worker_verdict_has_one_owner_and_both_entrypoints_call_it():
    runtime_lib = (ROOT / "deploy" / "scripts" / "compose-runtime-lib.sh").read_text()
    doctor = (ROOT / "deploy" / "scripts" / "doctor.sh").read_text()
    inert_check = (ROOT / "deploy" / "scripts" / "inert-stack-check.sh").read_text()

    assert runtime_lib.count("assert_worker_not_drained() {") == 1
    assert "assert_worker_not_drained" in doctor
    assert "assert_worker_not_drained" in inert_check
    assert "draining" not in doctor
    assert "draining" not in inert_check
    assert "$STACK_DRAINED_EXIT_CODE  DRAINED" in inert_check


def test_queue_health_adapter_passes_deploy_policy_to_api_cli():
    queue_health_lib = (
        ROOT / "deploy" / "scripts" / "agent-run-queue-health-lib.sh"
    )
    script = f'''
source "{queue_health_lib}"
compose() {{
  printf '%s\\n' "$*"
}}
ILLO_AGENT_RUN_QUEUED_DOCTOR_SECONDS=725
ILLO_AGENT_RUNNER_CONCURRENCY=8
assert_agent_run_queue_not_starved
'''

    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        "exec -T api python -m brain.app.cli.agent_run_queue_health "
        "--stale-after-seconds 725 --runner-concurrency 8"
    )
    assert "psql" not in queue_health_lib.read_text()

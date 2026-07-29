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
                  echo "{QUEUE_STARVATION_DIAGNOSTIC}" >&2
                  exit 1
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


def _entrypoint_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["ILLO_COMPOSE_ENV_FILE"] = str(_deploy_env_file(tmp_path))
    env["PATH"] = f"{_fake_docker(tmp_path)}:{env['PATH']}"
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

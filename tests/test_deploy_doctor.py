"""Focused tests for the Compose deploy doctor shell script."""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_strict_credentials_uses_current_db_tables_without_legacy_user_keys(tmp_path):
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
            ]
        )
        + "\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    docker = bin_dir / "docker"
    docker.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail

            printf '%s\n' "$*" >> "$DOCTOR_DOCKER_LOG"

            if [ "${1:-}" = "info" ]; then
              exit 0
            fi

            if [ "${1:-}" = "inspect" ]; then
              echo healthy
              exit 0
            fi

            if [ "${1:-}" = "compose" ]; then
              args="$*"
              case "$args" in
                *" config"*)
                  exit 0
                  ;;
                *" ps --services --status running"*)
                  printf '%s\n' postgres api web worker scheduler updater
                  exit 0
                  ;;
                *" ps -q "*)
                  echo container-id
                  exit 0
                  ;;
                *"user_api_keys"*)
                  echo "legacy user_api_keys table should not be queried" >&2
                  exit 44
                  ;;
                *"pg_extension"*)
                  echo vector
                  exit 0
                  ;;
                *"org_api_keys"*"vault_config"*)
                  echo 2
                  exit 0
                  ;;
                *"brain.app.cli.agent_run_queue_health"*"--stale-after-seconds 600"*"--runner-concurrency 4"*)
                  echo "AgentRun queue healthy: no stale queued backlog."
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
    curl = bin_dir / "curl"
    curl.write_text("#!/usr/bin/env bash\nexit 0\n")
    curl.chmod(0o755)

    env = os.environ.copy()
    env["DOCTOR_DOCKER_LOG"] = str(docker_log)
    env["ILLO_COMPOSE_ENV_FILE"] = str(env_file)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"

    result = subprocess.run(
        [str(ROOT / "deploy" / "scripts" / "doctor.sh"), "--strict-credentials"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "DB-backed provider credentials are configured (2 key record(s))" in result.stdout
    assert "user_api_keys" not in docker_log.read_text()


def test_profile_enabled_meetbot_must_be_running(tmp_path):
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
                "COMPOSE_PROFILES=meetbot",
            ]
        )
        + "\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            if [ "${1:-}" = "info" ]; then exit 0; fi
            if [ "${1:-}" = "inspect" ]; then echo healthy; exit 0; fi
            if [ "${1:-}" = "compose" ]; then
              args="$*"
              case "$args" in
                *" config"*) exit 0 ;;
                *" ps --services --status running"*)
                  printf '%s\n' postgres api web worker scheduler updater
                  exit 0
                  ;;
                *" ps --all -q meetbot"*) echo stopped-meetbot; exit 0 ;;
                *" ps -q "*) echo container-id; exit 0 ;;
                *"pg_extension"*) echo vector; exit 0 ;;
                *"brain.app.cli.agent_run_queue_health"*)
                  echo "AgentRun queue healthy: no stale queued backlog."
                  exit 0
                  ;;
              esac
            fi
            exit 0
            """
        )
    )
    docker.chmod(0o755)
    curl = bin_dir / "curl"
    curl.write_text("#!/usr/bin/env bash\nexit 0\n")
    curl.chmod(0o755)

    env = os.environ.copy()
    env.pop("COMPOSE_PROFILES", None)
    env["ILLO_COMPOSE_ENV_FILE"] = str(env_file)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"

    result = subprocess.run(
        [str(ROOT / "deploy" / "scripts" / "doctor.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "configured meetbot is not running" in result.stderr

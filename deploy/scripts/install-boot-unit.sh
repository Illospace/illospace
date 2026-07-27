#!/usr/bin/env bash
set -euo pipefail

# Installs a systemd unit that reconciles the Compose project once at boot.
#
# Per-container restart policies are not sufficient on their own (#527): they are
# a persistent property of a container, so anything that mutates or loses that
# property -- an interrupted worker swap, a container whose containerd shim died
# during host shutdown -- silently drops that service from the next boot. They
# also ignore `depends_on`, so on boot the worker races a cold Postgres.
#
# This unit is boot-only (Type=oneshot, WantedBy=multi-user.target) and has no
# ExecStop, so it can never fire during a deploy and never tears containers
# down. `up -d` without --force-recreate is a no-op for services that are
# already running, so it cannot resurrect a worker that a running handoff
# deliberately retired.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_DIR="$ROOT/deploy/compose"
COMPOSE_FILE="$COMPOSE_DIR/docker-compose.yml"
ENV_FILE="${ILLO_COMPOSE_ENV_FILE:-$COMPOSE_DIR/.env}"
UNIT_NAME="${ILLO_BOOT_UNIT_NAME:-illospace-compose.service}"
UNIT_DIR="${ILLO_BOOT_UNIT_DIR:-/etc/systemd/system}"

PRINT_ONLY=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --print)
      PRINT_ONLY=1
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage: ./illo deploy boot-unit [--print]
       deploy/scripts/install-boot-unit.sh [--print]

Generates and installs a systemd unit that runs `docker compose up -d` once at
boot, so the whole stack -- including the worker -- comes back after an
unplanned reboot.

The unit is generated rather than committed because the Docker unit name and
binary path differ per host (a Docker snap install exposes
snap.docker.dockerd.service and /snap/bin/docker, and has no docker.service at
all, so a hardcoded Requires=docker.service fails silently).

  --print   write the generated unit to stdout without installing it
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [ ! -f "$ENV_FILE" ]; then
  echo "Missing $ENV_FILE; run ./illo deploy init first." >&2
  exit 1
fi

docker_bin="$(command -v docker || true)"
if [ -z "$docker_bin" ]; then
  echo "docker is not installed or not on PATH; cannot generate a boot unit." >&2
  exit 1
fi

# Bind to whichever Docker unit this host actually has. Checking the snap unit
# first matters: hosts running the Docker snap have no docker.service, and a
# unit that Requires= a non-existent unit refuses to start with a message that
# reads like the stack is broken rather than the dependency being misnamed.
docker_unit=""
for candidate in snap.docker.dockerd.service docker.service; do
  if systemctl list-unit-files "$candidate" >/dev/null 2>&1 \
    && systemctl list-unit-files "$candidate" 2>/dev/null | grep -q "^$candidate"; then
    docker_unit="$candidate"
    break
  fi
done
if [ -z "$docker_unit" ]; then
  echo "Could not find a Docker systemd unit (looked for snap.docker.dockerd.service and docker.service)." >&2
  echo "Set ILLO_BOOT_DOCKER_UNIT=<unit> to name it explicitly." >&2
  exit 1
fi
docker_unit="${ILLO_BOOT_DOCKER_UNIT:-$docker_unit}"

# Compose profiles are opt-in, so a profile-gated service (slack-connector)
# would otherwise be left out of the boot reconcile. Read the project's own
# COMPOSE_PROFILES rather than inventing a second knob.
profile_args=""
profiles="$(grep -E '^[[:space:]]*COMPOSE_PROFILES=' "$ENV_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d '"'"'"'' || true)"
if [ -n "$profiles" ]; then
  IFS=',' read -r -a profile_list <<< "$profiles"
  for profile in "${profile_list[@]}"; do
    [ -n "$profile" ] || continue
    profile_args="$profile_args --profile $profile"
  done
fi

unit_text="$(cat <<EOF
[Unit]
Description=Illospace Compose stack
Documentation=https://github.com/Illospace/illospace/blob/main/deploy/compose/README.md
Requires=$docker_unit
After=$docker_unit network-online.target
Wants=network-online.target
ConditionPathExists=$ENV_FILE

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$COMPOSE_DIR
# No --force-recreate: this is a reconcile, not a deploy. Services that are
# already running are left untouched.
ExecStart=$docker_bin compose --env-file $ENV_FILE -f $COMPOSE_FILE$profile_args up -d
# Boot-time image pulls can be slow on a cold cache; let it finish.
TimeoutStartSec=0
# Deliberately no ExecStop -- stopping this unit must not tear down the stack.

[Install]
WantedBy=multi-user.target
EOF
)"

if [ "$PRINT_ONLY" = "1" ]; then
  printf '%s\n' "$unit_text"
  exit 0
fi

sudo_prefix=""
if [ "$(id -u)" != "0" ]; then
  if command -v sudo >/dev/null 2>&1; then
    sudo_prefix="sudo"
  else
    echo "Installing to $UNIT_DIR needs root and sudo is unavailable." >&2
    echo "Re-run as root, or write the unit yourself with: $0 --print" >&2
    exit 1
  fi
fi

printf '%s\n' "$unit_text" | $sudo_prefix tee "$UNIT_DIR/$UNIT_NAME" >/dev/null
$sudo_prefix systemctl daemon-reload
$sudo_prefix systemctl enable "$UNIT_NAME"

echo "Installed $UNIT_DIR/$UNIT_NAME (bound to $docker_unit) and enabled it at boot."
echo "Verify with: systemctl is-enabled $UNIT_NAME"
echo "Dry-run the reconcile now with: $sudo_prefix systemctl start $UNIT_NAME"

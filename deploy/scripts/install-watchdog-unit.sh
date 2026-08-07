#!/usr/bin/env bash
set -euo pipefail

# Installs a timer that reconciles missing Compose services and restarts only
# containers that Docker has already marked unhealthy. The check never tears a
# service down or recreates a running service, and it pauses during updates.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_DIR="$ROOT/deploy/compose"
ENV_FILE="${ILLO_COMPOSE_ENV_FILE:-$COMPOSE_DIR/.env}"
CHECK_SCRIPT="$SCRIPT_DIR/watchdog-check.sh"
UNIT_NAME="${ILLO_WATCHDOG_UNIT_NAME:-illospace-watchdog.service}"
TIMER_NAME="${ILLO_WATCHDOG_TIMER_NAME:-illospace-watchdog.timer}"

PRINT_ONLY=0
SCOPE=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --print)
      PRINT_ONLY=1
      shift
      ;;
    --system)
      SCOPE=system
      shift
      ;;
    --user)
      SCOPE=user
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage: deploy/scripts/install-watchdog-unit.sh [--user|--system] [--print]

Generates and installs an Illospace watchdog service and five-minute systemd
timer. The service reconciles an inert or down Compose stack and restarts
containers that Docker marks unhealthy. It takes no action during an update.

  --system  install to /etc/systemd/system (needs root; orders after Docker)
  --user    install to ~/.config/systemd/user (waits for Docker; needs lingering)
  --print   write both generated units to stdout without installing them

Defaults to --system when running as root, otherwise --user.
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [ -z "$SCOPE" ]; then
  if [ "$(id -u)" = "0" ]; then
    SCOPE=system
  else
    SCOPE=user
  fi
fi

if [ "$SCOPE" = "system" ]; then
  UNIT_DIR="${ILLO_WATCHDOG_UNIT_DIR:-/etc/systemd/system}"
  systemctl_scope=""
else
  UNIT_DIR="${ILLO_WATCHDOG_UNIT_DIR:-$HOME/.config/systemd/user}"
  systemctl_scope="--user"
fi

if [ ! -f "$ENV_FILE" ]; then
  echo "Missing $ENV_FILE; run ./illo deploy init first." >&2
  exit 1
fi

docker_bin="$(command -v docker || true)"
if [ -z "$docker_bin" ]; then
  echo "docker is not installed or not on PATH; cannot generate watchdog units." >&2
  exit 1
fi

docker_unit=""
for candidate in snap.docker.dockerd.service docker.service; do
  if systemctl list-unit-files "$candidate" >/dev/null 2>&1 \
    && systemctl list-unit-files "$candidate" 2>/dev/null | grep -q "^$candidate"; then
    docker_unit="$candidate"
    break
  fi
done
docker_unit="${ILLO_WATCHDOG_DOCKER_UNIT:-$docker_unit}"
if [ -z "$docker_unit" ] && [ "$SCOPE" = "system" ]; then
  echo "Could not find a Docker systemd unit (looked for snap.docker.dockerd.service and docker.service)." >&2
  echo "Set ILLO_WATCHDOG_DOCKER_UNIT=<unit>, or install user units with --user." >&2
  exit 1
fi

if [ "$SCOPE" = "system" ]; then
  ordering="Requires=$docker_unit
After=$docker_unit network-online.target
Wants=network-online.target"
  readiness="# Ordered after the Docker unit, so the daemon is already up."
else
  ordering="ConditionUser=!root"
  readiness="ExecStartPre=/bin/sh -c 'until $docker_bin info >/dev/null 2>&1; do sleep 2; done'"
fi

service_text="$(cat <<EOF
[Unit]
Description=Illospace Compose watchdog
Documentation=https://github.com/Illospace/illospace/blob/main/deploy/compose/README.md
$ordering
ConditionPathExists=$ENV_FILE

[Service]
Type=oneshot
WorkingDirectory=$COMPOSE_DIR
$readiness
Environment=ILLO_COMPOSE_ENV_FILE=$ENV_FILE
Environment=ILLO_WATCHDOG_DOCKER_BIN=$docker_bin
ExecStart=$CHECK_SCRIPT
TimeoutStartSec=900
# Deliberately no ExecStop: stopping the watchdog cannot tear down the stack.
EOF
)"

timer_text="$(cat <<EOF
[Unit]
Description=Run the Illospace Compose watchdog every five minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min
Persistent=true
Unit=$UNIT_NAME

[Install]
WantedBy=timers.target
EOF
)"

if [ "$PRINT_ONLY" = "1" ]; then
  printf '# %s\n%s\n\n# %s\n%s\n' \
    "$UNIT_NAME" "$service_text" "$TIMER_NAME" "$timer_text"
  exit 0
fi

sudo_prefix=""
if [ "$SCOPE" = "system" ] && [ "$(id -u)" != "0" ]; then
  if command -v sudo >/dev/null 2>&1; then
    sudo_prefix="sudo"
  else
    echo "Installing to $UNIT_DIR needs root and sudo is unavailable." >&2
    echo "Re-run with --user, or inspect the units with: $0 --system --print" >&2
    exit 1
  fi
fi

mkdir -p "$UNIT_DIR" 2>/dev/null || $sudo_prefix mkdir -p "$UNIT_DIR"
printf '%s\n' "$service_text" | $sudo_prefix tee "$UNIT_DIR/$UNIT_NAME" >/dev/null
printf '%s\n' "$timer_text" | $sudo_prefix tee "$UNIT_DIR/$TIMER_NAME" >/dev/null
$sudo_prefix systemctl $systemctl_scope daemon-reload
$sudo_prefix systemctl $systemctl_scope enable --now "$TIMER_NAME"

if [ "$SCOPE" = "user" ]; then
  if [ "$(loginctl show-user "$(id -un)" --property=Linger --value 2>/dev/null)" != "yes" ]; then
    if loginctl enable-linger "$(id -un)" >/dev/null 2>&1; then
      echo "Enabled lingering for $(id -un) so the timer runs without a login."
    else
      echo "WARNING: lingering is not enabled for $(id -un), so the timer will not run at boot." >&2
      echo "Enable it with: sudo loginctl enable-linger $(id -un)" >&2
    fi
  fi
fi

echo "Installed $UNIT_DIR/$UNIT_NAME and $UNIT_DIR/$TIMER_NAME (${SCOPE} scope) and started the timer."
echo "Verify with: systemctl $systemctl_scope status $TIMER_NAME"

#!/usr/bin/env bash
# Deploy Illo Brain on a remote server over SSH.
#
# Example:
#   ./ops/deploy-remote.sh --host example.com --user illo --dir ~/illo-brain
#
# The remote checkout is expected to already exist:
#   ssh illo@example.com
#   cd ~/illo-brain
#   git fetch origin main
#   git checkout -f -B main origin/main
#   git reset --hard origin/main
#   ./ops/deploy.sh
#
# Passwords are intentionally not stored here; SSH will prompt when needed.
set -euo pipefail

REMOTE_USER="${ILLO_DEPLOY_USER:-illo}"
REMOTE_HOST="${ILLO_DEPLOY_HOST:-}"
REMOTE_DIR="${ILLO_DEPLOY_DIR:-illo-brain}"
TRANSPORT="${ILLO_DEPLOY_TRANSPORT:-ssh}"
MODE="${ILLO_DEPLOY_MODE:-stream}"
WAIT_FOR_HEALTH="1"
HEALTH_TIMEOUT_SECONDS="${ILLO_DEPLOY_HEALTH_TIMEOUT_SECONDS:-600}"
HEALTH_URL="${ILLO_DEPLOY_HEALTH_URL:-http://127.0.0.1:8000/api/health/ready}"

usage() {
  cat <<'USAGE'
Usage: ./ops/deploy-remote.sh [options]

Deploy Illo Brain on a remote server.

Options:
  --stream       Run deploy in the background, stream live logs, and exit after readiness. Default.
  --attach       Run ./ops/deploy.sh attached, matching the manual SSH flow.
  --detached     Run ./ops/deploy.sh with nohup and return after readiness.
  --no-wait      In stream/detached mode, do not wait for readiness.
  --host HOST    Remote host. Required unless ILLO_DEPLOY_HOST is set.
  --user USER    Remote user. Default: illo
  --dir PATH     Remote checkout dir. Default: illo-brain relative to the remote home.
  --transport ssh|tailscale
                 Use plain ssh or tailscale ssh. Default: ssh.
  --health-url URL
                 Override readiness URL. Default: http://127.0.0.1:8000/api/health/ready
  -h, --help     Show this help.

Environment overrides:
  ILLO_DEPLOY_HOST, ILLO_DEPLOY_USER, ILLO_DEPLOY_DIR,
  ILLO_DEPLOY_TRANSPORT, ILLO_DEPLOY_MODE,
  ILLO_DEPLOY_HEALTH_TIMEOUT_SECONDS, ILLO_DEPLOY_HEALTH_URL
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stream)
      MODE="stream"
      shift
      ;;
    --attach)
      MODE="attach"
      shift
      ;;
    --detached)
      MODE="detached"
      shift
      ;;
    --no-wait)
      WAIT_FOR_HEALTH="0"
      shift
      ;;
    --host)
      REMOTE_HOST="${2:?--host requires a value}"
      shift 2
      ;;
    --user)
      REMOTE_USER="${2:?--user requires a value}"
      shift 2
      ;;
    --dir)
      REMOTE_DIR="${2:?--dir requires a value}"
      shift 2
      ;;
    --transport)
      TRANSPORT="${2:?--transport requires a value}"
      shift 2
      ;;
    --health-url)
      HEALTH_URL="${2:?--health-url requires a value}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$TRANSPORT" != "ssh" && "$TRANSPORT" != "tailscale" ]]; then
  echo "Unsupported transport: $TRANSPORT (expected ssh or tailscale)" >&2
  exit 2
fi

if [[ "$MODE" != "stream" && "$MODE" != "attach" && "$MODE" != "detached" ]]; then
  echo "Unsupported mode: $MODE (expected stream, attach, or detached)" >&2
  exit 2
fi

if [[ -z "$REMOTE_HOST" ]]; then
  echo "Missing remote host. Pass --host HOST or set ILLO_DEPLOY_HOST." >&2
  usage >&2
  exit 2
fi

TARGET="${REMOTE_USER}@${REMOTE_HOST}"
REMOTE_DIR_Q="$(printf '%q' "$REMOTE_DIR")"
HEALTH_TIMEOUT_Q="$(printf '%q' "$HEALTH_TIMEOUT_SECONDS")"
HEALTH_URL_Q="$(printf '%q' "$HEALTH_URL")"

ssh_run() {
  local command="$1"
  case "$TRANSPORT" in
    ssh)
      ssh -tt "$TARGET" "$command"
      ;;
    tailscale)
      tailscale ssh "$TARGET" "$command"
      ;;
  esac
}

remote_sync_main_cmd() {
  cat <<'REMOTE_SYNC'
echo "Syncing remote checkout to origin/main..."
git fetch origin main
git checkout -f -B main origin/main
git reset --hard origin/main
echo "Remote checkout now at $(git rev-parse --short HEAD)"
REMOTE_SYNC
}

echo "Deploying Illo Brain on $TARGET"
echo "Remote checkout: $REMOTE_DIR"
echo "Transport: $TRANSPORT"
echo "Mode: $MODE"
if [[ "$MODE" != "attach" && "$WAIT_FOR_HEALTH" == "1" ]]; then
  echo "Readiness URL: $HEALTH_URL"
fi
echo

if [[ "$MODE" == "attach" ]]; then
  remote_cmd="set -euo pipefail; cd $REMOTE_DIR_Q; $(remote_sync_main_cmd); exec ./ops/deploy.sh"
  ssh_run "$remote_cmd"
  exit $?
fi

remote_cmd=$(cat <<REMOTE
set -euo pipefail
cd $REMOTE_DIR_Q
$(remote_sync_main_cmd)
mkdir -p logs
log_path="\$PWD/logs/illo-remote-deploy.log"
pid_path="\$PWD/logs/illo-remote-deploy.pid"
health_url=$HEALTH_URL_Q
health_timeout=$HEALTH_TIMEOUT_Q
deploy_done=0
echo "Starting ./ops/deploy.sh in the background..."
: >"\$log_path"
nohup ./ops/deploy.sh >"\$log_path" 2>&1 < /dev/null &
deploy_pid=\$!
echo "\$deploy_pid" > "\$pid_path"
echo "Remote deploy process: \$deploy_pid"
echo "Remote log: \$log_path"
REMOTE
)

if [[ "$MODE" == "stream" ]]; then
  remote_cmd+=$(cat <<'REMOTE'

echo "Streaming remote deploy log until readiness passes..."
touch "$log_path"
tail -n +1 -F "$log_path" &
tail_pid=$!
cleanup_tail() {
  kill "$tail_pid" >/dev/null 2>&1 || true
  wait "$tail_pid" >/dev/null 2>&1 || true
}
trap cleanup_tail EXIT
REMOTE
)
fi

if [[ "$WAIT_FOR_HEALTH" == "1" ]]; then
  remote_cmd+=$(cat <<REMOTE

set -euo pipefail
deadline=\$((SECONDS + health_timeout))
while (( SECONDS < deadline )); do
  if curl -fsS "\$health_url" >/tmp/illo-deploy-health.json 2>/tmp/illo-deploy-health.err; then
    echo
    echo "Readiness check passed:"
    cat /tmp/illo-deploy-health.json
    echo
    exit 0
  fi
  if [ "\$deploy_done" = "0" ] && ! kill -0 "\$deploy_pid" 2>/dev/null; then
    set +e
    wait "\$deploy_pid"
    status=\$?
    set -e
    deploy_done=1
    if [ "\$status" -ne 0 ]; then
      echo "Deploy process failed before readiness passed (exit \$status)." >&2
      echo "Last readiness error:" >&2
      cat /tmp/illo-deploy-health.err >&2 || true
      echo "Last deploy log lines:" >&2
      tail -n 120 "\$log_path" >&2 || true
      exit "\$status"
    fi
    echo "Deploy process finished; waiting for readiness..."
  fi
  sleep 2
done
echo "Readiness check did not pass within $HEALTH_TIMEOUT_Q seconds." >&2
echo "Last readiness response:" >&2
cat /tmp/illo-deploy-health.json >&2 || true
cat /tmp/illo-deploy-health.err >&2 || true
echo "Last deploy log lines:" >&2
tail -n 120 "\$log_path" >&2 || true
exit 1
REMOTE
)
fi

ssh_run "$remote_cmd"

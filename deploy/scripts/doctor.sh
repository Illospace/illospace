#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_DIR="$ROOT/deploy/compose"
COMPOSE_FILE="$COMPOSE_DIR/docker-compose.yml"
ENV_FILE="${ILLO_COMPOSE_ENV_FILE:-$COMPOSE_DIR/.env}"

# Provides compose() plus the shared stack invariants.
source "$SCRIPT_DIR/compose-runtime-lib.sh"
source "$SCRIPT_DIR/agent-run-queue-health-lib.sh"

# Doctor runs after a deploy, so it holds the updater to the same standard as
# the always-on services; a bare monitor does not (a missing updater does not
# make Illo inert).
DOCTOR_REQUIRED_SERVICES="postgres api web worker scheduler updater"

errors=0
warnings=0
runtime_checks=1
tmp_running=""
strict_credentials=0
check_app_url=0

usage() {
  cat <<'EOF'
Usage: ./illo deploy doctor [--strict-credentials] [--check-app-url] [--no-runtime]
       deploy/scripts/doctor.sh [--strict-credentials] [--check-app-url] [--no-runtime]

Validates the Compose deployment environment, rendered Compose config, and
running service health when the stack is up.

Provider credentials for production self-hosting should be added inside
Illospace after first boot, where they are encrypted and stored in Postgres.
The strict credential check verifies those DB-backed credentials, not env vars.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --strict-credentials)
      strict_credentials=1
      shift
      ;;
    --check-app-url)
      check_app_url=1
      shift
      ;;
    --no-runtime)
      runtime_checks=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

cleanup() {
  [ -z "$tmp_running" ] || rm -f "$tmp_running"
}
trap cleanup EXIT

fail() {
  errors=$((errors + 1))
  echo "ERROR: $*" >&2
}

warn() {
  warnings=$((warnings + 1))
  echo "WARN:  $*" >&2
}

pass() {
  echo "OK:    $*"
}

need_command() {
  if command -v "$1" >/dev/null 2>&1; then
    pass "$1 is installed"
  else
    fail "$1 is not installed"
  fi
}

need_command docker
need_command python3

if [ ! -f "$ENV_FILE" ]; then
  fail "missing $ENV_FILE; run deploy/scripts/init-secrets.sh"
else
  pass "found $ENV_FILE"
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
  [ "$check_app_url" = "0" ] || export ILLO_CHECK_APP_URL=1
fi

for key in ILLO_PUBLIC_URL SECRET_KEY VAULT_MASTER_KEY DB_NAME DB_USER DB_PASSWORD; do
  value="${!key:-}"
  if [ -z "$value" ]; then
    fail "$key is empty in $ENV_FILE"
  elif [[ "$value" == *example.com* ]]; then
    warn "$key still uses an example value"
  else
    pass "$key is set"
  fi
done

if [ -n "${ILLO_PUBLIC_URL:-}" ]; then
  if python3 - "$ILLO_PUBLIC_URL" <<'PY'
import sys
from urllib.parse import urlparse

url = urlparse(sys.argv[1])
if url.scheme not in {"http", "https"} or not url.netloc:
    sys.exit(1)
PY
  then
    pass "ILLO_PUBLIC_URL is a valid absolute URL"
  else
    fail "ILLO_PUBLIC_URL must be an absolute http(s) URL"
  fi
fi

if [ -n "${ANTHROPIC_API_KEY:-}${OPENAI_API_KEY:-}${GEMINI_API_KEY:-}${EMBEDDING_API_KEY:-}" ]; then
  warn "provider API keys are present in $ENV_FILE; production credentials should be added inside Illospace so they are encrypted in Postgres"
fi

if [ -n "${VAULT_MASTER_KEY:-}" ]; then
  if python3 - "$VAULT_MASTER_KEY" <<'PY'
import base64
import sys

raw = sys.argv[1].encode("ascii")
try:
    decoded = base64.urlsafe_b64decode(raw)
except Exception:
    sys.exit(1)
if len(decoded) != 32:
    sys.exit(1)
PY
  then
    pass "VAULT_MASTER_KEY has Fernet-compatible shape"
  else
    fail "VAULT_MASTER_KEY must be a urlsafe base64-encoded 32-byte key"
  fi
fi

if [ "$runtime_checks" = "1" ]; then
  docker_info_output=""
  if docker_info_output="$(docker info 2>&1 >/dev/null)"; then
    pass "Docker daemon is reachable"
  else
    fail "Docker CLI is installed, but the Docker daemon/socket is not reachable"
    if [ -n "$docker_info_output" ]; then
      warn "docker info said: $(printf '%s' "$docker_info_output" | head -n 1)"
    fi
    runtime_checks=0
  fi
else
  warn "runtime checks disabled"
fi

if compose config >/dev/null; then
  pass "Compose configuration renders"
else
  fail "Compose configuration failed to render"
fi

if [ "$strict_credentials" = "1" ] && [ "$runtime_checks" = "0" ]; then
  fail "strict credential checks require a running Compose stack; remove --no-runtime and run after first boot"
fi

container_health() {
  local service="$1"
  local id
  id="$(compose ps -q "$service" 2>/dev/null || true)"
  [ -n "$id" ] || return 1
  docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null
}

check_db_provider_credentials() {
  local count
  if count="$(compose exec -T postgres psql -U "${DB_USER:-}" -d "${DB_NAME:-}" -tAc "
SELECT
  COALESCE((SELECT count(*) FROM org_api_keys), 0) +
  COALESCE((SELECT count(*) FROM vault_config WHERE key LIKE 'runtime_memory_api_key_%' AND value <> ''), 0);
" 2>/dev/null | tr -d '[:space:]')"; then
    if [[ "$count" =~ ^[0-9]+$ ]] && [ "$count" -gt 0 ]; then
      pass "DB-backed provider credentials are configured ($count key record(s))"
    elif [ "$strict_credentials" = "1" ]; then
      fail "no DB-backed provider credentials found; add provider credentials in Illospace System/Access after first boot"
    else
      warn "no DB-backed provider credentials found yet; add provider credentials in Illospace System/Access after first boot"
    fi
  elif [ "$strict_credentials" = "1" ]; then
    fail "could not inspect DB-backed provider credentials; check migrations and Postgres logs"
  else
    warn "could not inspect DB-backed provider credentials; skipping credential inventory"
  fi
}

if [ "$runtime_checks" = "0" ]; then
  :
elif tmp_running="$(mktemp "${TMPDIR:-/tmp}/illospace-compose-running.XXXXXX")" && compose ps --services --status running >"$tmp_running" 2>/dev/null; then
  running="$(cat "$tmp_running")"
  if printf '%s\n' "$running" | grep -qx api; then
    # Presence is the shared stack invariant; this loop only grades the health of
    # services that are present. Keeping one owner for presence means a monitor
    # and a deploy cannot disagree about whether the stack is inert.
    inert_status=0
    assert_stack_not_inert $DOCTOR_REQUIRED_SERVICES || inert_status=$?
    if [ "$inert_status" -eq 0 ]; then
      pass "every always-on service is running"
    else
      fail "Compose stack is missing an always-on service; see the report above"
    fi

    if printf '%s\n' "$running" | grep -qx worker; then
      worker_lifecycle_status=0
      assert_worker_not_drained || worker_lifecycle_status=$?
      if [ "$worker_lifecycle_status" -eq 0 ]; then
        pass "worker lifecycle phase is not drained"
      else
        fail "worker cannot claim new AgentRuns; see the lifecycle report above"
      fi
    fi

    for service in $DOCTOR_REQUIRED_SERVICES; do
      printf '%s\n' "$running" | grep -qx "$service" || continue
      health="$(container_health "$service" || true)"
      case "$health" in
        healthy|running)
          pass "$service service is $health"
          ;;
        starting)
          warn "$service service is still starting"
          ;;
        *)
          fail "$service service health is ${health:-unknown}"
          ;;
      esac
    done

    queue_health_output=""
    queue_health_status=0
    queue_health_output="$(assert_agent_run_queue_not_starved 2>&1)" || queue_health_status=$?
    if [ "$queue_health_status" -eq 0 ]; then
      pass "$queue_health_output"
    else
      fail "$queue_health_output"
    fi

    if printf '%s\n' "$running" | grep -qx postgres; then
      if compose exec -T postgres psql -U "${DB_USER:-}" -d "${DB_NAME:-}" -tAc "SELECT extname FROM pg_extension WHERE extname = 'vector'" 2>/dev/null | grep -qx vector; then
        pass "pgvector extension is installed"
      else
        fail "pgvector extension is missing; check migration logs"
      fi
      check_db_provider_credentials
    fi

    if [ "${ILLO_DOCTOR_SKIP_LOCAL_HTTP_PROBES:-0}" = "1" ]; then
      warn "skipping local HTTP probes; this doctor is running outside the host network namespace"
    elif command -v curl >/dev/null 2>&1; then
      api_port="${ILLO_API_PORT:-8000}"
      web_port="${ILLO_WEB_PORT:-8080}"
      if curl -fsS "http://127.0.0.1:${api_port}/api/health/live" >/dev/null; then
        pass "API liveness probe passed on 127.0.0.1:${api_port}"
      else
        fail "API liveness probe failed on 127.0.0.1:${api_port}"
      fi
      if curl -fsS "http://127.0.0.1:${api_port}/api/health/ready" >/dev/null; then
        pass "API readiness probe passed on 127.0.0.1:${api_port}"
      else
        fail "API readiness probe failed on 127.0.0.1:${api_port}"
      fi
      if curl -fsS "http://127.0.0.1:${web_port}/api/health/live" >/dev/null; then
        pass "web entrypoint API proxy passed on 127.0.0.1:${web_port}"
      else
        fail "web entrypoint API proxy failed on 127.0.0.1:${web_port}"
      fi
      if curl -fsS "http://127.0.0.1:${web_port}/" >/dev/null; then
        pass "web entrypoint dashboard passed on 127.0.0.1:${web_port}"
      else
        fail "web entrypoint dashboard failed on 127.0.0.1:${web_port}"
      fi
      if [ "${ILLO_CHECK_APP_URL:-0}" = "1" ]; then
        if curl -fsS "${ILLO_PUBLIC_URL%/}/api/health/live" >/dev/null; then
          pass "configured app URL liveness probe passed"
        else
          fail "configured app URL liveness probe failed at ${ILLO_PUBLIC_URL%/}/api/health/live"
        fi
      fi
    else
      warn "curl is not installed; skipping API HTTP probes"
    fi
  else
    warn "Compose stack is not running yet; skipping API HTTP probes"
    if [ "$strict_credentials" = "1" ]; then
      fail "strict credential checks require the Compose stack to be running"
    fi
  fi
else
  warn "Could not inspect Compose services; skipping runtime probes"
  if [ "$strict_credentials" = "1" ]; then
    fail "strict credential checks require Compose service inspection"
  fi
fi

if [ "$errors" -gt 0 ]; then
  echo
  echo "Illospace server doctor failed with $errors error(s) and $warnings warning(s)." >&2
  exit 1
fi

echo
echo "Illospace server doctor passed with $warnings warning(s)."

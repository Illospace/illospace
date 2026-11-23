#!/usr/bin/env bash
# Shared paths and commands keep each maintenance script on the same install.
set -euo pipefail
umask 077
DEPLOY_DIR=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ILLO_ROOT=$(CDPATH='' cd -- "$DEPLOY_DIR/.." && pwd)
export ILLO_ENV_FILE=${ILLO_ENV_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/illo/env}

fail() { printf '%s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || fail "Install $1, then run this command again."; }
config_get() { python3 "$DEPLOY_DIR/config.py" "$ILLO_ENV_FILE" "$1"; }

# Give status 1 for a relative path, or for a path inside the checkout or another folder.
# Callers in a condition must use `|| return 1`: errexit is off there.
outside_checkout() {
    [[ "$1" = /* ]] || { printf 'Use an absolute path outside the checkout.\n' >&2; return 1; }
    python3 - "$@" "$ILLO_ROOT" <<'PY'
import sys
from pathlib import Path
path, *folders = (Path(p).resolve() for p in sys.argv[1:])
if any(path.is_relative_to(folder) for folder in folders):
    sys.exit("Keep secrets and backups outside the checkout and the workspace folder.")
PY
}

load_config() {
    need python3
    [ -f "$ILLO_ENV_FILE" ] || fail "Run deploy/install.sh first, or set ILLO_ENV_FILE."
    outside_checkout "$ILLO_ENV_FILE"
    export ILLO_STATE_DIR=${ILLO_STATE_DIR:-$(config_get ILLO_STATE_DIR)}
    ILLO_STATE_DIR=${ILLO_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/illo}
    export ILLO_STATE_DIR
    mkdir -p "$ILLO_STATE_DIR"
    export ILLO_PROJECT_NAME=${ILLO_PROJECT_NAME:-$(config_get ILLO_PROJECT_NAME)}
    ILLO_PROJECT_NAME=${ILLO_PROJECT_NAME:-illo}
    export ILLO_PROJECT_NAME
    export ILLO_BRAIN_ENV_FILE=${ILLO_BRAIN_ENV_FILE:-$(config_get ILLO_BRAIN_ENV_FILE)}
    ILLO_BRAIN_ENV_FILE=${ILLO_BRAIN_ENV_FILE:-${ILLO_ENV_FILE%/*}/brain.env}
    export ILLO_BRAIN_ENV_FILE
    export ILLO_REVISION=${ILLO_REVISION:-$(config_get ILLO_REVISION)}
    if [ -z "$ILLO_REVISION" ] && [ -f "$ILLO_STATE_DIR/last-good" ]; then
        ILLO_REVISION=$(cat "$ILLO_STATE_DIR/last-good")
    fi
    ILLO_REVISION=${ILLO_REVISION:-local}
    export ILLO_REVISION
    export ILLO_IMAGE_PREFIX=${ILLO_IMAGE_PREFIX:-$(config_get ILLO_IMAGE_PREFIX)}
    ILLO_IMAGE_PREFIX=${ILLO_IMAGE_PREFIX:-illo}
    export ILLO_IMAGE_PREFIX
    export ILLO_HEALTH_ATTEMPTS=${ILLO_HEALTH_ATTEMPTS:-$(config_get ILLO_HEALTH_ATTEMPTS)}
    ILLO_HEALTH_ATTEMPTS=${ILLO_HEALTH_ATTEMPTS:-60}
    export ILLO_HEALTH_ATTEMPTS
    export ILLO_HEALTH_INTERVAL=${ILLO_HEALTH_INTERVAL:-2}
}

# The app file holds no provider key. Only the brain container loads the brain file. The app
# mounts the workspace folder, so the brain file stays outside it too.
check_env_files() {
    [ -f "$ILLO_BRAIN_ENV_FILE" ] || { printf 'Create the brain environment file %s from deploy/brain.env.example.\n' "$ILLO_BRAIN_ENV_FILE" >&2; return 1; }
    local workspace resolved_workspace rc
    workspace=$(config_get ILLO_WORKSPACE_DIR) || return 1
    outside_checkout "$ILLO_BRAIN_ENV_FILE" ${workspace:+"$workspace"} || return 1
    # Compose strips a UTF-8 BOM and reads `KEY=v`, `KEY = v`, `KEY: v`, `export KEY=v`, and a
    # bare `KEY`. Refuse every provider prefix and reference in the file, then check Compose's
    # resolved environment and mount source without printing any values.
    if ! python3 - "$ILLO_ENV_FILE" <<'PY'
import re, sys
from pathlib import Path
pattern = re.compile(r"(^\s*(?:export\s+)?|\$\{?)(ILLO_PROVIDER_|OPENAI_|ANTHROPIC_)", re.I)
for line in Path(sys.argv[1]).read_text(encoding="utf-8-sig").splitlines():
    stripped = line.lstrip()
    if stripped.startswith("#"):
        continue
    if pattern.search(line):
        sys.exit(1)
PY
    then
        printf 'The app environment file %s holds a provider variable. Move each ILLO_PROVIDER_, OPENAI_, and ANTHROPIC_ variable to %s. See docs/install.md.\n' "$ILLO_ENV_FILE" "$ILLO_BRAIN_ENV_FILE" >&2
        return 1
    fi
    rc=0
    resolved_workspace=$(compose config --format json 2>/dev/null | python3 -c '
import json, sys
data = json.load(sys.stdin)
illo = data.get("services", {}).get("illo", {})
forbidden = ("ILLO_PROVIDER_", "OPENAI_", "ANTHROPIC_")
env = illo.get("environment") or {}
keys = env.keys() if isinstance(env, dict) else (e.split("=", 1)[0] for e in env)
if any(k.upper().startswith(forbidden) for k in keys):
    sys.exit(2)
for volume in illo.get("volumes") or []:
    if isinstance(volume, dict) and volume.get("target") == "/workspace" and volume.get("source"):
        print(volume["source"])
        break
') || rc=$?
    if [ "$rc" -ne 0 ]; then
        if [ "$rc" -eq 2 ]; then
            printf 'The app environment file %s holds a provider variable. Move each ILLO_PROVIDER_, OPENAI_, and ANTHROPIC_ variable to %s. See docs/install.md.\n' "$ILLO_ENV_FILE" "$ILLO_BRAIN_ENV_FILE" >&2
        else
            printf 'Docker Compose could not resolve %s. Check the environment files.\n' "${compose_file:-$DEPLOY_DIR/compose.yaml}" >&2
        fi
        return 1
    fi
    if [ -n "$resolved_workspace" ]; then
        outside_checkout "$ILLO_BRAIN_ENV_FILE" "$resolved_workspace" || return 1
    fi
}

use_saved_layout() {
    local good
    if [ -f "$ILLO_STATE_DIR/last-good" ]; then
        good=$(cat "$ILLO_STATE_DIR/last-good")
        if [ -f "$ILLO_STATE_DIR/revisions/$good/deploy/compose.yaml" ]; then
            compose_file="$ILLO_STATE_DIR/revisions/$good/deploy/compose.yaml"
        fi
    fi
}

compose() {
    docker compose --project-name "$ILLO_PROJECT_NAME" --env-file "$ILLO_ENV_FILE" \
        -f "${compose_file:-$DEPLOY_DIR/compose.yaml}" "$@"
}

compose_timeout() {
    local seconds=$1
    shift
    timeout --kill-after=5 "$seconds" docker compose --project-name "$ILLO_PROJECT_NAME" --env-file "$ILLO_ENV_FILE" \
        -f "${compose_file:-$DEPLOY_DIR/compose.yaml}" "$@"
}

sandbox_health() {
    # Saved layouts from older revisions have no sandbox service.
    if ! grep -q '^  sandbox:' "${compose_file:-$DEPLOY_DIR/compose.yaml}"; then
        return 0
    fi
    compose exec -T illo python -c \
        'import json,urllib.request; assert json.load(urllib.request.urlopen("http://sandbox:8600/healthz",timeout=3))["status"]=="ok"' \
        >/dev/null 2>&1
}

wait_health() {
    local attempt
    for ((attempt=0; attempt<ILLO_HEALTH_ATTEMPTS; attempt++)); do
        if compose exec -T illo python -c \
            'import json,os,urllib.request; h=json.load(urllib.request.urlopen("http://127.0.0.1:8080/healthz",timeout=3)); assert h["status"]=="ok" and h["revision"]==os.environ["ILLO_REVISION"]' \
            >/dev/null 2>&1 && compose exec -T brain python -c \
            'import json,urllib.request; assert json.load(urllib.request.urlopen("http://127.0.0.1:8080/healthz",timeout=3))["status"]=="ok"' \
            >/dev/null 2>&1 && sandbox_health; then
            return 0
        fi
        sleep "$ILLO_HEALTH_INTERVAL"
    done
    return 1
}

record() {
    printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$ILLO_STATE_DIR/update.log"
}

save_deploy() {
    local revision=$1 destination="$ILLO_STATE_DIR/revisions/$1"
    [ -d "$destination/deploy" ] && return 0
    local temporary
    mkdir -p "$ILLO_STATE_DIR/revisions"
    temporary=$(mktemp -d "$ILLO_STATE_DIR/revisions/.deploy-XXXXXXXX")
    # Read the revision, even when the checkout still holds a failed update.
    if git -C "$ILLO_ROOT" archive "$revision" deploy | tar -x -C "$temporary"; then
        mv "$temporary" "$destination"
    else
        rm -rf "$temporary"
        return 1
    fi
}

save_good() {
    save_deploy "$ILLO_REVISION" || return 1
    printf '%s\n' "$ILLO_REVISION" > "$ILLO_STATE_DIR/last-good.tmp"
    mv "$ILLO_STATE_DIR/last-good.tmp" "$ILLO_STATE_DIR/last-good"
}

lock_deploy() {
    need flock
    exec 9>"$ILLO_STATE_DIR/deploy.lock"
    flock -n 9 || fail 'Another install, update, backup, or restore is active.'
}

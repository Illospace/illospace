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

outside_checkout() {
    [[ "$1" = /* ]] || fail 'Use an absolute path outside the checkout.'
    python3 - "$ILLO_ROOT" "$1" <<'PY'
import sys
from pathlib import Path
root, path = (Path(p).resolve() for p in sys.argv[1:])
if path.is_relative_to(root):
    sys.exit("Keep secrets and backups outside the checkout.")
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

compose() {
    docker compose --project-name "$ILLO_PROJECT_NAME" --env-file "$ILLO_ENV_FILE" \
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

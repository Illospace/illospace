#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=deploy/common.sh
source "$(dirname -- "$0")/common.sh"
self=$(cd -- "$(dirname -- "$0")" && pwd)/$(basename -- "$0")
args=("$@")
load_config
need git
need docker
need flock
cd "$ILLO_ROOT"
branch=$(config_get ILLO_DEPLOY_BRANCH)
branch=${branch:-main}
git check-ref-format --branch "$branch" >/dev/null || fail 'ILLO_DEPLOY_BRANCH is invalid.'
[ "$(git symbolic-ref --short HEAD)" = "$branch" ] || fail 'Check out ILLO_DEPLOY_BRANCH before an update.'
interval=0
if [ "${1:-}" = --watch ]; then
    interval=${2:-300}
    [[ "$interval" =~ ^[0-9]+$ ]] && [ "$interval" -gt 0 ] || fail 'Use --watch with a positive number of seconds.'
elif [ "$#" -ne 0 ]; then
    fail 'Use deploy/update.sh [--watch seconds].'
fi
[ -f "$ILLO_STATE_DIR/last-good" ] || fail 'Run deploy/install.sh before the first update.'

rollback() {
    local compose_file="$ILLO_STATE_DIR/revisions/$1/deploy/compose.yaml"
    export ILLO_REVISION=$1
    export ILLO_MIGRATE_ON_START=0
    if compose up -d --no-build --remove-orphans >/dev/null 2>&1 && wait_health; then
        record "rollback $ILLO_REVISION ok"
    else
        record "rollback $ILLO_REVISION FAILED; check the services"
        printf 'The rollback needs a check. Run deploy/status.sh.\n' >&2
    fi
}

update_once() {
    local old target stage before
    old=$(cat "$ILLO_STATE_DIR/last-good")
    if [ -n "$(git status --porcelain)" ]; then
        record 'failed: the checkout has local changes'
        return 1
    fi
    # Bootstrap the saved layout for installs made before snapshots existed.
    save_deploy "$old" || return 1
    if ! git fetch origin "+refs/heads/$branch:refs/remotes/origin/$branch"; then
        record 'failed: git fetch'
        return 1
    fi
    target=$(git rev-parse "refs/remotes/origin/$branch")
    [ "$target" != "$old" ] || return 0
    before=$(git rev-parse HEAD)
    if ! git merge-base --is-ancestor HEAD "$target" || ! git merge --ff-only "$target"; then
        record "failed $target: fast-forward"
        return 1
    fi
    # The fast-forward can change this script and its list of images. A running shell keeps the old text.
    # Start the new script one time for this target. The checkout is then at the target.
    if [ "$before" != "$target" ] && [ "${ILLO_UPDATE_RESTARTED_FOR:-}" != "$target" ]; then
        export ILLO_UPDATE_RESTARTED_FOR=$target
        exec "$self" ${args[@]+"${args[@]}"}
    fi
    export ILLO_REVISION=$target
    export ILLO_MIGRATE_ON_START=0
    stage=build
    if compose build illo brain sandbox; then
        stage=database
        if compose up -d --no-build --wait postgres; then
            stage=migration
            if compose run --rm --no-deps -T illo migrate; then
                stage=start
                if compose up -d --no-build --remove-orphans; then
                    stage=health
                    if wait_health && save_good; then
                        record "updated $target ok"
                        printf 'Illo now runs %s.\n' "$target"
                        return 0
                    fi
                fi
            fi
        fi
    fi
    record "failed $target: $stage; last good $old"
    rollback "$old"
    return 1
}

if [ "$interval" -eq 0 ]; then
    lock_deploy
    update_once
    exit $?
fi

# Release the shared lock during each wait so backups can run.
exec 9>"$ILLO_STATE_DIR/deploy.lock"
while :; do
    if flock -n 9; then
        update_once || true
        flock -u 9
    fi
    sleep "$interval"
done

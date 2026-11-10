#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=deploy/common.sh
source "$(dirname -- "$0")/common.sh"
load_config
need git
need docker
lock_deploy
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
    export ILLO_REVISION=$1
    export ILLO_MIGRATE_ON_START=0
    if compose up -d --no-build --no-deps --force-recreate illo brain >/dev/null 2>&1 && wait_health; then
        record "rollback $ILLO_REVISION ok"
    else
        record "rollback $ILLO_REVISION FAILED; check the services"
        printf 'The rollback needs a check. Run deploy/status.sh.\n' >&2
    fi
}

update_once() {
    local old target stage
    old=$(cat "$ILLO_STATE_DIR/last-good")
    if [ -n "$(git status --porcelain)" ]; then
        record 'failed: the checkout has local changes'
        return 1
    fi
    if ! git fetch origin "+refs/heads/$branch:refs/remotes/origin/$branch"; then
        record 'failed: git fetch'
        return 1
    fi
    target=$(git rev-parse "refs/remotes/origin/$branch")
    [ "$target" != "$old" ] || return 0
    if ! git merge-base --is-ancestor HEAD "$target" || ! git merge --ff-only "$target"; then
        record "failed $target: fast-forward"
        return 1
    fi
    export ILLO_REVISION=$target
    export ILLO_MIGRATE_ON_START=0
    stage=build
    if compose build illo brain; then
        stage=migration
        if compose run --rm --no-deps -T illo migrate; then
            stage=start
            if compose up -d --no-build --no-deps --force-recreate illo brain; then
                stage=health
                if wait_health; then
                    save_good
                    record "updated $target ok"
                    printf 'Illo now runs %s.\n' "$target"
                    return 0
                fi
            fi
        fi
    fi
    record "failed $target: $stage; last good $old"
    rollback "$old"
    return 1
}

while :; do
    result=0
    update_once || result=$?
    [ "$interval" -gt 0 ] || exit "$result"
    sleep "$interval"
done

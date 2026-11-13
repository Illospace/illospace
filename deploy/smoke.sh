#!/usr/bin/env bash
# Build and check a separate install. Never use a team's environment file.
set -euo pipefail
smoke_port=${ILLO_SMOKE_PORT:-18484}
# A test must not inherit a team's credentials or maintenance settings.
for variable in "${!ILLO_@}" POSTGRES_PASSWORD ANTHROPIC_API_KEY ANTHROPIC_BASE_URL OPENAI_API_KEY OPENAI_BASE_URL; do
    unset "$variable"
done
# shellcheck source=deploy/common.sh
source "$(dirname -- "$0")/common.sh"
need docker
need python3
tmp=$(mktemp -d)
export ILLO_ENV_FILE=$tmp/env
export ILLO_WORKSPACE_DIR=$tmp/workspace
export ILLO_STATE_DIR=$tmp/state
export ILLO_BACKUP_DIR=$tmp/backups
export ILLO_PROJECT_NAME
ILLO_PROJECT_NAME=illo-test-$(python3 -c 'import secrets; print(secrets.token_hex(5))')
export ILLO_IMAGE_PREFIX=$ILLO_PROJECT_NAME
export ILLO_PORT=$smoke_port
export ILLO_BRAIN_RUNTIME=echo
export ILLO_REVISION
ILLO_REVISION=$(git -C "$ILLO_ROOT" rev-parse HEAD)
python3 "$DEPLOY_DIR/init-env.py" "$ILLO_ENV_FILE" "$DEPLOY_DIR/env.example" </dev/null
cp -R "$ILLO_ROOT/workspace.example" "$ILLO_WORKSPACE_DIR"
# Make the temporary parent traversable for non-root container users.
chmod 755 "$tmp"
load_config
cleanup() {
    local result=$?
    trap - EXIT
    if [ "$result" -ne 0 ]; then
        compose logs --no-color --tail 60 illo brain sandbox >&2 || true
    fi
    compose down --volumes --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$tmp"
    exit "$result"
}
trap cleanup EXIT
compose build illo brain sandbox
compose up -d
wait_health || fail 'The deployment smoke test did not become healthy.'
compose exec -T illo illo doctor
compose exec -T illo python - < "$DEPLOY_DIR/smoke-check.py"
"$DEPLOY_DIR/backup.sh"
backup=$(find "$ILLO_BACKUP_DIR" -name '*.dump' -type f -print -quit)
compose exec -T postgres createdb -U illo illo_restore_smoke
compose exec -T postgres pg_restore -U illo -d illo_restore_smoke --exit-on-error --single-transaction < "$backup"
[ "$(compose exec -T postgres psql -U illo -d illo_restore_smoke -Atc 'SELECT count(*) FROM people')" -gt 0 ]
printf 'The database backup and restore passed.\n'

printf 'The deployment smoke test passed on revision %s.\n' "$ILLO_REVISION"

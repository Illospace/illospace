#!/usr/bin/env bash
# Build and check a separate install. Never use a team's environment file.
set -euo pipefail
smoke_port=${ILLO_SMOKE_PORT:-18484}
# A test must not inherit a team's credentials or maintenance settings.
for variable in "${!ILLO_@}" "${!OPENAI_@}" "${!ANTHROPIC_@}" POSTGRES_PASSWORD; do
    unset "$variable"
done
# shellcheck source=deploy/common.sh
source "$(dirname -- "$0")/common.sh"
need docker
need python3
tmp=$(mktemp -d)
export ILLO_ENV_FILE=$tmp/env
export ILLO_BRAIN_ENV_FILE=$tmp/brain.env
export ILLO_WORKSPACE_DIR=$tmp/workspace
export ILLO_STATE_DIR=$tmp/state
export ILLO_BACKUP_DIR=$tmp/backups
export ILLO_PROJECT_NAME
ILLO_PROJECT_NAME=illo-test-$(python3 -c 'import secrets; print(secrets.token_hex(5))')
export ILLO_IMAGE_PREFIX=$ILLO_PROJECT_NAME
export ILLO_PORT=$smoke_port
export ILLO_REVISION
ILLO_REVISION=$(git -C "$ILLO_ROOT" rev-parse HEAD)
python3 "$DEPLOY_DIR/init-env.py" "$ILLO_ENV_FILE" "$DEPLOY_DIR/env.example" \
    "$ILLO_BRAIN_ENV_FILE" "$DEPLOY_DIR/brain.env.example" </dev/null
# The fake provider makes no model request. Every route uses it.
printf 'ILLO_PROVIDER_FAKE=1\n' >> "$ILLO_BRAIN_ENV_FILE"
cp -R "$ILLO_ROOT/workspace.example" "$ILLO_WORKSPACE_DIR"
python3 - "$ILLO_WORKSPACE_DIR/illo.yaml" <<'PY'
import re, sys
path = sys.argv[1]
text = open(path).read()
text, count = re.subn(r"default: \{provider: chatgpt, model: [^,]+,", "default: {provider: fake, model: fake,", text)
assert count == 1, "The example has no default route on chatgpt"
# No route uses chatgpt now, so its payer policy goes too.
text, count = re.subn(r"\n  chatgpt:\n(    .*\n)+", "\n", text)
assert count == 1, "The example has no brain.chatgpt block"
open(path, "w").write(text)
PY
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

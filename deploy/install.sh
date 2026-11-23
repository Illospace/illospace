#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=deploy/common.sh
source "$(dirname -- "$0")/common.sh"
need python3
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || fail 'Install Python 3.9 or later.'
need docker
docker info >/dev/null 2>&1 || fail 'Docker is unavailable. Start Docker and give this user access.'
if ! docker compose version >/dev/null 2>&1; then
    answer=no
    if [ -t 0 ]; then
        read -r -p 'Install the official Docker Compose plugin for this user? [y/N] ' answer
    fi
    case "$answer" in
        y|Y|yes) "$DEPLOY_DIR/install-compose.sh" ;;
        *) fail 'Run deploy/install-compose.sh, then run this command again.' ;;
    esac
fi
outside_checkout "$ILLO_ENV_FILE"
python3 "$DEPLOY_DIR/init-env.py" "$ILLO_ENV_FILE" "$DEPLOY_DIR/env.example"
chmod 600 "$ILLO_ENV_FILE"
load_config
lock_deploy
workspace=$(config_get ILLO_WORKSPACE_DIR)
[ -n "$workspace" ] && [[ "$workspace" = /* ]] || fail 'Set an absolute ILLO_WORKSPACE_DIR.'
if [ ! -e "$workspace" ]; then
    mkdir -p "$(dirname -- "$workspace")"
    cp -R "$ILLO_ROOT/workspace.example" "$workspace"
fi
if [ ! -f "$ILLO_STATE_DIR/last-good" ]; then
    [ -f "$ILLO_ROOT/brain/Dockerfile" ] || fail 'The checkout needs brain/Dockerfile. Use the branch with the complete rebuild.'
    ILLO_REVISION=$(git -C "$ILLO_ROOT" rev-parse HEAD)
    export ILLO_REVISION
    compose build illo brain sandbox
    compose up -d
    wait_health || fail 'Illo did not become healthy. Check docker compose logs with the same environment file.'
    compose exec -T illo illo doctor
    save_good
    record "installed $ILLO_REVISION"
else
    wait_health || fail 'The existing install is not healthy. Run deploy/status.sh.'
    compose exec -T illo illo doctor
fi
printf 'Illo is ready. The environment file is %s.\n' "$ILLO_ENV_FILE"
printf 'The workspace folder is %s.\n' "$workspace"
cat <<'TEXT'
Next steps:
1. Create a Slack app from deploy/slack/manifest.yaml. Add its tokens to the environment file.
2. Set the team members in people.yaml in the workspace folder.
3. Add ANTHROPIC_API_KEY for Claude, or select openai_compat and add its API key and base URL.
4. Recreate the services after an environment change. See docs/install.md.
5. Create a token for the first agent. See docs/install.md.
TEXT

#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=deploy/common.sh
source "$(dirname -- "$0")/common.sh"
load_config
printf 'Last good revision: %s\n' "$ILLO_REVISION"
status=0
if ! compose exec -T illo python -c \
    'import json,urllib.request; h=json.load(urllib.request.urlopen("http://127.0.0.1:8080/healthz",timeout=3)); print("Running revision: "+h["revision"]); print("Health: "+h["status"])'; then
    printf 'Health: unavailable\n'
    status=1
fi
printf 'Last update result: '
if [ -f "$ILLO_STATE_DIR/update.log" ]; then
    tail -n 1 "$ILLO_STATE_DIR/update.log"
else
    printf 'No update yet.\n'
fi
exit "$status"

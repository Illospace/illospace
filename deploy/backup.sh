#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=deploy/common.sh
source "$(dirname -- "$0")/common.sh"
load_config
lock_deploy
backup_dir=$(config_get ILLO_BACKUP_DIR)
[ -n "$backup_dir" ] && [[ "$backup_dir" = /* ]] || fail 'Set an absolute ILLO_BACKUP_DIR outside the checkout.'
outside_checkout "$backup_dir"
keep=$(config_get ILLO_BACKUP_KEEP)
keep=${keep:-14}
[[ "$keep" =~ ^[0-9]+$ ]] && [ "$keep" -gt 0 ] || fail 'ILLO_BACKUP_KEEP must be a positive integer.'
mkdir -p "$backup_dir"
backup=$(mktemp "$backup_dir/illo-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX.dump")
trap 'rm -f "${backup}.partial"' EXIT
mv "$backup" "$backup.partial"
compose exec -T postgres pg_dump -U illo -d illo --format=custom > "$backup.partial"
[ -s "$backup.partial" ] || fail 'The database backup is empty.'
mv "$backup.partial" "$backup"
chmod 600 "$backup"
python3 - "$backup_dir" "$keep" <<'PY'
import sys
from pathlib import Path
backups = sorted(Path(sys.argv[1]).glob("illo-*.dump"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
for path in backups[int(sys.argv[2]):]:
    path.unlink()
PY
printf 'Backup: %s (%s bytes)\n' "$backup" "$(wc -c < "$backup" | tr -d ' ')"

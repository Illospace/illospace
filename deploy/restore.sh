#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=deploy/common.sh
source "$(dirname -- "$0")/common.sh"
[ "$#" -eq 2 ] && [ "$2" = --yes ] || fail 'Use deploy/restore.sh <file> --yes. The database must be fresh.'
[ -f "$1" ] && [ -r "$1" ] || fail 'The backup file is unavailable.'
backup=$1
load_config
lock_deploy
compose up -d --wait postgres
# Do not replace a live database. A fresh volume has an empty public schema.
tables=$(compose exec -T postgres psql -U illo -d illo -Atc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema')")
[ "$tables" = 0 ] || fail 'The database is not fresh. Restore into a new Compose project and volume.'
compose stop illo brain
compose exec -T postgres pg_restore -U illo -d illo --exit-on-error --single-transaction < "$backup"
printf 'The database is restored. Restore its vault key and workspace, then start Illo.\n'

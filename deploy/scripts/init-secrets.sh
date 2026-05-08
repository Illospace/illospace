#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_DIR="$ROOT/deploy/compose"
ENV_FILE="$COMPOSE_DIR/.env"
EXAMPLE_FILE="$COMPOSE_DIR/.env.production.example"

DOMAIN=""
EMAIL=""
PUBLIC_URL=""

usage() {
  cat <<'EOF'
Usage: ./illo deploy init [--domain team.example.com] [--email admin@example.com] [--public-url https://team.example.com] [--env-file path]

Creates or updates deploy/compose/.env from .env.production.example.
Existing non-empty values are preserved. Missing secrets are generated.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --domain)
      DOMAIN="${2:-}"
      shift 2
      ;;
    --email)
      EMAIL="${2:-}"
      shift 2
      ;;
    --public-url)
      PUBLIC_URL="${2:-}"
      shift 2
      ;;
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ ! -f "$EXAMPLE_FILE" ]; then
  echo "Missing template: $EXAMPLE_FILE" >&2
  exit 1
fi

if [ -z "$ENV_FILE" ]; then
  echo "--env-file requires a path" >&2
  exit 2
fi

case "$ENV_FILE" in
  /*) ;;
  *) ENV_FILE="$ROOT/$ENV_FILE" ;;
esac

if [ ! -f "$ENV_FILE" ]; then
  mkdir -p "$(dirname "$ENV_FILE")"
  cp "$EXAMPLE_FILE" "$ENV_FILE"
fi

python3 - "$ENV_FILE" "$DOMAIN" "$EMAIL" "$PUBLIC_URL" <<'PY'
from __future__ import annotations

import base64
import os
import re
import secrets
import stat
import sys
from pathlib import Path

env_path = Path(sys.argv[1])
domain_arg = sys.argv[2].strip()
email_arg = sys.argv[3].strip()
public_url_arg = sys.argv[4].strip()

assignment = re.compile(r"^(?P<key>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def shell_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]*", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


lines = env_path.read_text(encoding="utf-8").splitlines()
current: dict[str, str] = {}
for line in lines:
    match = assignment.match(line.strip())
    if match:
        current[match.group("key")] = unquote(match.group("value"))

updates: dict[str, str] = {}

if not current.get("COMPOSE_PROJECT_NAME"):
    updates["COMPOSE_PROJECT_NAME"] = "illospace"
if not current.get("DB_NAME"):
    updates["DB_NAME"] = "illospace"
if not current.get("DB_USER"):
    updates["DB_USER"] = "illospace"
if not current.get("DB_PASSWORD"):
    updates["DB_PASSWORD"] = secrets.token_urlsafe(32)
if not current.get("SECRET_KEY"):
    updates["SECRET_KEY"] = secrets.token_urlsafe(64)
if not current.get("VAULT_MASTER_KEY"):
    updates["VAULT_MASTER_KEY"] = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")

if domain_arg:
    updates["ILLO_DOMAIN"] = domain_arg
    if not current.get("ILLO_PUBLIC_URL") or current.get("ILLO_PUBLIC_URL") == "https://team.example.com":
        updates["ILLO_PUBLIC_URL"] = f"https://{domain_arg}"
if email_arg:
    updates["ILLO_ADMIN_EMAIL"] = email_arg
if public_url_arg:
    updates["ILLO_PUBLIC_URL"] = public_url_arg

seen: set[str] = set()
next_lines: list[str] = []
for line in lines:
    match = assignment.match(line.strip())
    if not match:
        next_lines.append(line)
        continue
    key = match.group("key")
    seen.add(key)
    if key in updates:
        next_lines.append(f"{key}={shell_quote(updates[key])}")
    else:
        next_lines.append(line)

for key in sorted(set(updates) - seen):
    next_lines.append(f"{key}={shell_quote(updates[key])}")

env_path.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")
env_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

print(f"Updated {env_path}")
for key in ("ILLO_DOMAIN", "ILLO_ADMIN_EMAIL", "ILLO_PUBLIC_URL"):
    value = updates.get(key) or current.get(key) or ""
    if value in {"", "team.example.com", "admin@example.com", "https://team.example.com"}:
        print(f"TODO: set {key} in {env_path}")
PY

echo
echo "Next:"
echo "  ./illo deploy up"
echo "  ./illo deploy doctor"

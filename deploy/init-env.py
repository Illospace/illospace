#!/usr/bin/env python3
"""Create boot settings outside the checkout without printing secrets."""

import base64
import getpass
import os
import secrets
import sys
from pathlib import Path

path, template = map(Path, sys.argv[1:])
path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
password = secrets.token_hex(24)
values = {
    "POSTGRES_PASSWORD": password,
    "ILLO_DATABASE_URL": f"postgresql://illo:{password}@postgres:5432/illo",
    "ILLO_VAULT_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
    "ILLO_BRAIN_SECRET": secrets.token_hex(32),
    "ILLO_UID": str(os.getuid()),
    "ILLO_GID": str(os.getgid()),
    "ILLO_WORKSPACE_DIR": str(Path.home() / ".local/share/illo/workspace"),
    "ILLO_STATE_DIR": str(Path.home() / ".local/state/illo"),
    "ILLO_BACKUP_DIR": str(Path.home() / ".local/share/illo/backups"),
}
for key in ("ILLO_WORKSPACE_DIR", "ILLO_STATE_DIR", "ILLO_BACKUP_DIR", "ILLO_DEPLOY_BRANCH", "ILLO_BRAIN_RUNTIME"):
    if os.environ.get(key):
        values[key] = os.environ[key]
if sys.stdin.isatty():
    print("Slack is optional. Press Enter to add its tokens later.")
    values["ILLO_SLACK_BOT_TOKEN"] = getpass.getpass("Slack bot token: ")
    values["ILLO_SLACK_APP_TOKEN"] = getpass.getpass("Slack app token: ")
for value in values.values():
    if any(char in value for char in "\r\n'"):
        sys.exit("Use single-line values without a single quote.")
lines = []
for line in template.read_text().splitlines():
    key = line.partition("=")[0]
    lines.append(f"{key}='{values[key]}'" if key in values else line)
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    sys.exit(0)
with os.fdopen(fd, "w") as output:
    output.write("\n".join(lines) + "\n")

"""Small commands for an install, its tokens, and its vault."""

import argparse
import asyncio
import getpass
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import uvicorn
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import ValidationError

from illo.config import Config
from illo.db import migrate, one
from illo.ledger import ask_workflow, people, tokens
from illo.log import configure
from illo.vault import Vault, delete, get_names
from illo.workspace import load_workspace


def parser() -> argparse.ArgumentParser:
    """Secrets have no value argument, so shell history cannot capture them."""
    result = argparse.ArgumentParser(prog="illo")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    archive = commands.add_parser("import-archive")
    archive.add_argument("file")
    archive.add_argument("--dry-run", action="store_true")
    commands.add_parser("doctor")
    commands.add_parser("stats")
    commands.add_parser("reload")
    commands.add_parser("routines").add_subparsers(dest="action", required=True).add_parser("list")
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    mcp = commands.add_parser("mcp").add_subparsers(dest="action", required=True)
    check = mcp.add_parser("check")
    check.add_argument("--url", required=True)
    check.add_argument("--token-file", type=Path, required=True)
    token = commands.add_parser("token").add_subparsers(dest="action", required=True)
    create = token.add_parser("create")
    create.add_argument("--person", required=True)
    create.add_argument("--label", required=True)
    create.add_argument("--scope", choices=("collector", "agent"), default="collector")
    listing = token.add_parser("list")
    listing.add_argument("--person")
    token.add_parser("revoke").add_argument("id", type=int)
    slack = commands.add_parser("slack").add_subparsers(dest="action", required=True)
    slack.add_parser("whoami")
    slack.add_parser("users")
    secret = commands.add_parser("secret").add_subparsers(dest="action", required=True)
    secret.add_parser("set").add_argument("name")
    secret.add_parser("get-names")
    secret.add_parser("delete").add_argument("name")
    connector = commands.add_parser("connector").add_subparsers(dest="action", required=True)
    login = connector.add_parser("login")
    login.add_argument("name")
    login.add_argument("--person")
    status = commands.add_parser("connectors").add_subparsers(dest="action", required=True)
    status.add_parser("status")
    commands.add_parser("chatgpt").add_subparsers(dest="action", required=True).add_parser("status")
    return result


async def doctor(config: Config) -> int:
    """Report missing requirements without exposing credentials."""
    failed = False
    try:
        load_workspace(config.workspace_dir)
        print("The workspace is valid.")
    except ValueError as exc:
        print(f"The workspace needs a fix: {exc}")
        failed = True
    try:
        async with await AsyncConnection.connect(
            config.database_url, row_factory=dict_row, connect_timeout=3
        ) as conn:
            await one(conn, "SELECT 1")
            schema = await one(conn, "SELECT to_regclass('public.schema_migrations') AS name")
            if not schema["name"]:
                print("The database needs migrations. Run: illo migrate")
                failed = True
            else:
                print("The database is ready.")
    except Exception:
        print("The database is unavailable. Check ILLO_DATABASE_URL and the database service.")
        failed = True
    if config.vault_key:
        try:
            Vault(config.vault_key.get_secret_value())
            print("The vault key is valid.")
        except ValueError:
            print("ILLO_VAULT_KEY must be a Fernet key.")
            failed = True
    else:
        print("The vault has no key. Set ILLO_VAULT_KEY before you store a secret.")
    if not config.slack_bot_token:
        print("Slack is optional and has no bot token.")
    return int(failed)


async def execute(args, config: Config) -> int:
    """Run commands in one connection and one transaction unless migrations own it."""
    if args.command == "slack":
        from illo.channels.slack.cli import execute as slack_execute

        return await slack_execute(args.action, config)
    if args.command == "mcp":
        from illo.channels.mcp.check import check

        return await check(args.url, args.token_file)
    if args.command == "doctor":
        return await doctor(config)
    if args.command == "reload":
        if not config.reload_token:
            raise ValueError("Set ILLO_RELOAD_TOKEN before a reload")
        if urlsplit(config.public_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Workspace reload needs a loopback ILLO_PUBLIC_URL")
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.post(
                config.public_url.rstrip("/") + "/admin/reload",
                headers={"Authorization": f"Bearer {config.reload_token.get_secret_value()}"},
            )
            response.raise_for_status()
        print("The workspace was reloaded.")
        return 0
    async with await AsyncConnection.connect(
        config.database_url, row_factory=dict_row, connect_timeout=5
    ) as conn:
        if args.command == "migrate":
            applied = await migrate(conn)
            print(f"Applied {len(applied)} migration(s).")
        elif args.command == "import-archive":
            from illo.sources.archive import import_archive

            async with conn.transaction(force_rollback=args.dry_run):
                await load_workspace(config.workspace_dir).sync_people(conn)
                counts = await import_archive(conn, args.file, dry_run=args.dry_run)
            print(("Dry run. " if args.dry_run else "") + counts.text())

        elif args.command == "routines":
            from illo.routines.scheduler import list_routines

            for row in await list_routines(conn, load_workspace(config.workspace_dir)):
                print(
                    f"{row['name']}  {row['next_run_at'] or 'not scheduled'}  {row['last_status']}"
                )
        elif args.command == "chatgpt":
            from illo.ledger.chatgpt import status

            for row in await status(conn):
                state = (
                    "broken"
                    if row["broken_at"]
                    else "connected"
                    if row["expires_at"] and not row["revoked_at"]
                    else "missing"
                )
                print(f"{row['person']}  {state}  {row['expires_at'] or 'no expiry'}")
        elif args.command == "stats":
            counts = await ask_workflow.stats(conn)
            print("Cards sent for each person and day:")
            for row in counts["cards"]:
                print(f"{row['person']}  {row['day']}  {row['cards_sent']}")
            print("Asks answered by a rule:")
            for row in counts["rules"]:
                print(f"{row['person']}  {row['answered_by_rule']}")
        elif args.command in {"connector", "connectors"}:
            from illo.connectors.login import execute as connector_execute

            await connector_execute(conn, args, config)
        elif args.command == "token":
            if args.action == "create":
                await load_workspace(config.workspace_dir).sync_people(conn)
                person = await people.get(conn, args.person)
                if not person:
                    raise ValueError("The person is missing or inactive")
                print(await tokens.create(conn, person["id"], args.label, scopes=[args.scope]))
            elif args.action == "list":
                person = await people.get(conn, args.person) if args.person else None
                if args.person and not person:
                    raise ValueError("The person is missing or inactive")
                for row in await tokens.list_tokens(conn, person["id"] if person else None):
                    state = "revoked" if row["revoked_at"] else "active"
                    print(f"{row['id']}  {row['label']}  {','.join(row['scopes'])}  {state}")
            elif args.action == "revoke":
                print(
                    "The token was revoked."
                    if await tokens.revoke(conn, args.id)
                    else "No active token has that ID."
                )
        elif args.command == "secret":
            if args.action == "get-names":
                for name in await get_names(conn):
                    print(name)
            elif args.action == "delete":
                print(
                    "The secret was deleted."
                    if await delete(conn, args.name)
                    else "No secret has that name."
                )
            else:
                if not config.vault_key:
                    raise ValueError("Set ILLO_VAULT_KEY before you store a secret")
                value = (
                    getpass.getpass("Secret value: ")
                    if sys.stdin.isatty()
                    else sys.stdin.read().rstrip("\r\n")
                )
                if not value:
                    raise ValueError("The secret value is empty")
                await Vault(config.vault_key.get_secret_value()).set(conn, args.name, value)
                print("The secret was stored.")
    return 0


def main() -> None:
    """Use a nonzero exit status when a command cannot finish."""
    args = parser().parse_args()
    configure()
    try:
        config = Config.from_env()
        if args.command == "serve":
            from illo.app import create_app

            uvicorn.run(create_app(config), host=args.host, port=args.port)
            return
        result = asyncio.run(execute(args, config))
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, e["loc"])) for e in exc.errors())
        print(f"The configuration has invalid fields: {fields}", file=sys.stderr)
        result = 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        result = 1
    except Exception:
        print("The command failed. Check the configuration and the service.", file=sys.stderr)
        result = 1
    raise SystemExit(result)

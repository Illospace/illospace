"""Run the self-hosted Slack Socket Mode connector."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from brain.systems.slack.connector import (
    SlackConnectorConfig,
    run_forever,
    validate_slack_connector_tokens,
)


def _redacted(value: str) -> str:
    text = str(value or "")
    if len(text) <= 8:
        return "***"
    return f"{text[:6]}...{text[-4:]}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m brain.app.cli.slack_connector",
        description="Run the self-hosted Illo Slack Socket Mode connector.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate environment configuration and print a redacted summary without opening Socket Mode.",
    )
    return parser


async def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.check:
        config = await SlackConnectorConfig.from_runtime()
        validate_slack_connector_tokens(
            bot_token=config.bot_token,
            app_token=config.app_token,
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "bot_token": _redacted(config.bot_token),
                    "app_token": _redacted(config.app_token),
                    "org_id": config.org_id,
                    "owner_user_id": config.owner_user_id,
                    "team_id": config.team_id,
                    "bot_user_id": config.bot_user_id,
                },
                indent=2,
            )
        )
        return 0
    await run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))

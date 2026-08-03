"""Interactive Google login bootstrap for Playwright storage state."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Sequence

DEFAULT_STORAGE_STATE = Path("google-storage-state.json")
DEFAULT_AUTH_URL = "https://accounts.google.com/"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Open Chromium for Google login and save meetbot storage state."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_STORAGE_STATE,
        help=f"Storage-state output path (default: {DEFAULT_STORAGE_STATE})",
    )
    parser.add_argument(
        "--auth-url",
        default=DEFAULT_AUTH_URL,
        help=f"Initial login page (default: {DEFAULT_AUTH_URL})",
    )
    return parser


async def bootstrap_auth(output: Path, auth_url: str) -> None:
    """Run a local headed login and save the resulting browser state."""

    from playwright.async_api import async_playwright

    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(auth_url, wait_until="domcontentloaded")
        await asyncio.to_thread(
            input,
            "Complete Google sign-in in Chromium, then press Enter here to save auth state: ",
        )
        await context.storage_state(path=str(output))
        output.chmod(0o600)
        await browser.close()
    print(f"Saved Google storage state to {output}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    asyncio.run(bootstrap_auth(args.output, args.auth_url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

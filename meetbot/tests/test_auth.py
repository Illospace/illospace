from __future__ import annotations

from pathlib import Path

from meetbot.auth import DEFAULT_AUTH_URL, build_parser


def test_auth_cli_defaults_to_a_local_copyable_storage_state() -> None:
    args = build_parser().parse_args([])

    assert args.output == Path("google-storage-state.json")
    assert args.auth_url == DEFAULT_AUTH_URL

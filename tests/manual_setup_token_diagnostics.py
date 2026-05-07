"""
Live setup-token diagnostics for Anthropic transport changes.

Run locally, without the frontend:
    python3 tests/manual_setup_token_diagnostics.py

Optional env vars:
    ILLO_TEST_ANTHROPIC_TOKEN   Use this token instead of loading the latest DB token
    ILLO_TEST_DB_HOST           Defaults to 127.0.0.1
    ILLO_TEST_DB_PORT           Defaults to 5432
    ILLO_TEST_DB_NAME           Defaults to illo_memory
    ILLO_TEST_DB_USER           Defaults to illo
    ILLO_TEST_DB_PASSWORD       Defaults to illo

The shared adapter already writes exact request/response dumps to:
    /tmp/anthropic-debug/request_*.json
    /tmp/anthropic-debug/response_*.json
This script prints the new dump paths created by each hypothesis.
"""

from __future__ import annotations

import glob
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import anthropic
import psycopg2

sys.path.insert(0, ".")

from brain.platform.integrations.anthropic_adapter import build_auth_adapter, get_oauth_betas
from brain.systems.vault import _decrypt

DEBUG_DIR = Path("/tmp/anthropic-debug")
MODEL = "claude-sonnet-4-6"


@dataclass
class Case:
    label: str
    kwargs: dict
    client_mode: str
    default_headers: dict[str, str] | None = None


def _load_token() -> str:
    env_token = os.environ.get("ILLO_TEST_ANTHROPIC_TOKEN", "").strip()
    if env_token:
        return env_token

    conn = psycopg2.connect(
        host=os.environ.get("ILLO_TEST_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("ILLO_TEST_DB_PORT", "5432")),
        dbname=os.environ.get("ILLO_TEST_DB_NAME", "illo_memory"),
        user=os.environ.get("ILLO_TEST_DB_USER", "illo"),
        password=os.environ.get("ILLO_TEST_DB_PASSWORD", "illo"),
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT encrypted_key
                FROM user_api_keys
                WHERE provider = 'anthropic' AND is_active = TRUE
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise RuntimeError("No active anthropic token found in user_api_keys")
    return _decrypt(bytes(row[0]))


def _make_client(token: str, mode: str, default_headers: dict[str, str] | None = None):
    if mode == "adapter":
        adapter = build_auth_adapter(token, timeout=30)
        return adapter.client, dict(adapter.extra_headers)
    return anthropic.Anthropic(
        api_key=None,
        auth_token=token,
        timeout=30,
        default_headers=default_headers,
    ), {}


def _base_messages() -> list[dict]:
    return [{"role": "user", "content": "Say hello in exactly one word."}]


def _tool_defs() -> list[dict]:
    return [{
        "name": "echo_tool",
        "description": "Echo a string.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }]


def _adaptive_kwargs(*, with_tools: bool) -> dict:
    kwargs = {
        "model": MODEL,
        "max_tokens": 4096,
        "messages": _base_messages(),
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium"},
    }
    if with_tools:
        kwargs["tools"] = _tool_defs()
    return kwargs


def _enabled_kwargs(*, with_tools: bool) -> dict:
    kwargs = {
        "model": MODEL,
        "max_tokens": 4096,
        "messages": _base_messages(),
        "thinking": {"type": "enabled", "budget_tokens": 1024},
    }
    if with_tools:
        kwargs["tools"] = _tool_defs()
    return kwargs


def _minimal_kwargs() -> dict:
    return {
        "model": MODEL,
        "max_tokens": 32,
        "messages": _base_messages(),
    }


def _build_cases() -> list[Case]:
    core_betas = "claude-code-20250219,oauth-2025-04-20"
    full_betas = ",".join(get_oauth_betas())
    return [
        Case(
            label="plain_auth_token_no_headers",
            client_mode="sdk",
            default_headers=None,
            kwargs=_minimal_kwargs(),
        ),
        Case(
            label="plain_auth_token_x_app_default_headers",
            client_mode="sdk",
            default_headers={"x-app": "cli"},
            kwargs=_minimal_kwargs(),
        ),
        Case(
            label="plain_auth_token_core_betas_extra_headers",
            client_mode="sdk",
            default_headers={"x-app": "cli"},
            kwargs={**_minimal_kwargs(), "extra_headers": {"anthropic-beta": core_betas}},
        ),
        Case(
            label="plain_auth_token_full_betas_extra_headers",
            client_mode="sdk",
            default_headers={"x-app": "cli"},
            kwargs={**_minimal_kwargs(), "extra_headers": {"anthropic-beta": full_betas}},
        ),
        Case(
            label="shared_adapter_minimal",
            client_mode="adapter",
            kwargs=_minimal_kwargs(),
        ),
        Case(
            label="shared_adapter_adaptive_no_tools",
            client_mode="adapter",
            kwargs=_adaptive_kwargs(with_tools=False),
        ),
        Case(
            label="shared_adapter_adaptive_tools",
            client_mode="adapter",
            kwargs=_adaptive_kwargs(with_tools=True),
        ),
        Case(
            label="shared_adapter_enabled_no_tools",
            client_mode="adapter",
            kwargs=_enabled_kwargs(with_tools=False),
        ),
        Case(
            label="shared_adapter_enabled_tools",
            client_mode="adapter",
            kwargs=_enabled_kwargs(with_tools=True),
        ),
    ]


def _status_from_exc(exc: Exception) -> str:
    if isinstance(exc, anthropic.AuthenticationError):
        return "401"
    if isinstance(exc, anthropic.BadRequestError):
        return "400"
    if isinstance(exc, anthropic.InternalServerError):
        return "500"
    code = getattr(exc, "status_code", None)
    return str(code) if code is not None else type(exc).__name__


def _new_debug_files(before: set[str]) -> list[str]:
    current = set(glob.glob(str(DEBUG_DIR / "*.json")))
    return sorted(current - before)


def run_case(token: str, case: Case) -> tuple[str, str, list[str]]:
    before = set(glob.glob(str(DEBUG_DIR / "*.json")))
    client, adapter_headers = _make_client(token, case.client_mode, case.default_headers)
    kwargs = dict(case.kwargs)
    extra_headers = dict(kwargs.pop("extra_headers", {}) or {})
    extra_headers.update(adapter_headers)
    if extra_headers:
        kwargs["extra_headers"] = extra_headers
    try:
        response = client.messages.create(**kwargs)
        request_id = getattr(response, "_request_id", "") or "ok"
        return "PASS", request_id, _new_debug_files(before)
    except Exception as exc:
        return _status_from_exc(exc), str(exc)[:220], _new_debug_files(before)


def main() -> int:
    token = _load_token()
    print(f"SDK version: {anthropic.__version__}")
    print(f"Token prefix: {token[:18]}...")
    print(f"Token suffix: ...{token[-40:]}")
    print(f"Debug dir: {DEBUG_DIR}")
    print()

    results: list[tuple[str, str, str, list[str]]] = []
    for case in _build_cases():
        print(f"=== {case.label} ===")
        status, detail, debug_files = run_case(token, case)
        print(f"status: {status}")
        print(f"detail: {detail}")
        if debug_files:
            print("debug:")
            for path in debug_files[-4:]:
                print(f"  {path}")
        else:
            print("debug: none")
        print()
        results.append((case.label, status, detail, debug_files))
        time.sleep(1)

    print("=== summary ===")
    for label, status, detail, _debug_files in results:
        print(f"{label}: {status}  {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

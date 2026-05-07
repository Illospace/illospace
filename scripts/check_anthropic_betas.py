#!/usr/bin/env python3
"""Manual Anthropic beta-header probe.

This is an operator diagnostic, not an automated pytest test. It calls the
live Anthropic API using the local Claude OAuth token.
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

import anthropic


def _get_token():
    from brain.systems.auth.claude_oauth import get_access_token

    token, method = get_access_token()
    return token, method


def make_client(token: str, betas: list[str]):
    headers = {"x-app": "cli"}
    if betas:
        headers["anthropic-beta"] = ",".join(betas)
    return anthropic.Anthropic(
        api_key=None,
        auth_token=token,
        timeout=30,
        default_headers=headers,
    )


def run_beta_case(token: str, label: str, betas: list[str], use_thinking: bool = True) -> None:
    client = make_client(token, betas)
    kwargs = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 16000 if use_thinking else 100,
        "messages": [{"role": "user", "content": "Say hi in one word."}],
    }
    if use_thinking:
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": 5000}

    results = []
    for _ in range(3):
        try:
            client.messages.create(**kwargs)
            results.append("PASS")
        except anthropic.BadRequestError:
            results.append("400")
        except anthropic.InternalServerError:
            results.append("500")
        except anthropic.AuthenticationError:
            results.append("401")
        except Exception as exc:
            results.append(f"ERR:{type(exc).__name__}")
        time.sleep(1)

    print(f"  {label}: {', '.join(results)}  betas={betas}")


def main() -> None:
    print(f"SDK version: {anthropic.__version__}")

    token, method = _get_token()
    print(f"Token source: {method}")

    print("\n=== With thinking ===")
    run_beta_case(
        token,
        "current",
        ["claude-code-20250219", "oauth-2025-04-20", "prompt-caching-2024-07-31"],
    )
    run_beta_case(
        token,
        "+interleaved",
        [
            "claude-code-20250219",
            "oauth-2025-04-20",
            "prompt-caching-2024-07-31",
            "interleaved-thinking-2025-05-14",
        ],
    )
    run_beta_case(
        token,
        "+output128k",
        [
            "claude-code-20250219",
            "oauth-2025-04-20",
            "prompt-caching-2024-07-31",
            "interleaved-thinking-2025-05-14",
            "output-128k-2025-02-19",
        ],
    )

    print("\n=== Without thinking (baseline) ===")
    run_beta_case(
        token,
        "no_thinking",
        ["claude-code-20250219", "oauth-2025-04-20"],
        use_thinking=False,
    )


if __name__ == "__main__":
    main()

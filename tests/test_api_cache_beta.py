"""
Test: does cache_control require the prompt-caching beta header?
Run: python3 tests/test_api_cache_beta.py
"""
import os
import sys
import time
sys.path.insert(0, ".")

import anthropic
import pytest
from brain.platform.integrations.llm import resolve_llm_client
from brain.platform.integrations.anthropic_adapter import is_oauth_token

print(f"SDK version: {anthropic.__version__}")

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="requires live Anthropic credentials",
)


def _call(label, client, **kwargs):
    try:
        r = client.messages.create(**kwargs)
        print(f"  {label}: PASS ({r.stop_reason})")
        return True
    except Exception as e:
        code = getattr(e, 'status_code', '?')
        print(f"  {label}: {code} — {str(e)[:150]}")
        return False


def main():
    llm = resolve_llm_client()
    client = llm.client
    token = getattr(client, 'auth_token', None) or getattr(client, 'api_key', None)
    print(f"Token: {token[:18]}...")
    print(f"OAuth: {is_oauth_token(token)}")

    # Base kwargs (no cache_control)
    base_msg = [{"role": "user", "content": "Say hi."}]
    base_system = [{"type": "text", "text": "You are helpful."}]

    # With cache_control on system prompt
    cached_system = [{"type": "text", "text": "You are helpful.", "cache_control": {"type": "ephemeral"}}]

    print("\n=== 1. No cache_control, current client (has claude-code + oauth betas) ===")
    _call("no_cache", client, model="claude-sonnet-4-6", max_tokens=100,
          messages=base_msg, system=base_system)
    time.sleep(2)

    print("\n=== 2. With cache_control on system, current client ===")
    _call("cache_current_betas", client, model="claude-sonnet-4-6", max_tokens=100,
          messages=base_msg, system=cached_system)
    time.sleep(2)

    print("\n=== 3. With cache_control, client WITH prompt-caching beta ===")
    betas_with_caching = ["claude-code-20250219", "oauth-2025-04-20", "prompt-caching-2024-07-31"]
    client_with_cache_beta = anthropic.Anthropic(
        api_key=None, auth_token=token, timeout=30,
        default_headers={"x-app": "cli", "anthropic-beta": ",".join(betas_with_caching)},
    )
    _call("cache_with_beta", client_with_cache_beta, model="claude-sonnet-4-6", max_tokens=100,
          messages=base_msg, system=cached_system)
    time.sleep(2)

    print("\n=== 4. With cache_control + thinking, current client ===")
    _call("cache+thinking", client, model="claude-sonnet-4-6", max_tokens=16000,
          messages=base_msg, system=cached_system,
          thinking={"type": "enabled", "budget_tokens": 5000})
    time.sleep(2)

    print("\n=== 5. With cache_control + thinking, WITH prompt-caching beta ===")
    _call("cache+thinking+beta", client_with_cache_beta, model="claude-sonnet-4-6", max_tokens=16000,
          messages=base_msg, system=cached_system,
          thinking={"type": "enabled", "budget_tokens": 5000})
    time.sleep(2)

    print("\n=== 6. Consistency check: cache+thinking+beta x3 ===")
    for i in range(3):
        _call(f"run_{i}", client_with_cache_beta, model="claude-sonnet-4-6", max_tokens=16000,
              messages=base_msg, system=cached_system,
              thinking={"type": "enabled", "budget_tokens": 5000})
        time.sleep(2)

    print("\n=== Done ===")


if __name__ == "__main__":
    main()

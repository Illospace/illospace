"""
Test hypotheses for the API 500 error — isolate variables.
Run on the server: python3 tests/test_api_500_hypothesis.py
"""
import os
import sys
import time
sys.path.insert(0, ".")

import anthropic
import pytest
from brain.platform.integrations.llm import resolve_llm_client
from brain.platform.integrations.anthropic_adapter import is_oauth_token as _is_oauth_token


pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="requires live Anthropic credentials",
)


def _call(label, client, **kwargs):
    """Make an API call and report result."""
    try:
        r = client.messages.create(**kwargs)
        print(f"  {label}: PASS ({r.stop_reason})")
        return True
    except anthropic.BadRequestError as e:
        print(f"  {label}: 400 — {str(e)[:120]}")
        return False
    except anthropic.InternalServerError as e:
        resp = getattr(e, 'response', None)
        hdrs = dict(getattr(resp, 'headers', {}) or {}) if resp else {}
        print(f"  {label}: 500 — retry={hdrs.get('x-should-retry','?')}, req={hdrs.get('x-request-id','?')}")
        return False
    except Exception as e:
        print(f"  {label}: {type(e).__name__} — {str(e)[:120]}")
        return False


def test_isolate_variables():
    """Isolate: is it thinking? tools? model? OAuth headers?"""
    client = resolve_llm_client().client
    token = getattr(client, 'auth_token', None) or getattr(client, 'api_key', None)
    print(f"Client token prefix: {token[:18] if token else 'NONE'}...")
    print(f"Is OAuth: {_is_oauth_token(token) if token else False}")
    print()

    simple_msg = [{"role": "user", "content": "Say hello in one word."}]
    tool_defs = [{"name": "test_tool", "description": "A test tool.",
                  "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}}]

    # 1. Simplest possible call — no thinking, no tools
    print("=== 1. No thinking, no tools ===")
    _call("medium", client, model="claude-sonnet-4-6", max_tokens=100, messages=simple_msg)
    time.sleep(1)

    # 2. With tools, no thinking
    print("\n=== 2. With tools, no thinking ===")
    _call("sonnet+tools", client, model="claude-sonnet-4-6", max_tokens=100,
          messages=simple_msg, tools=tool_defs)
    time.sleep(1)

    # 3. With thinking (OLD type=enabled), no tools
    print("\n=== 3. OLD thinking type=enabled (should fail) ===")
    _call("sonnet+enabled", client, model="claude-sonnet-4-6", max_tokens=16000,
          messages=simple_msg, thinking={"type": "enabled", "budget_tokens": 5000})
    time.sleep(1)

    # 4. With thinking (NEW type=adaptive), no tools
    print("\n=== 4. NEW thinking type=adaptive ===")
    _call("sonnet+adaptive", client, model="claude-sonnet-4-6", max_tokens=16000,
          messages=simple_msg, thinking={"type": "adaptive", "budget_tokens": 5000})
    time.sleep(1)

    # 5. Adaptive + tools (what coordinator should do now)
    print("\n=== 5. Adaptive + tools ===")
    _call("sonnet+adaptive+tools", client, model="claude-sonnet-4-6", max_tokens=16000,
          messages=simple_msg, thinking={"type": "adaptive", "budget_tokens": 5000}, tools=tool_defs)
    time.sleep(1)

    # 6. Opus + adaptive + tools
    print("\n=== 6. Opus + adaptive + tools ===")
    _call("opus+adaptive+tools", client, model="claude-opus-4-6", max_tokens=26384,
          messages=simple_msg, thinking={"type": "adaptive", "budget_tokens": 10000}, tools=tool_defs)
    time.sleep(1)

    # 7. Plain client (no OAuth headers)
    print("\n=== 7. Without OAuth beta headers ===")
    plain_client = anthropic.Anthropic(auth_token=token, timeout=30)
    _call("plain_client+adaptive+tools", plain_client, model="claude-sonnet-4-6",
          max_tokens=16000, messages=simple_msg,
          thinking={"type": "adaptive", "budget_tokens": 5000}, tools=tool_defs)
    time.sleep(1)

    # 8. Streaming + adaptive (what coordinator actually uses)
    print("\n=== 8. Streaming + adaptive ===")
    try:
        with client.messages.stream(
            model="claude-sonnet-4-6", max_tokens=16000, messages=simple_msg,
            thinking={"type": "adaptive", "budget_tokens": 5000}, tools=tool_defs,
        ) as stream:
            r = stream.get_final_message()
            print(f"  streaming+adaptive+tools: PASS ({r.stop_reason})")
    except Exception as e:
        print(f"  streaming+adaptive+tools: {type(e).__name__} — {str(e)[:120]}")


def test_rapid_fire():
    """Test if rapid successive calls trigger 500 (rate limiting hypothesis)."""
    client = resolve_llm_client().client
    simple_msg = [{"role": "user", "content": "Say hi."}]

    print("\n=== 9. Rapid fire — 5 calls with no delay ===")
    for i in range(5):
        _call(f"call_{i}", client, model="claude-sonnet-4-6", max_tokens=100, messages=simple_msg)


def test_replay_dump():
    """Replay actual dump if available."""
    import json
    import glob

    dumps = glob.glob("/tmp/agent_payload_turn1_*.json")
    if not dumps:
        print("\n=== 9. Replay dump === SKIPPED")
        return

    with open(dumps[0]) as f:
        kwargs = json.load(f)

    client = resolve_llm_client().client
    print(f"\n=== 9. Replay dump as-is ===")
    _call("dump_as_is", client, **kwargs)

    # Strip ALL cache_control
    import copy
    clean = copy.deepcopy(kwargs)
    for msg in clean["messages"]:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
    system = clean.get("system")
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict):
                block.pop("cache_control", None)

    print("\n=== 10. Replay dump — no cache_control anywhere ===")
    _call("dump_no_cache", client, **clean)


if __name__ == "__main__":
    test_isolate_variables()
    test_rapid_fire()
    test_replay_dump()
    print("\n=== All tests done ===")

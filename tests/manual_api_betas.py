"""
Test: which beta header combination works reliably?
Run: python3 tests/test_api_betas.py
"""
import sys
import time
sys.path.insert(0, ".")

import anthropic

print(f"SDK version: {anthropic.__version__}")

from brain.systems.auth.claude_oauth import get_access_token
token, method = get_access_token()
print(f"Token: {token[:18]}..., method: {method}")


def make_client(betas):
    headers = {"x-app": "cli"}
    if betas:
        headers["anthropic-beta"] = ",".join(betas)
    return anthropic.Anthropic(api_key=None, auth_token=token, timeout=30, default_headers=headers)


def test_betas(label, betas, use_thinking=True):
    client = make_client(betas)
    kwargs = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 16000 if use_thinking else 100,
        "messages": [{"role": "user", "content": "Say hi in one word."}],
    }
    if use_thinking:
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": 5000}

    results = []
    for i in range(3):
        try:
            r = client.messages.create(**kwargs)
            results.append("PASS")
        except anthropic.BadRequestError as e:
            results.append(f"400")
        except anthropic.InternalServerError:
            results.append("500")
        except anthropic.AuthenticationError:
            results.append("401")
        except Exception as e:
            results.append(f"ERR:{type(e).__name__}")
        time.sleep(1)

    print(f"  {label}: {', '.join(results)}  betas={betas}")


# Current (what we have now)
print("\n=== With thinking ===")
test_betas("current", ["claude-code-20250219", "oauth-2025-04-20", "prompt-caching-2024-07-31"])
time.sleep(1)

# Add interleaved-thinking
test_betas("+interleaved", ["claude-code-20250219", "oauth-2025-04-20", "prompt-caching-2024-07-31", "interleaved-thinking-2025-05-14"])
time.sleep(1)

# Add output-128k
test_betas("+output128k", ["claude-code-20250219", "oauth-2025-04-20", "prompt-caching-2024-07-31", "interleaved-thinking-2025-05-14", "output-128k-2025-02-19"])
time.sleep(1)

# Try using SDK's betas parameter instead of default_headers
print("\n=== Using SDK betas parameter ===")
client_sdk_betas = anthropic.Anthropic(api_key=None, auth_token=token, timeout=30)
for i in range(3):
    try:
        r = client_sdk_betas.messages.create(
            model="claude-sonnet-4-6", max_tokens=16000,
            messages=[{"role": "user", "content": "Say hi."}],
            thinking={"type": "enabled", "budget_tokens": 5000},
            extra_headers={
                "anthropic-beta": "claude-code-20250219,oauth-2025-04-20,prompt-caching-2024-07-31,interleaved-thinking-2025-05-14",
            },
        )
        print(f"  sdk_extra_headers run_{i}: PASS")
    except anthropic.InternalServerError:
        print(f"  sdk_extra_headers run_{i}: 500")
    except Exception as e:
        print(f"  sdk_extra_headers run_{i}: {type(e).__name__}: {str(e)[:100]}")
    time.sleep(1)

print("\n=== Without thinking (baseline) ===")
test_betas("no_thinking", ["claude-code-20250219", "oauth-2025-04-20"], use_thinking=False)

print("\n=== Done ===")

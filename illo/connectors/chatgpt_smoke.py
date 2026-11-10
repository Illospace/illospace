"""Check one person's model grant through the normal app-to-brain request."""

import asyncio
import re
import time
from contextlib import aclosing
from types import SimpleNamespace

import httpx

from illo.brain.client import BrainClient, RunRequest
from illo.brain.run_manager import provider_auth
from illo.connectors.chatgpt import ChatGPTClient, TemporaryRefresh
from illo.db import all_rows, create_pool
from illo.gateway.principal import Authority
from illo.workspace import load_workspace


def safe_model(value):
    return value if re.fullmatch(r"[A-Za-z0-9_./:-]{1,128}", value) else "unknown"


def http_status(value):
    return value if type(value) is int and 100 <= value <= 599 else "none"


async def smoke(config, person_id):
    started = time.monotonic()
    auth_kind, model, round_trip, usage = "none", "unknown", False, {}
    reason, status = "The smoke check did not finish", "none"
    try:
        workspace = load_workspace(config.workspace_dir)
        brain = workspace.config.brain
        brain.check_runtime(config.brain_runtime)
        runtime = config.brain_runtime or brain.runtime
        model = safe_model(config.brain_model or brain.model)
        if runtime != "openai_responses":
            raise ValueError("The smoke check needs openai_responses")
        async with create_pool(config.database_url, min_size=1, max_size=2) as pool:
            ctx = SimpleNamespace(
                config=config,
                workspace=workspace,
                pool=pool,
                chatgpt_client=ChatGPTClient(),
                slack_client=None,
            )
            async with pool.connection() as conn:
                people = await all_rows(
                    conn,
                    "SELECT id,key FROM people WHERE active AND (key=%s OR id::text=%s)",
                    (person_id, person_id),
                )
            if len(people) != 1:
                raise ValueError("The person is missing, inactive, or ambiguous")
            person = people[0]
            authority = Authority("person:" + person["key"], "person_dm", person["id"])
            auth = await provider_auth(ctx, authority, runtime)
            if auth is None:
                raise ValueError("The person has no usable ChatGPT grant. API fallback is off")
            auth_kind = auth.kind
            request = RunRequest(
                run_id=0,
                system_prompt="Call smoke_ping once. Then reply Ready.",
                user_message="Check the function tool.",
                mcp_url=config.public_url.rstrip("/") + "/brain/mcp",
                mcp_token="smoke-no-mcp-access",
                runtime=runtime,
                model=config.brain_model or brain.model,
                max_turns=2,
                timeout_s=brain.timeout_s,
                provider_auth=auth,
                api_key_fallback=brain.api_key_fallback,
                smoke=True,
            )
            async with (
                asyncio.timeout(brain.timeout_s),
                aclosing(BrainClient(config.brain_url).run(request)) as stream,
            ):
                async for item in stream:
                    if item.type == "error":
                        usage = item.usage
                        if item.api_key_fallback_failed:
                            auth_kind = "api_key"
                        status = http_status(getattr(item, "http_status", None))
                        reason = item.failure_reason()
                        break
                    if item.type == "final":
                        used = getattr(item, "auth_kind", auth_kind)
                        auth_kind = used if used in {"chatgpt", "api_key"} else "unknown"
                        round_trip = getattr(item, "tool_round_trip", False) is True
                        usage = item.usage
                        reason = "The function tool did not complete a round trip"
                        break
    except TemporaryRefresh:
        reason = "The sign-in service is unavailable. Try again in a minute"
    except ValueError as error:
        # Only local validation messages reach this path. Provider bodies never do.
        reason = (
            str(error)
            if str(error)
            in {
                "The smoke check needs openai_responses",
                "The person is missing, inactive, or ambiguous",
                "The person has no usable ChatGPT grant. API fallback is off",
            }
            else "The workspace or model sign-in needs a fix"
        )
    except httpx.HTTPStatusError as error:
        status = http_status(error.response.status_code)
        reason = "The brain service refused the request"
    except (TimeoutError, httpx.TimeoutException):
        reason = "The smoke check reached its time limit"
    except Exception:
        reason = "The brain service is unavailable"
    print(f"Auth kind: {auth_kind}")
    print(f"Model: {model}")
    print(f"Tool round trip: {'yes' if round_trip else 'no'}")
    counts = []
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(key)
        counts.append(f"{key}={value if type(value) is int and value >= 0 else 0}")
    print("Token usage: " + " ".join(counts))
    print(f"Latency: {time.monotonic() - started:.3f} s")
    if not round_trip:
        print(f"Failed: HTTP {status}; {reason}.")
    return 0 if round_trip else 1

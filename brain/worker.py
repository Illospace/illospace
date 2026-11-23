"""Read secrets from stdin. Never write request bodies or exception details."""

import asyncio
import json
import sys
from functools import partial
from pathlib import Path

from brain import loop
from brain.errors import failure_event
from brain.models import RunRequest, event
from brain.providers import Provider
from brain.wire import agent_sdk, chat, fake, responses

# One runner for each wire format. The provider names the wire.
RUNNERS = {
    "agent_sdk": agent_sdk.run,
    "chat": partial(loop.run, chat.Chat),
    "responses": partial(loop.run, responses.Responses),
    "fake": fake.run,
}


def provider(data: dict | None) -> Provider | None:
    return Provider(**{**data, "efforts": tuple(data["efforts"])}) if data else None


async def main() -> None:
    request = RunRequest.model_validate_json(sys.stdin.readline())
    providers = json.loads(sys.stdin.readline())
    selected, fallback = provider(providers["provider"]), provider(providers["fallback"])
    directory = Path(sys.argv[1])
    terminal = False
    try:
        async for item in RUNNERS[selected.wire](request, selected, directory, fallback=fallback):
            terminal = item["type"] in ("final", "error")
            print(json.dumps(item), flush=True)
            if terminal:
                break
        if not terminal:
            failure = event("error", category="internal", message="The runtime stopped without an answer")
            print(json.dumps(failure), flush=True)
    except Exception as error:
        print(json.dumps(failure_event(error)), flush=True)


if __name__ == "__main__":
    asyncio.run(main())

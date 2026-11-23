"""Read secrets from stdin. Never write request bodies or exception details."""

import asyncio
import json
import sys
from functools import partial
from pathlib import Path

from brain import loop
from brain.errors import failure_event
from brain.models import RunRequest, event
from brain.wire import agent_sdk, chat, fake, responses

RUNTIMES = {
    "claude": agent_sdk.run,
    "openai_compat": partial(loop.run, chat.Chat),
    "openai_responses": partial(loop.run, responses.Responses),
    "echo": fake.run,
}


async def main() -> None:
    request = RunRequest.model_validate_json(sys.stdin.readline())
    directory = Path(sys.argv[1])
    terminal = False
    try:
        async for item in RUNTIMES[request.runtime](request, directory):
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

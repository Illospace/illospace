"""Read secrets from stdin. Never write request bodies or exception details."""

import asyncio
import json
import sys
from pathlib import Path

import httpx

from brain.models import RunError, RunRequest, event
from brain.runtimes import claude, echo, openai_compat

RUNTIMES = {"claude": claude, "openai_compat": openai_compat, "echo": echo}


def error_category(error: BaseException) -> str:
    if isinstance(error, RunError):
        return error.category
    if isinstance(error, BaseExceptionGroup):
        categories = [error_category(child) for child in error.exceptions]
        return "auth" if "auth" in categories else "runtime"
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (401, 403):
        return "auth"
    return "runtime"


async def main() -> None:
    request = RunRequest.model_validate_json(sys.stdin.readline())
    directory = Path(sys.argv[1])
    terminal = False
    try:
        async for item in RUNTIMES[request.runtime].run(request, directory):
            terminal = item["type"] in ("final", "error")
            print(json.dumps(item), flush=True)
            if terminal:
                break
        if not terminal:
            print(
                json.dumps(
                    event(
                        "error", category="runtime", message="The runtime stopped without an answer"
                    )
                ),
                flush=True,
            )
    except RunError as error:
        print(json.dumps(event("error", category=error.category, message=str(error))), flush=True)
    except Exception as error:
        print(
            json.dumps(
                event("error", category=error_category(error), message="The brain runtime failed")
            ),
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(main())

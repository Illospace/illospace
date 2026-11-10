"""A process group gives the service one kill boundary for each run."""

import asyncio
import json
import os
import signal
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from tempfile import TemporaryDirectory

from brain.models import BrainEvent, RunRequest, event


async def run_worker(
    request: RunRequest, command: list[str] | None = None
) -> AsyncIterator[BrainEvent]:
    process = None
    with TemporaryDirectory(prefix="illo-brain-", dir=os.getenv("ILLO_BRAIN_TMPDIR")) as temporary:
        directory = Path(temporary)
        # The SDK and its children stay in this group. Never log their arguments.
        argv = command or [sys.executable, "-m", "brain.worker", str(directory)]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
                cwd=directory,
                limit=4 * 1024 * 1024,
            )
            process.stdin.write(json.dumps(request.wire()).encode() + b"\n")
            await process.stdin.drain()
            process.stdin.close()
            terminal = False
            while line := await process.stdout.readline():
                item = json.loads(line)
                terminal = item["type"] in ("final", "error")
                yield item
                if terminal:
                    break
            if not terminal:
                yield event(
                    "error",
                    category="runtime",
                    message="The brain worker stopped without an answer",
                )
        finally:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()

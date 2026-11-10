"""Discover the run's tools without a model request."""

import json
from collections.abc import AsyncIterator
from pathlib import Path

from brain.mcp_client import connect, list_tools
from brain.models import BrainEvent, RunRequest, event


async def run(request: RunRequest, directory: Path) -> AsyncIterator[BrainEvent]:
    async with connect(request) as session:
        names = [tool.name for tool in await list_tools(session)]
        yield event("started", catalog=names)
        yield event("final", text=json.dumps(names), usage={})

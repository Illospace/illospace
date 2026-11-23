"""The smoke check has one fixed test function. It reads and writes no team data."""

from mcp.types import CallToolResult, TextContent, Tool

from brain.models import RunError

TOOLS = [
    Tool(
        name="smoke_ping",
        description="Return Ready to test a function call.",
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    )
]


class SmokeSession:
    def __init__(self):
        self.calls = 0

    async def call_tool(self, name, arguments):
        if name != "smoke_ping" or arguments:
            raise RunError("runtime", "The smoke tool needs no arguments")
        self.calls += 1
        return CallToolResult(content=[TextContent(type="text", text="Ready.")])

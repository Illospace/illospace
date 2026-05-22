"""Architecture guardrails for the MCP protocol server."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MCP_SERVER = ROOT / "brain" / "app" / "mcp" / "server.py"
MAX_MCP_SERVER_LINES = 1000

EXTRACTED_TOOL_WRAPPERS = {
    "_async_log_retrieval",
    "_finalize_recall_response",
    "async_tool_brain_encode",
    "async_tool_brain_recall",
    "async_tool_brain_skills",
    "async_tool_skill_asset",
    "async_tool_skill_view",
}


def test_mcp_server_stays_protocol_sized() -> None:
    line_count = len(MCP_SERVER.read_text(encoding="utf-8").splitlines())

    assert line_count <= MAX_MCP_SERVER_LINES, (
        f"brain/app/mcp/server.py has grown to {line_count} lines. Keep this file as "
        "protocol wiring; move tool implementation into brain/app/mcp/tools/."
    )


def test_extracted_mcp_tools_are_thin_server_wrappers() -> None:
    tree = ast.parse(MCP_SERVER.read_text(encoding="utf-8"), filename=str(MCP_SERVER))
    definitions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
    }

    missing = sorted(EXTRACTED_TOOL_WRAPPERS - definitions.keys())
    assert not missing

    for name in sorted(EXTRACTED_TOOL_WRAPPERS):
        body = _without_docstring(definitions[name].body)
        assert len(body) == 1 and isinstance(body[0], ast.Return), (
            f"{name} should remain a compatibility wrapper. Put implementation logic "
            "in brain/app/mcp/tools/ instead of regrowing server.py."
        )


def test_mcp_tool_modules_exist_for_memory_and_skills() -> None:
    for relative_path in (
        "brain/app/mcp/tools/common.py",
        "brain/app/mcp/tools/encode.py",
        "brain/app/mcp/tools/recall.py",
        "brain/app/mcp/tools/skills.py",
    ):
        assert (ROOT / relative_path).is_file(), f"Missing MCP tool module: {relative_path}"


def _without_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            return body[1:]
    return body

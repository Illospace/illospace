"""Execution and web access tool schemas."""

from __future__ import annotations

from brain.systems.runs.secret_mounts import SECRET_ENV_SCHEMA
from brain.systems.runs.workspace_tool_runtime import WORKSPACE_TOOL_AUTH_SCHEMA


# ── Execution Tools ──────────────────────────────────────────
# Filesystem and shell tools — give agents the ability to DO things

EXEC_TOOLS = [
    {
        "name": "exec_command",
        "description": (
            "Execute a shell command and return stdout, stderr, and exit code. "
            "Use for running tests, git operations, builds, and any CLI task. "
            "Commands run in the project workspace directory. "
            "For multi-step shell operations or when you need to iterate/aggregate "
            "across commands, prefer `run_script` which executes a full Python script in one call. "
            "For running tests, prefer `test_runner` which parses output into structured format."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "workspace": {
                    "type": "string",
                    "description": "Optional workspace root or registered workspace name to target",
                },
                "working_dir": {"type": "string", "description": "Working directory (optional, defaults to workspace)"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 60, max 300)", "default": 60},
                "secret_env": SECRET_ENV_SCHEMA,
                "workspace_tool_auth": WORKSPACE_TOOL_AUTH_SCHEMA,
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a file's contents. Supports optional line range for large files. "
            "Returns the file content with line numbers. "
            "For large files or when you only need structure (imports, classes, functions), "
            "prefer `file_summary`. To read/search multiple files at once, prefer `run_script`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (absolute or relative to workspace)"},
                "workspace": {
                    "type": "string",
                    "description": "Optional workspace root or registered workspace name to target",
                },
                "start_line": {"type": "integer", "description": "First line to read (1-based, optional)"},
                "end_line": {"type": "integer", "description": "Last line to read (inclusive, optional)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file. Creates parent directories if needed. Overwrites existing files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (absolute or relative to workspace)"},
                "workspace": {
                    "type": "string",
                    "description": "Optional workspace root or registered workspace name to target",
                },
                "content": {"type": "string", "description": "Full file content to write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Make a surgical edit to a file by replacing an exact string match. "
            "More efficient than write_file for modifications — only sends the diff."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "workspace": {
                    "type": "string",
                    "description": "Optional workspace root or registered workspace name to target",
                },
                "old_text": {"type": "string", "description": "Exact text to find and replace (must be unique in file)"},
                "new_text": {"type": "string", "description": "Replacement text"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "search_files",
        "description": (
            "Search file contents using a regex pattern. Returns matching lines with context. "
            "For semantic/conceptual search (not just text matching), prefer `semantic_search`. "
            "For searching across many files with complex logic, prefer `run_script`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "workspace": {
                    "type": "string",
                    "description": "Optional workspace root or registered workspace name to target",
                },
                "path": {"type": "string", "description": "Directory or file to search in (default: workspace root)"},
                "glob": {"type": "string", "description": "File glob filter (e.g., '*.py', '**/*.js')"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "list_files",
        "description": "List files matching a glob pattern. Returns file paths sorted by modification time.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern (e.g., '**/*.py', 'src/**/*.ts')"},
                "workspace": {
                    "type": "string",
                    "description": "Optional workspace root or registered workspace name to target",
                },
                "path": {"type": "string", "description": "Base directory (default: workspace root)"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "run_script",
        "description": (
            "Write and execute a Python script in one shot. Use this INSTEAD of chaining "
            "multiple exec_command/read_file/search_files calls when you need to iterate, "
            "search, or aggregate across multiple files or resources. The script runs with "
            "the full Python standard library and returns stdout. Print structured results "
            "(JSON or formatted text) to stdout for clean output."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "script": {"type": "string", "description": "Python 3 script body to execute"},
                "description": {"type": "string", "description": "Brief description of what the script does"},
                "workspace": {
                    "type": "string",
                    "description": "Optional workspace root or registered workspace name to target",
                },
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 60, max 300)", "default": 60},
                "secret_env": SECRET_ENV_SCHEMA,
                "workspace_tool_auth": WORKSPACE_TOOL_AUTH_SCHEMA,
            },
            "required": ["script"],
        },
    },
    {
        "name": "parallel_tool_batch",
        "description": (
            "Execute multiple independent read/search/fetch-style tool calls concurrently in the runtime. "
            "Use this instead of serial tool calls when you need several files, searches, or web fetches "
            "that do not depend on each other. Safe tools only; write/edit/exec side effects are blocked."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "description": "List of tool invocations to execute in parallel.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool_name": {
                                "type": "string",
                                "description": "Safe tool name to invoke in parallel",
                            },
                            "args": {
                                "type": "object",
                                "description": "Arguments for that tool invocation",
                                "default": {},
                            },
                        },
                        "required": ["tool_name"],
                    },
                },
                "max_parallel": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional concurrency cap for this batch",
                },
            },
            "required": ["operations"],
        },
    },
    {
        "name": "web_search",
        "description": (
            "Search the public web using a configured provider with safe defaults and bounded output. "
            "Use this to discover URLs and recent information before escalating to browser automation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "provider": {
                    "type": "string",
                    "description": "Optional search provider override (e.g. brave, tavily, duckduckgo-lite)",
                },
                "limit": {"type": "integer", "description": "Maximum result count", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_fetch",
        "description": (
            "Fetch a public URL with SSRF protection and readable-content extraction. "
            "Use this when you already know the URL and do not need a full browser."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Public HTTP/HTTPS URL"},
                "extract_mode": {
                    "type": "string",
                    "enum": ["markdown", "text", "html"],
                    "default": "markdown",
                },
                "max_chars": {"type": "integer", "description": "Max output characters", "default": 12000},
            },
            "required": ["url"],
        },
    },
]


__all__ = [
    "EXEC_TOOLS",
]

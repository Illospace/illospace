"""
Illo Brain — Extended Agent Tools.

Specialized tools that make agents more token-efficient:
- semantic_search: Embedding-based code search (finds conceptually related code)
- file_summary: Get file metadata without reading full content (saves tokens)
- test_runner: Run tests with structured pass/fail output
- project_context: One-call project overview

These complement the base tools in core/agent.py (read_file, write_file, etc.)
by providing higher-level, structured, bounded-output alternatives.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import pathlib
import re
import subprocess
import sys
import time

import numpy as np

from brain.kernel import config as brain_config
from brain.systems.runs.project_execution_env import (
    annotate_project_execution_result,
    prepare_project_execution_env,
    redact_sensitive_output,
)
from brain.platform.async_io import run_blocking, run_subprocess_sync

logger = logging.getLogger("agent.tools")


# ── Workspace ────────────────────────────────────────────────

WORKSPACE_ROOT = str(brain_config.resolve_workspace_root(default=brain_config.BRAIN_DIR))


def _resolve_path(path: str, workspace_root: str | None = None) -> str:
    """Resolve path relative to workspace, enforce containment."""
    base = os.path.realpath(workspace_root or WORKSPACE_ROOT)
    if os.path.isabs(path):
        resolved = os.path.realpath(path)
    else:
        resolved = os.path.realpath(os.path.join(base, path))
    if not resolved.startswith(base + os.sep) and resolved != base:
        raise ValueError(f"Path escapes workspace: {path}")
    return resolved


# ── Tool Definitions ─────────────────────────────────────────

EXTENDED_TOOLS = [
    {
        "name": "semantic_search",
        "description": (
            "Search code and memories using semantic similarity (embeddings), not just text matching. "
            "Finds conceptually related code even when different words are used. "
            "More expensive than search_files (grep) but finds things grep can't."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for (natural language)"},
                "scope": {
                    "type": "string",
                    "enum": ["memories", "code", "both"],
                    "description": "Where to search (default: both)",
                    "default": "both",
                },
                "limit": {"type": "integer", "description": "Max results (default 5)", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "file_summary",
        "description": (
            "Get a structured summary of a file WITHOUT reading its full content. "
            "Returns: file type, size, line count, imports, classes, functions, and a brief overview. "
            "Much more token-efficient than `read_file` for understanding file structure. "
            "Use this FIRST, then `read_file` only for specific line ranges you need."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (absolute or relative to workspace)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "test_runner",
        "description": (
            "Run tests and return structured results (pass/fail counts, failure details). "
            "More token-efficient than exec_command('pytest ...') — parses output into structured format."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Test target: file path, directory, or specific test (e.g., 'tests/test_agent.py::TestBrainGate')",
                },
                "pattern": {
                    "type": "string",
                    "description": "Only run tests matching this pattern (pytest -k flag)",
                },
                "verbose": {"type": "boolean", "description": "Include full failure output", "default": False},
            },
            "required": ["target"],
        },
    },
    {
        "name": "project_context",
        "description": (
            "Get a high-level overview of the project: type, key files, recent changes, "
            "dependencies, and structure. One tool call instead of 5+ read/list calls. "
            "Use at the start of a task to understand the codebase."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Project root (default: workspace)"},
            },
        },
    },
    {
        "name": "summarize_file_for_task",
        "description": (
            "Answer a narrow question about one file without dumping the full file into the "
            "main worker context. Uses deterministic structure extraction first and may use a "
            "low-intelligence reader model for bounded synthesis. Returns structured evidence and citations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (absolute or relative to workspace)"},
                "question": {"type": "string", "description": "Specific question to answer about the file"},
                "focus": {
                    "type": "string",
                    "enum": ["api", "data-flow", "control-flow", "dependencies", "risks", "behavior"],
                    "description": "Optional lens to bias the summary",
                },
            },
            "required": ["path", "question"],
        },
    },
    {
        "name": "summarize_files_for_task",
        "description": (
            "Answer a narrow question across multiple files without forcing the main worker "
            "to ingest all of them. Uses deterministic ranking first and a low-intelligence reader model "
            "for bounded synthesis when available. Returns structured evidence, ranked files, "
            "and citations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Candidate file paths to analyze",
                },
                "question": {"type": "string", "description": "Specific cross-file question"},
                "max_files": {
                    "type": "integer",
                    "description": "Cap how many files to inspect (default 8)",
                    "default": 8,
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["ranked_evidence", "comparison", "implementation_map"],
                    "description": "How to shape the summary",
                    "default": "ranked_evidence",
                },
            },
            "required": ["paths", "question"],
        },
    },
    {
        "name": "trace_symbol",
        "description": (
            "Trace where a symbol is defined and referenced across the workspace. "
            "Uses deterministic search and lightweight Python AST parsing where available. "
            "Prefer this before broad raw file reads when you need to locate ownership and usage."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Function, class, variable, or imported name to trace"},
                "path": {
                    "type": "string",
                    "description": "Optional subpath to constrain the search",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Cap references returned (default 20)",
                    "default": 20,
                },
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "build_implementation_map",
        "description": (
            "Build a task-scoped map of likely relevant files, symbols, and lightweight edges "
            "between them. Prefer this for cross-file coding tasks before broad reading."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Task or code-understanding question"},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional candidate files to constrain the map",
                },
                "max_files": {
                    "type": "integer",
                    "description": "Cap how many files to include (default 10)",
                    "default": 10,
                },
            },
            "required": ["question"],
        },
    },
]


# ── Tool Handlers ────────────────────────────────────────────

async def handle_semantic_search(query: str, scope: str = "both", limit: int = 5, workspace_root: str | None = None) -> dict:
    """Search using embedding similarity."""
    results = []

    # Memory search
    if scope in ("memories", "both"):
        try:
            from brain.app.mcp.server import async_tool_brain_recall
            memories = await async_tool_brain_recall(query=query, limit=limit)
            if isinstance(memories, dict) and "memories" in memories:
                for m in memories["memories"]:
                    results.append({
                        "source": "memory",
                        "type": m.get("type", "unknown"),
                        "content": m.get("content", "")[:300],
                        "similarity": m.get("similarity", 0),
                    })
        except Exception as e:
            logger.debug(f"Memory search failed: {e}")

    # Code search via embeddings
    if scope in ("code", "both"):
        try:
            code_results = _semantic_code_search(query, limit=limit, workspace_root=workspace_root)
            results.extend(code_results)
        except Exception as e:
            logger.debug(f"Code semantic search failed: {e}")

    # Sort by similarity
    results.sort(key=lambda r: r.get("similarity", 0), reverse=True)

    return {
        "results": results[:limit],
        "count": len(results),
        "query": query,
    }


def _semantic_code_search(query: str, limit: int = 5, workspace_root: str | None = None) -> list[dict]:
    """Search code files using embeddings.

    Strategy: embed the query, then compare against file-level summaries.
    Falls back to grep if embeddings unavailable.
    """
    try:
        from brain.systems.memory.embeddings import embed_query

        query_vec = embed_query(query)

        # Get candidate files (Python files, limited to avoid embedding everything)
        workspace = pathlib.Path(workspace_root or WORKSPACE_ROOT)
        py_files = sorted(workspace.glob("**/*.py"), key=lambda p: p.stat().st_mtime, reverse=True)
        # Skip venv, __pycache__, .git
        py_files = [
            f for f in py_files
            if not any(skip in str(f) for skip in ("venv/", "__pycache__", ".git/", "node_modules/"))
        ][:50]  # Cap at 50 most recent

        # Build lightweight summaries for embedding
        candidates = []
        for f in py_files:
            try:
                first_lines = f.read_text(errors="replace")[:500]
                rel_path = str(f.relative_to(workspace))
                candidates.append((rel_path, first_lines))
            except Exception:
                continue

        if not candidates:
            return []

        # Embed summaries
        from brain.systems.memory.embeddings import embed_batch
        texts = [f"{path}: {preview}" for path, preview in candidates]
        vecs = embed_batch(texts, mode="document")

        # Compute similarities
        similarities = np.dot(vecs, query_vec) / (
            np.linalg.norm(vecs, axis=1) * np.linalg.norm(query_vec) + 1e-8
        )

        # Return top matches
        top_indices = np.argsort(similarities)[::-1][:limit]
        results = []
        for idx in top_indices:
            if similarities[idx] > 0.3:  # minimum threshold
                path, preview = candidates[idx]
                results.append({
                    "source": "code",
                    "path": path,
                    "preview": preview[:200],
                    "similarity": round(float(similarities[idx]), 3),
                })

        return results

    except Exception as e:
        logger.debug(f"Semantic code search fell back to grep: {e}")
        # Fallback: simple grep
        try:
            proc = run_subprocess_sync(
                ["grep", "-rn", "--include=*.py", "-l", query.split()[0], workspace_root or WORKSPACE_ROOT],
                capture_output=True, text=True, timeout=10,
            )
            files = proc.stdout.strip().split("\n")[:limit]
            return [{"source": "code", "path": f, "similarity": 0.5} for f in files if f]
        except Exception:
            return []


def _workspace_search_root(path: str | None = None, workspace_root: str | None = None) -> str:
    return _resolve_path(path, workspace_root=workspace_root) if path else (workspace_root or WORKSPACE_ROOT)


def _iter_candidate_code_files(root: str) -> list[pathlib.Path]:
    base = pathlib.Path(root)
    if base.is_file():
        return [base]
    files = []
    for ext in ("*.py", "*.js", "*.ts", "*.tsx", "*.jsx", "*.go", "*.rs"):
        files.extend(base.rglob(ext))
    filtered = [
        p for p in files
        if not any(skip in str(p) for skip in ("node_modules/", "venv/", "__pycache__/", ".git/", "dist/", "build/"))
    ]
    return sorted(set(filtered))


def _python_definitions_for_symbol(path: pathlib.Path, symbol: str, workspace_root: str | None = None) -> list[dict]:
    try:
        tree = ast.parse(path.read_text(errors="replace"))
    except Exception:
        return []

    definitions = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol:
            definitions.append({
                "path": str(path.relative_to(pathlib.Path(workspace_root or WORKSPACE_ROOT))),
                "line": node.lineno,
                "kind": type(node).__name__.replace("Def", "").lower(),
            })
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == symbol:
                    definitions.append({
                        "path": str(path.relative_to(pathlib.Path(workspace_root or WORKSPACE_ROOT))),
                        "line": node.lineno,
                        "kind": "assignment",
                    })
    return definitions


def handle_trace_symbol(symbol: str, path: str | None = None, max_results: int = 20, workspace_root: str | None = None) -> dict:
    """Trace where a symbol is defined and referenced."""
    try:
        root = _workspace_search_root(path, workspace_root=workspace_root)
    except ValueError as exc:
        return {"error": str(exc)}

    candidate_files = _iter_candidate_code_files(root)
    if not candidate_files:
        return {"symbol": symbol, "definitions": [], "references": [], "count": 0}

    definitions: list[dict] = []
    references: list[dict] = []
    symbol_pattern = re.compile(rf"\b{re.escape(symbol)}\b")

    for file_path in candidate_files:
        rel_path = (
            str(file_path.relative_to(pathlib.Path(root)))
            if str(file_path).startswith(str(pathlib.Path(root)))
            else str(file_path)
        )
        if file_path.suffix == ".py":
            definitions.extend(_python_definitions_for_symbol(file_path, symbol, workspace_root=root))

        try:
            lines = file_path.read_text(errors="replace").splitlines()
        except Exception:
            continue

        for lineno, line in enumerate(lines, 1):
            if not symbol_pattern.search(line):
                continue
            stripped = line.strip()
            references.append({
                "path": rel_path,
                "line": lineno,
                "snippet": stripped[:220],
            })

    references = references[:max(1, max_results)]
    definition_keys = {(item["path"], item["line"]) for item in definitions}
    non_definition_refs = [
        ref for ref in references
        if (ref["path"], ref["line"]) not in definition_keys
    ]

    related_symbols = []
    for ref in references[:8]:
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", ref["snippet"]):
            if token != symbol and token not in related_symbols:
                related_symbols.append(token)
            if len(related_symbols) >= 10:
                break
        if len(related_symbols) >= 10:
            break

    likely_execution_path = []
    if definitions:
        likely_execution_path.append(
            f"Definition in {definitions[0]['path']}:{definitions[0]['line']}"
        )
    for ref in non_definition_refs[:3]:
        likely_execution_path.append(f"Referenced in {ref['path']}:{ref['line']}")

    return {
        "symbol": symbol,
        "definitions": definitions[:10],
        "references": references,
        "related_symbols": related_symbols,
        "likely_execution_path": likely_execution_path,
        "count": len(references),
    }


def handle_file_summary(path: str, workspace_root: str | None = None) -> dict:
    """Get structured file summary without reading full content."""
    try:
        resolved = _resolve_path(path, workspace_root=workspace_root)
    except ValueError as e:
        return {"error": str(e)}

    if not os.path.isfile(resolved):
        return {"error": f"File not found: {path}"}

    stat = os.stat(resolved)
    result = {
        "path": path,
        "size_bytes": stat.st_size,
        "extension": os.path.splitext(resolved)[1],
    }

    try:
        with open(resolved, "r", errors="replace") as f:
            lines = f.readlines()
        result["line_count"] = len(lines)
    except Exception:
        result["line_count"] = -1
        return result

    # For Python files, extract structure using AST
    if resolved.endswith(".py"):
        try:
            source = "".join(lines)
            tree = ast.parse(source)

            imports = []
            classes = []
            functions = []
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.append(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    imports.append(f"from {node.module}" if node.module else "from .")
                elif isinstance(node, ast.ClassDef):
                    methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                    classes.append({"name": node.name, "methods": methods[:10], "line": node.lineno})
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions.append({"name": node.name, "line": node.lineno})

            result["imports"] = imports[:20]
            result["classes"] = classes[:10]
            result["functions"] = functions[:20]

            # Docstring
            docstring = ast.get_docstring(tree)
            if docstring:
                result["docstring"] = docstring[:300]

        except SyntaxError:
            result["parse_error"] = "Could not parse Python AST"

    # For other files, just return first few lines as preview
    elif result["line_count"] > 0:
        result["preview"] = "".join(lines[:5]).strip()[:300]

    return result


def handle_test_runner(target: str, pattern: str | None = None, verbose: bool = False, workspace_root: str | None = None) -> dict:
    """Run tests and return structured results."""
    cmd = [sys.executable, "-m", "pytest", target, "--tb=short", "-q"]
    if pattern:
        cmd.extend(["-k", pattern])
    if verbose:
        cmd.append("-v")

    project_execution = prepare_project_execution_env()

    try:
        proc = run_subprocess_sync(
            cmd, capture_output=True, text=True,
            timeout=120, cwd=workspace_root or WORKSPACE_ROOT,
            env=project_execution.env,
        )

        output = redact_sensitive_output(
            (proc.stdout or "") + (proc.stderr or ""),
            project_execution.sensitive_values,
        )
        result = {
            "exit_code": proc.returncode,
            "success": proc.returncode == 0,
        }

        # Parse pytest output for counts
        for line in output.split("\n"):
            line = line.strip()
            if "passed" in line or "failed" in line or "error" in line:
                result["summary"] = line
                break

        # Extract failures
        if proc.returncode != 0:
            failures = []
            in_failure = False
            current = []
            for line in output.split("\n"):
                if line.startswith("FAILED ") or line.startswith("ERROR "):
                    failures.append(line.strip()[:200])
                elif "FAILURES" in line:
                    in_failure = True
                elif in_failure and line.startswith("_____"):
                    if current:
                        failures.append("\n".join(current)[:500])
                        current = []
                elif in_failure:
                    current.append(line)

            result["failures"] = failures[:5]  # Cap at 5

        annotate_project_execution_result(result, project_execution)
        return result

    except subprocess.TimeoutExpired:
        result = {"exit_code": -1, "success": False, "error": "Tests timed out after 120s"}
        annotate_project_execution_result(result, project_execution)
        return result
    except Exception as e:
        error = redact_sensitive_output(str(e), project_execution.sensitive_values)
        result = {"exit_code": -1, "success": False, "error": error}
        annotate_project_execution_result(result, project_execution)
        return result


def handle_project_context(path: str | None = None, workspace_root: str | None = None) -> dict:
    """Get project overview in one call."""
    root = pathlib.Path(_resolve_path(path, workspace_root=workspace_root) if path else (workspace_root or WORKSPACE_ROOT))

    result = {
        "root": str(root),
        "type": "unknown",
    }

    # Detect project type
    markers = {
        "package.json": "node",
        "requirements.txt": "python",
        "Cargo.toml": "rust",
        "go.mod": "go",
        "pom.xml": "java",
    }
    for marker, ptype in markers.items():
        if (root / marker).exists():
            result["type"] = ptype
            break

    # Key files
    key_files = []
    for pattern in ["*.py", "*.js", "*.ts", "*.go", "*.rs"]:
        files = sorted(root.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        key_files.extend(str(f.relative_to(root)) for f in files[:5])
    result["key_files"] = key_files[:15]

    # Directory structure (top level)
    try:
        dirs = sorted([
            d.name for d in root.iterdir()
            if d.is_dir() and not d.name.startswith(".") and d.name not in (
                "node_modules", "venv", "__pycache__", ".git", "dist", "build"
            )
        ])
        result["directories"] = dirs
    except Exception:
        result["directories"] = []

    # Recent git changes
    try:
        proc = run_subprocess_sync(
            ["git", "log", "--oneline", "-5"],
            capture_output=True, text=True, timeout=5, cwd=str(root),
        )
        if proc.returncode == 0:
            result["recent_commits"] = proc.stdout.strip().split("\n")
    except Exception:
        pass

    # Line counts by extension
    try:
        counts = {}
        for ext in (".py", ".js", ".ts", ".go"):
            files = list(root.glob(f"**/*{ext}"))
            files = [f for f in files if "node_modules" not in str(f) and "venv" not in str(f)]
            counts[ext] = len(files)
        result["file_counts"] = counts
    except Exception:
        pass

    return result


def _reader_model(user_id: str | None = None, org_id: str | None = None) -> str:
    """Pick a low-intelligence reader model from the active provider policy."""
    from brain.platform.providers.model_policy import (
        get_model_for_tier,
        resolve_default_provider,
    )

    provider = resolve_default_provider(user_id=user_id, org_id=org_id)
    model = get_model_for_tier(
        "low",
        provider=provider,
        include_provider_prefix=True,
        user_id=user_id,
        org_id=org_id,
    )
    return model


def _read_file_excerpt(path: str, max_chars: int = 6000, workspace_root: str | None = None) -> tuple[str | None, int | None, str | None]:
    """Read a bounded excerpt from a file under workspace rules."""
    try:
        resolved = _resolve_path(path, workspace_root=workspace_root)
    except ValueError as exc:
        return None, None, str(exc)

    if not os.path.isfile(resolved):
        return None, None, f"File not found: {path}"

    try:
        with open(resolved, "r", errors="replace") as f:
            text = f.read()
        return text[:max_chars], len(text.splitlines()), None
    except Exception as exc:
        return None, None, str(exc)


def _read_file_lines(path: str, workspace_root: str | None = None) -> tuple[list[str] | None, str | None]:
    try:
        resolved = _resolve_path(path, workspace_root=workspace_root)
    except ValueError as exc:
        return None, str(exc)
    if not os.path.isfile(resolved):
        return None, f"File not found: {path}"
    try:
        with open(resolved, "r", errors="replace") as f:
            return f.read().splitlines(), None
    except Exception as exc:
        return None, str(exc)


def _keyword_lines(text: str, question: str, limit: int = 3) -> list[dict]:
    keywords = [w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", question or "")]
    if not keywords:
        return []

    results = []
    for lineno, line in enumerate(text.splitlines(), 1):
        lower = line.lower()
        score = sum(1 for kw in keywords if kw in lower)
        if score:
            results.append({"line": lineno, "score": score, "text": line.strip()[:200]})
    results.sort(key=lambda item: (-item["score"], item["line"]))
    return results[:limit]


def _build_file_outline(summary: dict) -> str:
    parts = [
        f"path={summary.get('path', '')}",
        f"extension={summary.get('extension', '')}",
        f"lines={summary.get('line_count', -1)}",
    ]
    imports = summary.get("imports") or []
    functions = summary.get("functions") or []
    classes = summary.get("classes") or []
    if imports:
        parts.append("imports=" + ", ".join(imports[:10]))
    if functions:
        parts.append(
            "functions="
            + ", ".join(f"{item.get('name')}@{item.get('line')}" for item in functions[:12])
        )
    if classes:
        parts.append(
            "classes="
            + ", ".join(f"{item.get('name')}@{item.get('line')}" for item in classes[:8])
        )
    if summary.get("docstring"):
        parts.append(f"docstring={summary['docstring'][:220]}")
    if summary.get("preview"):
        parts.append(f"preview={summary['preview'][:220]}")
    return "\n".join(parts)


def _chunk_lines(lines: list[str], chunk_size: int = 120, overlap: int = 20) -> list[dict]:
    if not lines:
        return []
    chunks = []
    step = max(1, chunk_size - overlap)
    for start_idx in range(0, len(lines), step):
        end_idx = min(len(lines), start_idx + chunk_size)
        chunks.append({
            "start_line": start_idx + 1,
            "end_line": end_idx,
            "text": "\n".join(lines[start_idx:end_idx]),
        })
        if end_idx >= len(lines):
            break
    return chunks


def _chunk_score(chunk_text: str, question: str, summary: dict) -> float:
    keywords = [w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", question or "")]
    if not keywords:
        return 0.0
    lowered = chunk_text.lower()
    score = sum(1.5 for kw in keywords if kw in lowered)
    symbols = [item.get("name", "").lower() for item in (summary.get("functions") or [])]
    symbols += [item.get("name", "").lower() for item in (summary.get("classes") or [])]
    keyword_set = set(keywords)
    score += sum(0.6 for sym in symbols[:20] if sym and sym in lowered and sym in keyword_set)
    return score


def _select_relevant_chunks(lines: list[str], question: str, summary: dict, max_chunks: int = 3) -> list[dict]:
    chunks = _chunk_lines(lines)
    scored = []
    for chunk in chunks:
        scored.append(( _chunk_score(chunk["text"], question, summary), chunk))
    scored.sort(key=lambda item: (-item[0], item[1]["start_line"]))
    selected = [chunk for score, chunk in scored if score > 0][:max_chunks]
    if not selected and chunks:
        selected = [chunks[0]]
    return sorted(selected, key=lambda item: item["start_line"])


def _fallback_multihop_file_answer(path: str, question: str, summary: dict, lines: list[str]) -> dict:
    chunks = _select_relevant_chunks(lines, question, summary, max_chunks=3)
    citations = []
    chunk_ranges = []
    for chunk in chunks:
        reason = f"Relevant chunk around lines {chunk['start_line']}-{chunk['end_line']}"
        citations.append({
            "path": path,
            "start_line": chunk["start_line"],
            "end_line": chunk["end_line"],
            "reason": reason,
        })
        chunk_ranges.append({
            "start_line": chunk["start_line"],
            "end_line": chunk["end_line"],
            "reason": reason,
        })

    chunk_text = "\n".join(chunk["text"] for chunk in chunks)
    question_keywords = _keywords_from_question(question)
    chunk_tokens = [
        token for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", chunk_text)
        if token not in {"return", "true", "false", "none"}
    ]
    key_symbols = []
    for token in chunk_tokens:
        lowered = token.lower()
        if lowered in question_keywords and token not in key_symbols:
            key_symbols.append(token)
    for item in (summary.get("functions") or [])[:20]:
        name = item.get("name")
        if name and name in chunk_text and name not in key_symbols:
            key_symbols.append(name)
    for item in (summary.get("classes") or [])[:12]:
        name = item.get("name")
        if name and name in chunk_text and name not in key_symbols:
            key_symbols.append(name)
    if not key_symbols:
        key_symbols = [item.get("name") for item in (summary.get("functions") or [])[:10]]
        key_symbols += [item.get("name") for item in (summary.get("classes") or [])[:6]]
    chunk_labels = ", ".join(f"{c['start_line']}-{c['end_line']}" for c in chunks[:3])
    answer = (
        f"Selected {len(chunks)} relevant chunk(s) from {path}. "
        f"Focus on lines {chunk_labels}. "
        "Verify raw code before editing."
    )
    return {
        "answer": answer,
        "key_symbols": [name for name in key_symbols if name][:12],
        "relevant_ranges": chunk_ranges,
        "citations": citations,
        "risks": ["Chunk selection is heuristic; inspect cited ranges before editing."],
        "confidence": 0.58 if len(chunks) > 1 else 0.42,
        "model": "deterministic-multihop-fallback",
    }


def _keywords_from_question(question: str) -> list[str]:
    seen = []
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", question or ""):
        lowered = token.lower()
        if lowered not in seen:
            seen.append(lowered)
    return seen[:20]


def _resolve_candidate_paths(paths: list[str] | None = None, workspace_root: str | None = None) -> list[str]:
    if paths:
        return list(paths)
    files = _iter_candidate_code_files(workspace_root or WORKSPACE_ROOT)
    return [
        str(path.relative_to(pathlib.Path(workspace_root or WORKSPACE_ROOT)))
        if str(path).startswith(str(pathlib.Path(workspace_root or WORKSPACE_ROOT)))
        else str(path)
        for path in files[:80]
    ]


def handle_build_implementation_map(
    question: str,
    paths: list[str] | None = None,
    max_files: int = 10,
    workspace_root: str | None = None,
) -> dict:
    """Build a lightweight structural map for a task across candidate files."""
    candidates = []
    for candidate in _resolve_candidate_paths(paths, workspace_root=workspace_root):
        summary = handle_file_summary(candidate, workspace_root=workspace_root)
        if "error" in summary:
            continue
        candidates.append({
            "path": candidate,
            "summary": summary,
        })
    if not candidates:
        return {"error": "No readable candidate files"}

    keywords = _keywords_from_question(question)
    ranked = []
    edges = []
    symbol_hits: dict[str, list[dict]] = {}

    for item in candidates:
        summary = item["summary"]
        functions = summary.get("functions") or []
        classes = summary.get("classes") or []
        imports = summary.get("imports") or []
        symbol_names = [f.get("name") for f in functions] + [c.get("name") for c in classes]
        outline_text = _build_file_outline(summary).lower()
        relevance = 0.1
        overlap = [kw for kw in keywords if kw in outline_text]
        relevance += 0.12 * len(overlap)
        for symbol in symbol_names[:15]:
            if symbol and symbol.lower() in keywords:
                relevance += 0.25
                symbol_hits.setdefault(symbol, []).append({"path": item["path"], "kind": "definition"})
        ranked.append({
            "path": item["path"],
            "relevance": round(min(0.98, relevance), 2),
            "why": (
                f"keyword overlap: {', '.join(overlap[:5])}"
                if overlap else f"{len(symbol_names)} symbols, {len(imports)} imports"
            ),
            "symbols": symbol_names[:10],
            "imports": imports[:8],
        })

    ranked.sort(key=lambda item: item["relevance"], reverse=True)
    selected = ranked[: max(1, min(max_files, len(ranked)))]
    selected_paths = {item["path"] for item in selected}

    for item in selected:
        for imported in item.get("imports", []):
            target = None
            mod = imported.replace("from ", "").split()[0] if imported.startswith("from ") else imported
            normalized = mod.replace(".", "/")
            for other in selected:
                if other["path"] == item["path"]:
                    continue
                other_base = other["path"].rsplit(".", 1)[0]
                if other_base.endswith(normalized) or normalized.endswith(other_base.replace("/", ".")):
                    target = other["path"]
                    break
            if target:
                edges.append({"from": item["path"], "to": target, "kind": "import"})

    likely_entrypoints = []
    likely_edit_zones = []
    for item in selected[:5]:
        likely_entrypoints.append(item["path"])
        summary = next(c["summary"] for c in candidates if c["path"] == item["path"])
        for fn in (summary.get("functions") or [])[:4]:
            likely_edit_zones.append({
                "path": item["path"],
                "start_line": fn.get("line"),
                "end_line": (fn.get("line") or 0) + 20 if fn.get("line") else None,
                "reason": f"function {fn.get('name')}",
            })

    return {
        "question": question,
        "files_ranked": selected,
        "symbol_hits": symbol_hits,
        "edges": edges[:20],
        "likely_entrypoints": likely_entrypoints,
        "likely_edit_zones": likely_edit_zones[:12],
        "file_count": len(selected),
    }


def _fallback_file_answer(path: str, question: str, summary: dict, text: str) -> dict:
    keyword_hits = _keyword_lines(text, question, limit=3)
    relevant_ranges = []
    citations = []
    for hit in keyword_hits:
        start = max(1, hit["line"] - 2)
        end = hit["line"] + 2
        relevant_ranges.append({"start_line": start, "end_line": end, "reason": hit["text"]})
        citations.append({"path": path, "start_line": start, "end_line": end, "reason": hit["text"]})

    key_symbols = [item.get("name") for item in (summary.get("functions") or [])[:8]]
    key_symbols += [item.get("name") for item in (summary.get("classes") or [])[:5]]

    answer_parts = []
    if summary.get("functions") or summary.get("classes"):
        answer_parts.append(
            f"{path} exposes {len(summary.get('functions') or [])} top-level functions and "
            f"{len(summary.get('classes') or [])} classes."
        )
    if keyword_hits:
        answer_parts.append(
            "Most relevant lines: "
            + ", ".join(f"{hit['line']}" for hit in keyword_hits)
            + "."
        )
    elif summary.get("docstring"):
        answer_parts.append(f"Docstring summary: {summary['docstring'][:180]}")
    else:
        answer_parts.append("No strong keyword match found; use raw read for exact verification.")

    return {
        "answer": " ".join(answer_parts),
        "key_symbols": [name for name in key_symbols if name][:10],
        "relevant_ranges": relevant_ranges,
        "citations": citations,
        "risks": ["Use read_file before editing to verify exact implementation details."],
        "confidence": 0.45 if keyword_hits else 0.25,
        "model": "deterministic-fallback",
    }


async def _reader_completion(prompt: str, *, user_id: str | None = None, org_id: str | None = None) -> dict | None:
    """Best-effort low-intelligence reader subcall. Returns parsed JSON or None."""
    from brain.platform.integrations.completions import simple_text_completion
    from brain.systems.runs.direct_loop.telemetry import async_record_api_call
    from brain.systems.runs.tool_handlers import _agent_context

    model = _reader_model(user_id=user_id, org_id=org_id)
    start_time = time.time()
    response = None
    error = None
    try:
        response = await run_blocking(
            simple_text_completion,
            prompt,
            model=model,
            max_tokens=700,
            user_id=user_id,
            org_id=org_id,
            system_prompt=(
                "You are a low-cost code reading helper. "
                "Return strict JSON only. Be compact, cite files and line ranges, "
                "and never invent code details that are not present in the provided excerpts."
            ),
        )
        return _parse_reader_completion_response(response)
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        run = getattr(_agent_context, "run", None)
        run_id = getattr(run, "run_id", None)
        session_id = getattr(_agent_context, "session_id", None)
        await async_record_api_call(
            session_id=session_id,
            run_id=run_id,
            turn=0,
            model=model,
            context_messages=1,
            status="error" if error else "success",
            stop_reason="reader_subcall",
            latency_ms=int((time.time() - start_time) * 1000),
            error=error,
        )


def _parse_reader_completion_response(response: str | None) -> dict | None:
    """Parse the reader helper's strict-JSON response."""
    if not response:
        return None
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        logger.debug("Reader subcall returned non-JSON output")
        return None


def _record_reader_artifact(tool_name: str, payload: dict) -> None:
    """Best-effort execution artifact logging for reader-tool usage."""
    try:
        from brain.systems.runs.tool_handlers import _agent_context
    except Exception:
        return

    artifact = {
        "type": "reader_subcall",
        "tool": tool_name,
        "question": payload.get("question"),
        "path": payload.get("path"),
        "file_count": payload.get("file_count"),
        "model": payload.get("model"),
        "confidence": payload.get("confidence"),
        "citations": payload.get("citations", [])[:5],
    }
    existing = getattr(_agent_context, "execution_artifacts", None)
    if existing is None:
        _agent_context.execution_artifacts = [artifact]
    else:
        existing.append(artifact)


async def handle_summarize_file_for_task(
    path: str,
    question: str,
    focus: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    allow_llm: bool = True,
    workspace_root: str | None = None,
) -> dict:
    """Answer a narrow question about one file with bounded context."""
    summary = handle_file_summary(path, workspace_root=workspace_root)
    if "error" in summary:
        return {"error": summary["error"]}

    lines, error = _read_file_lines(path, workspace_root=workspace_root)
    if error:
        return {"error": error}
    total_lines = len(lines or [])
    selected_chunks = _select_relevant_chunks(lines or [], question, summary, max_chunks=3)
    excerpt = "\n\n".join(
        f"[Chunk {idx + 1}: lines {chunk['start_line']}-{chunk['end_line']}]\n{chunk['text']}"
        for idx, chunk in enumerate(selected_chunks)
    )

    prompt = (
        "Answer the question about this file using only the provided outline and chunk excerpts.\n"
        "Return JSON with keys: answer, key_symbols, relevant_ranges, citations, risks, confidence.\n\n"
        f"QUESTION: {question}\n"
        f"FOCUS: {focus or 'general'}\n\n"
        "FILE OUTLINE:\n"
        f"{_build_file_outline(summary)}\n\n"
        f"CHUNK EXCERPTS:\n{excerpt}\n"
    )
    llm_result = await _reader_completion(prompt, user_id=user_id, org_id=org_id) if allow_llm else None
    if llm_result:
        llm_result.setdefault("key_symbols", [])
        llm_result.setdefault(
            "relevant_ranges",
            [
                {
                    "start_line": chunk["start_line"],
                    "end_line": chunk["end_line"],
                    "reason": "selected chunk",
                }
                for chunk in selected_chunks
            ],
        )
        llm_result.setdefault("citations", llm_result.get("relevant_ranges", []))
        llm_result.setdefault("risks", [])
        llm_result.setdefault("confidence", 0.5)
        llm_result["path"] = path
        llm_result["line_count"] = total_lines
        llm_result["model"] = _reader_model(user_id=user_id, org_id=org_id)
        llm_result["question"] = question
        _record_reader_artifact("summarize_file_for_task", llm_result)
        return llm_result

    result = _fallback_multihop_file_answer(path, question, summary, lines or [])
    result["path"] = path
    result["line_count"] = total_lines
    result["question"] = question
    _record_reader_artifact("summarize_file_for_task", result)
    return result


async def handle_summarize_files_for_task(
    paths: list[str],
    question: str,
    max_files: int = 8,
    output_mode: str = "ranked_evidence",
    user_id: str | None = None,
    org_id: str | None = None,
    allow_llm: bool = True,
    workspace_root: str | None = None,
) -> dict:
    """Answer a narrow question across multiple files with bounded synthesis."""
    implementation_map = handle_build_implementation_map(
        question,
        paths=paths,
        max_files=max_files,
        workspace_root=workspace_root,
    )
    if "error" in implementation_map:
        return implementation_map

    selected = []
    ranked_paths = [item["path"] for item in implementation_map.get("files_ranked", [])]
    for raw_path in ranked_paths[: max(1, min(max_files, 12))]:
        summary = handle_file_summary(raw_path, workspace_root=workspace_root)
        if "error" in summary:
            continue
        lines, error = _read_file_lines(raw_path, workspace_root=workspace_root)
        if error:
            continue
        chunks = _select_relevant_chunks(lines or [], question, summary, max_chunks=2)
        selected.append({
            "path": raw_path,
            "summary": summary,
            "excerpt": "\n\n".join(
                f"[lines {chunk['start_line']}-{chunk['end_line']}]\n{chunk['text']}"
                for chunk in chunks
            ),
            "line_count": len(lines or []) or summary.get("line_count"),
            "chunks": chunks,
        })

    if not selected:
        return {"error": "No readable files available"}

    prompt_files = []
    for item in selected:
        prompt_files.append(
            "FILE:\n"
            f"path={item['path']}\n"
            f"{_build_file_outline(item['summary'])}\n"
            f"excerpt:\n{item['excerpt']}\n"
        )
    prompt = (
        "Answer the cross-file question using only the provided file outlines and excerpts.\n"
        "Return JSON with keys: answer, files_ranked, cross_file_findings, open_questions, citations, confidence.\n"
        "files_ranked items must contain path, relevance, why.\n"
        "citations items must contain path, start_line, end_line, reason.\n\n"
        f"QUESTION: {question}\n"
        f"OUTPUT_MODE: {output_mode}\n\n"
        f"IMPLEMENTATION_MAP:\n{json.dumps(implementation_map, default=str)[:4000]}\n\n"
        + "\n\n".join(prompt_files)
    )

    llm_result = await _reader_completion(prompt, user_id=user_id, org_id=org_id) if allow_llm else None
    if llm_result:
        llm_result.setdefault("files_ranked", [])
        llm_result.setdefault("cross_file_findings", [])
        llm_result.setdefault("open_questions", [])
        llm_result.setdefault("citations", [])
        llm_result.setdefault("confidence", 0.5)
        llm_result["file_count"] = len(selected)
        llm_result["model"] = _reader_model(user_id=user_id, org_id=org_id)
        llm_result["question"] = question
        _record_reader_artifact("summarize_files_for_task", llm_result)
        return llm_result

    files_ranked = implementation_map.get("files_ranked", [])
    citations = []
    cross_file_findings = [
        f"{edge['from']} -> {edge['to']} ({edge['kind']})"
        for edge in implementation_map.get("edges", [])[:8]
    ]
    for item in selected:
        for chunk in item.get("chunks", [])[:2]:
            citations.append({
                "path": item["path"],
                "start_line": chunk["start_line"],
                "end_line": chunk["end_line"],
                "reason": "selected relevant chunk",
            })

    result = {
        "answer": (
            "Built a task-scoped implementation map and selected relevant chunks from the highest-value files. "
            "Use the cited files and ranges for raw verification before editing."
        ),
        "files_ranked": files_ranked[:max_files],
        "cross_file_findings": cross_file_findings[:8],
        "open_questions": [] if citations else ["No strong matches found; consider direct reads or broader search."],
        "citations": citations[:10],
        "confidence": 0.62 if citations else 0.24,
        "file_count": len(selected),
        "model": "deterministic-implementation-map",
        "implementation_map": implementation_map,
    }
    result["question"] = question
    _record_reader_artifact("summarize_files_for_task", result)
    return result


# ── Handler Map ──────────────────────────────────────────────

def get_extended_handlers() -> dict:
    """Return handler map for extended tools."""
    return {
        "semantic_search": handle_semantic_search,
        "file_summary": handle_file_summary,
        "test_runner": handle_test_runner,
        "project_context": handle_project_context,
        "summarize_file_for_task": handle_summarize_file_for_task,
        "summarize_files_for_task": handle_summarize_files_for_task,
        "trace_symbol": handle_trace_symbol,
        "build_implementation_map": handle_build_implementation_map,
    }

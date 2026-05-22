"""Architecture guardrails for DB-backed embeddings."""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RAW_EMBEDDING_CALLS = {"embed_query", "embed_document", "embed_batch"}
RAW_EMBEDDING_ALLOWLIST = {
    "brain/systems/memory/embeddings.py",
    "brain/systems/memory/embedding_service.py",
}


def test_async_runtime_paths_use_embedding_service() -> None:
    offenders: list[str] = []
    for path in _runtime_python_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in RAW_EMBEDDING_ALLOWLIST or rel.startswith("brain/app/cli/"):
            continue

        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        parent_map = _parent_map(tree)
        for node in ast.walk(tree):
            if not _is_raw_embedding_call(node):
                continue
            if _enclosing_function_is_async(node, parent_map):
                offenders.append(f"{rel}:{node.lineno} calls {node.func.id}()")

    assert not offenders, (
        "Async server/job/system code must use EmbeddingService instead of raw "
        "embed_query/embed_document/embed_batch calls:\n" + "\n".join(offenders)
    )


def _runtime_python_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "brain/app", "brain/jobs", "brain/systems"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return [
            ROOT / line
            for line in result.stdout.splitlines()
            if line.endswith(".py")
        ]
    return [
        path
        for folder in ("brain/app", "brain/jobs", "brain/systems")
        for path in (ROOT / folder).rglob("*.py")
        if "__pycache__" not in path.parts
    ]


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }


def _is_raw_embedding_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name):
        return node.func.id in RAW_EMBEDDING_CALLS
    if isinstance(node.func, ast.Attribute):
        return node.func.attr in RAW_EMBEDDING_CALLS
    return False


def _enclosing_function_is_async(node: ast.AST, parent_map: dict[ast.AST, ast.AST]) -> bool:
    current = parent_map.get(node)
    while current is not None:
        if isinstance(current, ast.AsyncFunctionDef):
            return True
        if isinstance(current, ast.FunctionDef):
            return False
        current = parent_map.get(current)
    return False

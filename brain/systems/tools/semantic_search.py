"""Semantic search tool implementation."""
from __future__ import annotations

import logging
import pathlib

import numpy as np

from brain.kernel import config as brain_config
from brain.platform.async_io import run_subprocess_sync

logger = logging.getLogger("agent.tools.semantic_search")

WORKSPACE_ROOT = str(brain_config.resolve_workspace_root(default=brain_config.BRAIN_DIR))


def semantic_code_search(
    query: str,
    limit: int = 5,
    workspace_root: str | None = None,
    embedding_service=None,
) -> dict:
    """Search code files using embeddings, with grep fallback."""
    try:
        if embedding_service is None:
            from brain.systems.memory.embedding_service import EmbeddingService

            embedding_service = EmbeddingService.from_legacy_sync_config()

        query_vec = embedding_service.query(query)
        candidates = _candidate_python_previews(workspace_root)
        if not candidates:
            return []

        texts = [f"{path}: {preview}" for path, preview in candidates]
        vecs = embedding_service.batch(texts, mode="document")
        similarities = np.dot(vecs, query_vec) / (
            np.linalg.norm(vecs, axis=1) * np.linalg.norm(query_vec) + 1e-8
        )

        results = []
        for idx in np.argsort(similarities)[::-1][:limit]:
            if similarities[idx] > 0.3:
                path, preview = candidates[idx]
                results.append({
                    "source": "code",
                    "path": path,
                    "preview": preview[:200],
                    "similarity": round(float(similarities[idx]), 3),
                })
        return results
    except Exception as exc:
        from brain.systems.memory.embedding_service import embedding_degradation_reason

        logger.debug("Semantic code search fell back to grep: %s", embedding_degradation_reason(exc))
        return _grep_code_fallback(query, limit, workspace_root)


def _candidate_python_previews(workspace_root: str | None) -> list[tuple[str, str]]:
    workspace = pathlib.Path(workspace_root or WORKSPACE_ROOT)
    py_files = sorted(workspace.glob("**/*.py"), key=lambda path: path.stat().st_mtime, reverse=True)
    candidates = []
    for path in py_files:
        if any(skip in str(path) for skip in ("venv/", "__pycache__", ".git/", "node_modules/")):
            continue
        try:
            candidates.append((str(path.relative_to(workspace)), path.read_text(errors="replace")[:500]))
        except Exception:
            continue
        if len(candidates) >= 50:
            break
    return candidates


def _grep_code_fallback(query: str, limit: int, workspace_root: str | None) -> list[dict]:
    try:
        first_term = query.split()[0]
    except IndexError:
        return []
    try:
        proc = run_subprocess_sync(
            ["grep", "-rn", "--include=*.py", "-l", first_term, workspace_root or WORKSPACE_ROOT],
            capture_output=True,
            text=True,
            timeout=10,
        )
        files = proc.stdout.strip().split("\n")[:limit]
        return [{"source": "code", "path": path, "similarity": 0.5} for path in files if path]
    except Exception:
        return []

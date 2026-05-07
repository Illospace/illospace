#!/usr/bin/env python3
"""Nightly repo-summary refresh shell.

The shell is deliberately thin: it loads prior summary metadata, delegates the
bounded deterministic comparison to ``brain.systems.memory.repo_summary``, and emits a
JSON payload for a later persistence step.  It performs no LLM calls.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from brain.systems.memory.repo_summary import (  # noqa: E402
    DEFAULT_ARCHITECTURE_GLOBS,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_TOTAL_BYTES,
    RepoSummarySpec,
    refresh_repo_summaries,
)


def run_nightly_repo_refresh(
    *,
    repo_root: str | os.PathLike[str] | None = None,
    path_globs: Sequence[str] | None = None,
    previous_summaries: Sequence[Mapping[str, Any]] | None = None,
    previous_json: str | os.PathLike[str] | None = None,
    output_json: str | os.PathLike[str] | None = None,
    include_prose: bool = False,
    dry_run: bool = False,
    max_files: int = DEFAULT_MAX_FILES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> dict[str, Any]:
    """Run the deterministic repo-summary refresh and optionally write JSON."""

    loaded_previous = list(previous_summaries or [])
    if previous_json:
        loaded_previous.extend(_load_previous_json(Path(previous_json)))

    spec = RepoSummarySpec(
        repo_root=repo_root or Path.cwd(),
        path_globs=tuple(path_globs or DEFAULT_ARCHITECTURE_GLOBS),
        include_prose=include_prose,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
    )
    result = refresh_repo_summaries([spec], previous_summaries=loaded_previous)

    if output_json and not dry_run:
        output_path = Path(output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")

    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh deterministic repo summary metadata")
    parser.add_argument("--repo-root", default=str(Path.cwd()), help="Repository root to summarize")
    parser.add_argument(
        "--glob",
        dest="path_globs",
        action="append",
        help="Scoped file glob to include. Repeatable. Defaults to compact architecture globs.",
    )
    parser.add_argument("--previous-json", help="JSON file containing previous summary payload(s)")
    parser.add_argument("--output-json", help="Write refresh payload to this JSON file")
    parser.add_argument("--include-prose", action="store_true", help="Request prose; emits pending action only")
    parser.add_argument("--dry-run", action="store_true", help="Do not write output JSON")
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES)
    parser.add_argument("--max-total-bytes", type=int, default=DEFAULT_MAX_TOTAL_BYTES)
    args = parser.parse_args(argv)

    result = run_nightly_repo_refresh(
        repo_root=args.repo_root,
        path_globs=args.path_globs,
        previous_json=args.previous_json,
        output_json=args.output_json,
        include_prose=args.include_prose,
        dry_run=args.dry_run,
        max_files=args.max_files,
        max_file_bytes=args.max_file_bytes,
        max_total_bytes=args.max_total_bytes,
    )
    if args.dry_run or not args.output_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _load_previous_json(path: Path) -> list[Mapping[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text())
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    for key in ("current_summaries", "current", "summaries"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
    return [payload]


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the knowledge-recall evaluation against the configured live database."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from typing import Sequence

from brain.jobs.evals.knowledge_recall import (
    DEFAULT_K_VALUES,
    DEFAULT_QUESTION_SET_PATH,
    DEFAULT_SEARCH_LIMIT,
    load_knowledge_recall_question_set,
    run_knowledge_recall_eval,
)
from brain.platform.db import SessionFactory


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Score ranked Illo Knowledge recall against known-best evidence.",
    )
    parser.add_argument(
        "--org-id",
        required=True,
        help="Organization UUID whose knowledge corpus should be searched.",
    )
    parser.add_argument(
        "--question-set",
        type=Path,
        default=DEFAULT_QUESTION_SET_PATH,
        help=f"Versioned question-set JSON (default: {DEFAULT_QUESTION_SET_PATH}).",
    )
    parser.add_argument(
        "--k",
        action="append",
        dest="k_values",
        type=int,
        help="Recall cutoff; repeat for multiple values (default: 3 and 10).",
    )
    parser.add_argument(
        "--generated-at",
        default=None,
        help="Optional fixed ISO timestamp for reproducible snapshots.",
    )
    parser.add_argument(
        "--search-limit",
        type=int,
        default=DEFAULT_SEARCH_LIMIT,
        help="Retrieval depth used for MRR (default and maximum: 50).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write JSON to this path instead of stdout.",
    )
    return parser


def _emit(payload: dict, output: Path | None) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output is None:
        print(rendered, end="")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")


async def _run(args: argparse.Namespace) -> dict:
    question_set = load_knowledge_recall_question_set(args.question_set)
    async with SessionFactory() as session:
        result = await run_knowledge_recall_eval(
            session,
            org_id=args.org_id,
            question_set=question_set,
            k_values=args.k_values or DEFAULT_K_VALUES,
            search_limit=args.search_limit,
            generated_at=args.generated_at,
            live_database=True,
        )
    return result.to_dict()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = asyncio.run(_run(args))
        _emit(payload, args.output)
    except Exception as exc:
        print(
            f"Knowledge recall eval failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2
    return 1 if payload["summary"]["search_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

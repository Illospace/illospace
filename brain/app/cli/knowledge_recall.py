"""Evaluate, harvest, or compare knowledge-recall artifacts."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from brain.platform.db import SessionFactory
from brain.systems.knowledge.recall_eval import (
    DEFAULT_K_VALUES,
    DEFAULT_QUESTION_SET_PATH,
    load_knowledge_recall_question_set,
    run_knowledge_recall_eval,
)
from brain.systems.knowledge.recall_eval_comparison import (
    compare_knowledge_recall_artifacts,
)
from brain.systems.knowledge.recall_eval_contract import (
    KnowledgeRecallArtifact,
    parse_knowledge_recall_artifact_json,
)
from brain.systems.knowledge.recall_eval_harvester import (
    harvest_knowledge_recall_candidates,
)
from brain.systems.knowledge.search_contract import (
    KNOWLEDGE_SEARCH_MAX_RESULTS,
    normalize_knowledge_search_limit,
)


def _bounded_search_limit(value: str) -> int:
    try:
        requested = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("search limit must be an integer") from exc
    normalized = normalize_knowledge_search_limit(
        requested,
        default=KNOWLEDGE_SEARCH_MAX_RESULTS,
    )
    if requested != normalized:
        raise argparse.ArgumentTypeError(
            f"search limit must be between 1 and {KNOWLEDGE_SEARCH_MAX_RESULTS}"
        )
    return normalized


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--org-id",
        required=True,
        help="Organization UUID whose knowledge corpus should be used.",
    )
    parser.add_argument(
        "--generated-at",
        default=None,
        help="Optional fixed ISO timestamp for reproducible artifacts.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write JSON to this path instead of stdout.",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate, harvest, or compare Illo Knowledge recall artifacts.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    eval_parser = commands.add_parser(
        "eval",
        help="Score ranked recall against known-best evidence.",
    )
    _add_common_arguments(eval_parser)
    eval_parser.add_argument(
        "--question-set",
        type=Path,
        default=DEFAULT_QUESTION_SET_PATH,
        help=f"Versioned question-set JSON (default: {DEFAULT_QUESTION_SET_PATH}).",
    )
    eval_parser.add_argument(
        "--engine",
        choices=("knowledge", "memory"),
        default="knowledge",
        help="Recall engine to evaluate (default: knowledge).",
    )
    eval_parser.add_argument(
        "--k",
        action="append",
        dest="k_values",
        type=int,
        help="Recall cutoff; repeat for multiple values (default: 3 and 10).",
    )
    eval_parser.add_argument(
        "--search-limit",
        type=_bounded_search_limit,
        default=KNOWLEDGE_SEARCH_MAX_RESULTS,
        help=(
            "Retrieval depth used for MRR "
            f"(default and maximum: {KNOWLEDGE_SEARCH_MAX_RESULTS})."
        ),
    )

    harvest_parser = commands.add_parser(
        "harvest",
        help="Harvest unfinished recall candidates for human curation.",
    )
    _add_common_arguments(harvest_parser)
    harvest_parser.add_argument("--limit-per-source", type=int, default=25)

    compare_parser = commands.add_parser(
        "compare",
        help="Compare two serialized recall evaluation artifacts.",
    )
    compare_parser.add_argument(
        "--baseline",
        type=Path,
        required=True,
        help="Baseline evaluation artifact JSON.",
    )
    compare_parser.add_argument(
        "--candidate",
        type=Path,
        required=True,
        help="Candidate evaluation artifact JSON.",
    )
    compare_parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write JSON to this path instead of stdout.",
    )
    return parser


def _emit(payload: dict[str, Any], output: Path | None) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output is None:
        print(rendered, end="")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    async with SessionFactory() as session:
        if args.command == "eval":
            question_set = load_knowledge_recall_question_set(args.question_set)
            report = await run_knowledge_recall_eval(
                session,
                org_id=args.org_id,
                question_set=question_set,
                k_values=args.k_values or DEFAULT_K_VALUES,
                search_limit=args.search_limit,
                engine=args.engine,
                generated_at=args.generated_at,
                live_database=True,
            )
            return report.to_dict()
        candidates = await harvest_knowledge_recall_candidates(
            session,
            org_id=args.org_id,
            limit_per_source=args.limit_per_source,
            generated_at=args.generated_at,
        )
        return candidates.to_dict()


def _load_artifact(path: Path, *, label: str) -> KnowledgeRecallArtifact:
    try:
        return parse_knowledge_recall_artifact_json(
            path.read_text(encoding="utf-8")
        )
    except ValueError as exc:
        raise ValueError(f"Invalid {label} artifact at {path}: {exc}") from exc


def _compare(args: argparse.Namespace) -> dict[str, Any]:
    return compare_knowledge_recall_artifacts(
        _load_artifact(args.baseline, label="baseline"),
        _load_artifact(args.candidate, label="candidate"),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = (
            _compare(args)
            if args.command == "compare"
            else asyncio.run(_run(args))
        )
        _emit(payload, args.output)
    except Exception as exc:
        print(
            f"Knowledge recall {args.command} failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2
    if args.command == "eval" and payload["result_type"] == "invalid":
        return 1
    if args.command == "compare":
        verdict = payload["comparability"]["verdict"]
        if verdict != "ranking-attributable":
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

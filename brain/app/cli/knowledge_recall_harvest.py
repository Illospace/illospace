"""Harvest reviewable knowledge-recall candidates from the live database."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from typing import Sequence

from brain.jobs.evals.knowledge_recall_harvester import (
    harvest_knowledge_recall_candidates,
)
from brain.platform.db import SessionFactory


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Harvest recall candidates from closed indexed GitHub issues and "
            "historical AgentRun questions."
        ),
    )
    parser.add_argument("--org-id", required=True)
    parser.add_argument("--limit-per-source", type=int, default=25)
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--output", type=Path, default=None)
    return parser


async def _run(args: argparse.Namespace) -> dict:
    async with SessionFactory() as session:
        candidates = await harvest_knowledge_recall_candidates(
            session,
            org_id=args.org_id,
            limit_per_source=args.limit_per_source,
            generated_at=args.generated_at,
        )
    return candidates.to_dict()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = asyncio.run(_run(args))
        rendered = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        if args.output is None:
            print(rendered, end="")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
    except Exception as exc:
        print(
            f"Knowledge recall harvest failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

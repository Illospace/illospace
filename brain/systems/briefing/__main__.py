"""CLI probe: assemble a dossier from a JSON fixture and print it.

Usage::

    python -m brain.systems.briefing --fixture tests/fixtures/briefing/uwear_bug.json
    python -m brain.systems.briefing --fixture … --json   # JSON only (golden refresh)

Fixture shape: ``{"job_ref": str, "headline"?: str, "budget"?: {…},
"pieces": [{"source", "ref", "title", "body", "ts"?: ISO-8601, "weight"?}]}``.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from brain.systems.briefing.core import DossierBudget, SourcePiece, assemble_dossier


def _piece(data: dict) -> SourcePiece:
    ts_raw = data.get("ts")
    return SourcePiece(
        source=str(data.get("source", "")),
        ref=str(data.get("ref", "")),
        title=str(data.get("title", "")),
        body=str(data.get("body", "")),
        ts=datetime.fromisoformat(ts_raw) if ts_raw else None,
        weight=int(data.get("weight", 0)),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m brain.systems.briefing")
    parser.add_argument("--fixture", required=True, help="path to a fixture JSON file")
    parser.add_argument("--json", action="store_true", help="print the JSON dump only")
    args = parser.parse_args(argv)

    data = json.loads(Path(args.fixture).read_text())
    dossier = assemble_dossier(
        [_piece(item) for item in data.get("pieces", [])],
        job_ref=str(data.get("job_ref", "")),
        budget=DossierBudget(**data.get("budget", {})),
        headline=data.get("headline"),
    )
    dump = json.dumps(dossier.to_dict(), indent=2, ensure_ascii=False)
    if args.json:
        print(dump)
    else:
        print(dossier.render_text())
        print()
        print(dump)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

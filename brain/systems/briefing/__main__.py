"""CLI probe: assemble a dossier from a JSON fixture and print it.

Usage::

    python -m brain.systems.briefing --fixture tests/fixtures/briefing/uwear_bug.json
    python -m brain.systems.briefing --fixture … --json   # JSON only (golden refresh)
    python -m brain.systems.briefing --fixture … --compose --ask "fix the batch"

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
    parser.add_argument("--compose", action="store_true", help="also render the packet (slice 02)")
    parser.add_argument("--ask", default="take a pass", help="the ask line for --compose")
    parser.add_argument("--owner", default=None, help="owner label for --compose")
    parser.add_argument("--live", action="store_true", help="gather a real job (pre-merge probe)")
    args = parser.parse_args(argv)

    if args.live:
        parser.error(
            "--live needs a dev checkout with illo-dev read env (DB + Slack + GitHub); "
            "wire it via gather.DefaultSlackReader/DefaultGithubReader — arrives with slice 05's "
            "pre-merge probe. Use --fixture for offline runs."
        )

    data = json.loads(Path(args.fixture).read_text())
    dossier = assemble_dossier(
        [_piece(item) for item in data.get("pieces", [])],
        job_ref=str(data.get("job_ref", "")),
        budget=DossierBudget(**data.get("budget", {})),
        headline=data.get("headline"),
    )
    dump = json.dumps(dossier.to_dict(), indent=2, ensure_ascii=False)
    if args.compose:
        from dataclasses import asdict

        from brain.systems.briefing.compose import compose_packet

        packet = compose_packet(
            dossier, org_id="cli-probe", ask=args.ask, owner_label=args.owner
        )
        print(packet.human_brief)
        print()
        print(json.dumps(asdict(packet.handoff_input), indent=2, ensure_ascii=False, default=str))
        return 0
    if args.json:
        print(dump)
    else:
        print(dossier.render_text())
        print()
        print(dump)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

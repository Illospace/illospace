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


def _run_triage_probe(*, since_hours: float) -> int:
    """The slice-05 pre-merge probe: recent real triaged ideas rendered as
    briefs. Creates no handoffs, posts nothing, rolls back its session; the
    one sanctioned side effect is the GitHub token resolver's vault
    access-audit rows (auth owner's behavior). Briefs print the literal
    ``{launch_url}`` placeholder — honest for a no-row render. Sample output
    belongs in specs/illo-handoff-packets/assets/pre-merge-probe-05.md on
    the PR. Dev CLI: queries all orgs, ignores archival — fine at Uwear
    scale."""
    import asyncio
    from datetime import datetime, timedelta, timezone

    async def _probe() -> int:
        try:
            return await _probe_inner()
        except Exception as exc:  # noqa: BLE001 — a dev CLI fails helpfully, not with a stack
            print(f"probe unavailable (need dev checkout + illo-dev read env): {type(exc).__name__}: {exc}")
            return 2

    async def _probe_inner() -> int:
        import os as _os

        from sqlalchemy import select

        from brain.platform.db.models.idea import Idea
        from brain.platform.db.repositories.unit_of_work import UnitOfWork
        from brain.systems.briefing.mint import _owner_label, build_packet_for_job
        from brain.systems.launch_handoffs import agent_target_for_member, parse_member_agent_targets
        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        targets = {}
        try:
            targets = parse_member_agent_targets(_os.environ.get("ILLO_MEMBER_AGENT_TARGETS"))
        except Exception:  # noqa: BLE001
            pass
        async with UnitOfWork() as uow:
            session = uow.session
            ideas = (
                await session.execute(
                    select(Idea)
                    .where(Idea.origin == "inbound_signal", Idea.updated_at >= cutoff)
                    .order_by(Idea.updated_at.desc())
                    .limit(10)
                )
            ).scalars().all()
            if not ideas:
                print(f"no inbound-triaged ideas in the last {since_hours:g}h")
                return 0
            for idea in ideas:
                details = dict(idea.agent_details or {})
                assignment = dict(details.get("assignment") or {})
                owner_id = str(assignment.get("owner_id") or "") or None
                packet, dossier = await build_packet_for_job(
                    session,
                    org_id=str(idea.org_id),
                    job_ref=f"idea:{idea.id}",
                    ask=f"Pick up this {details.get('task_domain', 'other')} item: {idea.title}",
                    owner_user_id=owner_id,
                    owner_label=await _owner_label(session, owner_id),
                    target_tool=agent_target_for_member(owner_id, targets),
                )
                print("=" * 72)
                print(packet.human_brief)
                if dossier.source_notes:
                    print(f"[notes] {'; '.join(dossier.source_notes)}")
            # Read-only by construction: nothing above creates or posts.
            await session.rollback()
        return 0

    return asyncio.run(_probe())


def _run_outcomes(*, since_hours: float) -> int:
    """Slice 07: `--outcomes --since-hours 168` prints the packet outcome
    summary + the digest footer line, read-only."""
    import asyncio
    import json as _json
    from datetime import datetime, timedelta, timezone

    async def _report() -> int:
        try:
            from brain.platform.db.repositories.unit_of_work import UnitOfWork
            from brain.systems.briefing.outcomes import (
                format_outcomes_line,
                load_packet_handoffs,
                packet_outcomes,
            )

            now = datetime.now(timezone.utc)
            async with UnitOfWork() as uow:
                session = uow.session
                from sqlalchemy import select

                from brain.platform.db.models.org import Org

                org_ids = (await session.execute(select(Org.id))).scalars().all()
                for org_id in org_ids:
                    rows = await load_packet_handoffs(
                        session, org_id=str(org_id), since=now - timedelta(hours=since_hours)
                    )
                    summary = packet_outcomes(rows, now=now)
                    print(f"org {org_id}: {format_outcomes_line(summary) or 'no packets in window'}")
                    print(_json.dumps(summary.to_dict(), indent=2))
                await session.rollback()
            return 0
        except Exception as exc:  # noqa: BLE001 — dev CLI fails helpfully
            print(f"outcomes unavailable (need dev checkout + illo-dev read env): {type(exc).__name__}: {exc}")
            return 2

    return asyncio.run(_report())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m brain.systems.briefing")
    parser.add_argument("--fixture", help="path to a fixture JSON file")
    parser.add_argument("--json", action="store_true", help="print the JSON dump only")
    parser.add_argument("--compose", action="store_true", help="also render the packet (slice 02)")
    parser.add_argument("--ask", default="take a pass", help="the ask line for --compose")
    parser.add_argument("--owner", default=None, help="owner label for --compose")
    parser.add_argument(
        "--probe-triage", action="store_true",
        help="pre-merge probe: render briefs for recent REAL triaged items (read-only, "
             "creates nothing, posts nothing; needs dev checkout + illo-dev read env)",
    )
    parser.add_argument("--since-hours", type=float, default=24.0, help="probe/outcomes window")
    parser.add_argument(
        "--outcomes", action="store_true",
        help="packet outcome summary over recent real handoffs (read-only; slice 07)",
    )
    args = parser.parse_args(argv)

    if args.probe_triage:
        return _run_triage_probe(since_hours=args.since_hours)
    if args.outcomes:
        return _run_outcomes(since_hours=args.since_hours)
    if not args.fixture:
        parser.error("--fixture is required unless --probe-triage/--outcomes is used")

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

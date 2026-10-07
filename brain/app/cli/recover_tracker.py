"""Plan or apply recovery of explicitly selected GitHub tracker items."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.inbound.tracker_recovery import recover_tracker_snapshot


async def _run(args: argparse.Namespace) -> dict:
    snapshot = json.loads(Path(args.snapshot).read_text())
    reviewed_plan = json.loads(Path(args.plan).read_text()) if args.plan else None
    async with UnitOfWork() as uow:
        result = await recover_tracker_snapshot(
            uow.session, org_id=args.org_id, projection_id=args.projection_id,
            snapshot=snapshot, apply=args.apply, reviewed_plan=reviewed_plan,
            deduplicate_only=args.deduplicate_only,
        )
        if not args.apply:
            await uow.session.rollback()
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org-id", required=True)
    parser.add_argument("--projection-id", required=True)
    parser.add_argument("--snapshot", required=True, help="Fresh GitHub snapshot JSON file")
    parser.add_argument("--apply", action="store_true", help="Apply the reviewed plan atomically")
    parser.add_argument("--plan", help="JSON output from the prior dry-run")
    parser.add_argument("--deduplicate-only", action="store_true", help="Archive copies while preserving canonical data; skip missing rows")
    args = parser.parse_args()
    if args.apply and not args.plan:
        parser.error("--apply requires --plan")
    print(json.dumps(asyncio.run(_run(args)), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()

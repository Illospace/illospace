"""Preview or add the canonical GitHub tracker projections."""
from __future__ import annotations

import argparse
import asyncio
import json

from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.inbound.tracker_setup import configure_github_tracker


async def _run(args: argparse.Namespace) -> dict:
    async with UnitOfWork() as uow:
        result = await configure_github_tracker(uow.session, org_id=args.org_id, apply=args.apply)
        if not args.apply:
            await uow.session.rollback()
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org-id", required=True)
    parser.add_argument("--apply", action="store_true")
    print(json.dumps(asyncio.run(_run(parser.parse_args())), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()

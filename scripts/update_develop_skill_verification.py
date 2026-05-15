#!/usr/bin/env python3
"""
Add mandatory verification gate to the 'develop' skill procedure.

This script appends concrete verification steps to the develop skill's procedure,
ensuring workers mechanically verify their work before claiming success.

Run: python scripts/update_develop_skill_verification.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import brain.kernel.config  # noqa: F401 — loads .env
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.platform.db.models.skill import Skill, SkillVersion
from sqlalchemy import select

VERIFICATION_BLOCK = """

## Mandatory Verification (do NOT skip)

Before reporting success, mechanically verify every artifact you produced:

1. **If you created/modified a PR:** run `gh pr view <number> --json state,mergedAt` — confirm state is MERGED. If not merged, merge it now with `gh pr merge <number> --squash --delete-branch`.
2. **If you committed to a branch:** run `git log main --oneline -3` — confirm your commit appears on main. If not, the task is NOT done.
3. **If you wrote files:** run `ls -la <filepath>` — confirm files exist on disk with non-zero size.
4. **If tests exist for your change:** run them (`python -m pytest <test_file> -v`) and confirm they pass.
5. **Only report success AFTER all verification checks pass.** If any check fails, fix the issue and re-verify.

Skipping this section means the task is NOT complete, regardless of what you think you did.
"""


async def main():
    async with UnitOfWork() as uow:
        skill_rows = await uow.session.scalars(
            select(Skill).where(Skill.name == "develop")
        )
        skill = skill_rows.first()

        if skill is None:
            print("ERROR: 'develop' skill not found in database.")
            sys.exit(1)

        # Check if already added
        if "Mandatory Verification (do NOT skip)" in skill.procedure:
            print("SKIP: Verification gate already present in develop skill procedure.")
            return

        # Snapshot current version
        version = SkillVersion(
            skill_id=skill.id,
            version=skill.version,
            procedure=skill.procedure,
            changed_by="dispatch-736",
        )
        uow.session.add(version)

        # Append verification block
        skill.procedure = skill.procedure.rstrip() + VERIFICATION_BLOCK
        skill.version = skill.version + 1

        # Also record as refinement
        refinements = list(skill.refinements or [])
        refinements.append({
            "change": "Added mandatory verification gate with concrete commands",
            "reason": "Workers were claiming success without verifying PRs were merged or files existed",
        })
        skill.refinements = refinements

        print(f"SUCCESS: Updated develop skill to v{skill.version}")
        print(f"Procedure now ends with verification gate ({len(skill.procedure)} chars total)")


if __name__ == "__main__":
    asyncio.run(main())

"""Skill import/export service — orchestrates validation + persistence."""
from __future__ import annotations

from brain.platform.db.repositories.skills import SkillRepository
from brain.platform.db.schemas.skills import SkillExport
from brain.systems.skills.gate import validate_skill_structure


class SkillIOService:
    """Import/export orchestration. Business logic here, not in the repo."""

    def __init__(self, skill_repo: SkillRepository):
        self._repo = skill_repo

    def export_all(self) -> list[dict]:
        skills = self._repo.list_active()
        return [SkillExport.model_validate(s).model_dump() for s in skills]

    def import_batch(self, skills_data: list[dict]) -> dict:
        imported, skipped, errors = 0, 0, []
        for s in skills_data:
            s = dict(s)
            if "thinking_tier" not in s and s.get("reasoning_effort"):
                s["thinking_tier"] = s.get("reasoning_effort")
            name = (s.get("name") or "").strip()
            procedure = (s.get("procedure") or "").strip()
            if not name or not procedure:
                skipped += 1
                continue
            violations = validate_skill_structure(name, s.get("description"), procedure)
            if violations:
                skipped += 1
                errors.append(f"{name}: {'; '.join(violations)}")
                continue
            existing = self._repo.get_by_name(name)
            if existing:
                existing.description = s.get("description", existing.description)
                existing.procedure = procedure
                existing.pitfalls = s.get("pitfalls", existing.pitfalls)
                existing.refinements = s.get("refinements", existing.refinements)
                existing.triggers = s.get("triggers", existing.triggers)
                existing.thinking_tier = s.get("thinking_tier", existing.thinking_tier)
            else:
                self._repo.create(
                    name=name, description=s.get("description", ""),
                    procedure=procedure, version=s.get("version", 1),
                    level=s.get("level", "cognitive"),
                    maturity=s.get("maturity", "emerging"),
                    confidence=s.get("confidence", 0.3),
                    pitfalls=s.get("pitfalls", []),
                    refinements=s.get("refinements", []),
                    triggers=s.get("triggers", []),
                    thinking_tier=s.get("thinking_tier", "medium"),
                )
            imported += 1
        return {"imported": imported, "skipped": skipped, "errors": errors}

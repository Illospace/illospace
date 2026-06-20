from sqlalchemy import inspect
from brain.platform.db.models.skill import Skill, SkillExecution, SkillDependency, SkillVersion, SkillHeuristic

def test_skill_has_all_columns():
    cols = {c.name for c in inspect(Skill).columns}
    expected = {
        "id", "name", "description", "procedure", "version", "parent_skill_id",
        "level", "skill_type", "maturity", "confidence", "use_count", "success_count",
        "failure_count", "partial_count", "avg_duration_sec", "last_used",
        "pitfalls", "refinements", "triggers", "guardrails", "embedding",
        "task_centroid", "centroid_count", "auto_emerged", "provisional",
        "builtin", "thinking_tier", "generation",
        "procedure_tokens", "fitness_score", "last_distilled_at",
        "heuristic_count", "archived", "created_at", "updated_at",
        "skill_installation_id", "bundle_version_id", "bundle_digest", "overlay_revision",
        "effective_digest", "source_kind", "trust_level",
    }
    assert cols >= expected, f"Missing: {expected - cols}"

def test_skill_tablename():
    assert Skill.__tablename__ == "skills"

def test_skill_success_rate_property():
    s = Skill(use_count=10, success_count=8)
    assert s.success_rate == 0.8

def test_skill_success_rate_zero_uses():
    s = Skill(use_count=0, success_count=0)
    assert s.success_rate == 0.0

def test_skill_execution_columns():
    cols = {c.name for c in inspect(SkillExecution).columns}
    assert cols >= {"id", "skill_id", "outcome", "duration_sec", "started_at", "flagged", "task_description", "rework_rounds"}

def test_skill_dependency_columns():
    cols = {c.name for c in inspect(SkillDependency).columns}
    assert cols >= {"id", "parent_id", "child_id", "relationship", "execution_order", "strength"}

def test_skill_version_columns():
    cols = {c.name for c in inspect(SkillVersion).columns}
    assert cols >= {"id", "skill_id", "version", "procedure", "pitfalls", "refinements", "changed_by"}

def test_skill_heuristic_columns():
    cols = {c.name for c in inspect(SkillHeuristic).columns}
    assert cols >= {"id", "skill_name", "condition", "action", "confidence", "active", "created_at", "updated_at"}

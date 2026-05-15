from brain.platform.db.models.skill import Skill


def test_skill_success_rate_property():
    s = Skill(use_count=10, success_count=8)
    assert s.success_rate == 0.8


def test_skill_success_rate_zero_uses():
    s = Skill(use_count=0, success_count=0)
    assert s.success_rate == 0.0

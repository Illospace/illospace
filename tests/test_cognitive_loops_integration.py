# tests/test_cognitive_loops_integration.py
"""Integration tests for cognitive loops — verify all four systems are wired."""
import pytest


def test_graduation_constants_exist():
    from brain.systems.feedback.heuristics import (
        GRADUATION_CONFIDENCE,
        GRADUATION_MIN_VALIDATIONS,
        GRADUATION_MIN_SOURCES,
        GRADUATION_COOLDOWN_DAYS,
        DEMOTION_CONFIDENCE,
    )
    assert GRADUATION_CONFIDENCE == 0.9
    assert GRADUATION_MIN_VALIDATIONS == 8
    assert GRADUATION_MIN_SOURCES == 3
    assert GRADUATION_COOLDOWN_DAYS == 7
    assert DEMOTION_CONFIDENCE == 0.7


def test_emotional_steering_importable():
    from brain.systems.runs.emotions import apply_emotional_steering as _apply_emotional_steering, NEGATIVE_EMOTIONS
    assert "frustrated" in NEGATIVE_EMOTIONS


def test_adaptation_functions_importable():
    from brain.systems.runs.cortex import _record_adaptation, _get_adaptation_history


def test_skill_gap_handler_importable():
    from brain.systems.runs.skill_gap import handle_flag_skill_gap as _handle_flag_skill_gap


def test_orm_models_have_new_columns():
    from brain.platform.db.models.skill import SkillHeuristic, Skill
    from brain.platform.db.models.run import AgentRun

    # SkillHeuristic should have graduation columns
    assert hasattr(SkillHeuristic, 'graduated')
    assert hasattr(SkillHeuristic, 'graduated_at')
    assert hasattr(SkillHeuristic, 'demoted_at')

    # Skill should have graduated_steps
    assert hasattr(Skill, 'graduated_steps')

    # AgentRun should have adaptations
    assert hasattr(AgentRun, 'adaptations')

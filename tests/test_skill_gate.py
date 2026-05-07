"""Tests for core/skill_gate.py — centralized skill-creator enforcement.

Public release note: internal issue links were removed from test comments.
"""

import pytest
from brain.systems.skills.gate import (
    validate_skill_structure,
    enforce_gate,
    enforce_cli_gate,
    enforce_live_gate,
    SkillGateError,
)


# ---------------------------------------------------------------------------
# validate_skill_structure
# ---------------------------------------------------------------------------

class TestValidateSkillStructure:
    """Test the structural validation function."""

    def test_valid_skill_passes(self):
        violations = validate_skill_structure(
            name="my-cool-skill",
            description="A skill that does useful things for debugging",
            procedure="1. First, check the logs for errors\n2. Then trace the call stack\n3. Identify the root cause and fix it",
        )
        assert violations == []

    def test_name_too_short(self):
        violations = validate_skill_structure("x", "desc enough", "a" * 60)
        assert any("Name too short" in v for v in violations)

    def test_name_too_long(self):
        violations = validate_skill_structure("a" * 65, "desc enough", "a" * 60)
        assert any("Name too long" in v for v in violations)

    def test_name_invalid_chars(self):
        violations = validate_skill_structure("My Skill!", "desc enough", "a" * 60)
        assert any("lowercase" in v for v in violations)

    def test_name_uppercase_rejected(self):
        violations = validate_skill_structure("MySkill", "desc enough", "a" * 60)
        assert any("lowercase" in v for v in violations)

    def test_description_too_short(self):
        violations = validate_skill_structure("my-skill", "hi", "a" * 60)
        assert any("Description too short" in v for v in violations)

    def test_empty_description(self):
        violations = validate_skill_structure("my-skill", "", "a" * 60)
        assert any("Description too short" in v for v in violations)

    def test_none_description(self):
        violations = validate_skill_structure("my-skill", None, "a" * 60)
        assert any("Description too short" in v for v in violations)

    def test_procedure_too_short(self):
        violations = validate_skill_structure("my-skill", "A good description", "do stuff")
        assert any("Procedure too short" in v for v in violations)

    def test_empty_procedure(self):
        violations = validate_skill_structure("my-skill", "A good description", "")
        assert any("Procedure too short" in v for v in violations)

    def test_vague_language_detected(self):
        violations = validate_skill_structure(
            "my-skill",
            "A good description",
            "When working on this task, just try hard and do your best to handle appropriately. " * 3,
        )
        assert any("Vague language" in v for v in violations)

    def test_strict_mode_requires_structure(self):
        # A long blob of text with no structure
        procedure = "This is a procedure that explains things in detail but has no numbered steps or bullet points or headers just a wall of text that goes on and on."
        violations = validate_skill_structure(
            "my-skill", "A good description", procedure, strict=True,
        )
        assert any("lacks structure" in v for v in violations)

    def test_strict_mode_passes_with_numbered_steps(self):
        procedure = (
            "1. Check the logs for errors\n"
            "2. Trace the call stack\n"
            "3. Identify the root cause"
        )
        violations = validate_skill_structure(
            "my-skill", "A good description", procedure, strict=True,
        )
        assert violations == []

    def test_strict_mode_passes_with_bullets(self):
        procedure = (
            "- Check the logs for errors\n"
            "- Trace the call stack\n"
            "- Identify the root cause"
        )
        violations = validate_skill_structure(
            "my-skill", "A good description", procedure, strict=True,
        )
        assert violations == []

    def test_strict_mode_passes_with_headers(self):
        procedure = (
            "## Step 1\nCheck the logs for errors\n"
            "## Step 2\nTrace the call stack"
        )
        violations = validate_skill_structure(
            "my-skill", "A good description", procedure, strict=True,
        )
        assert violations == []

    def test_multiple_violations(self):
        violations = validate_skill_structure("X!", "", "")
        assert len(violations) >= 3  # name, description, procedure


# ---------------------------------------------------------------------------
# enforce_gate
# ---------------------------------------------------------------------------

class TestEnforceGate:
    """Test the universal gate function."""

    def test_valid_skill_passes(self):
        passed, violations = enforce_gate(
            "my-skill", "Good description here",
            "1. Check the logs for errors\n2. Trace the call stack backward\n3. Identify the root cause and apply the fix",
        )
        assert passed is True
        assert violations == []

    def test_invalid_skill_raises(self):
        with pytest.raises(SkillGateError) as exc_info:
            enforce_gate("", "", "")
        assert len(exc_info.value.violations) > 0

    def test_invalid_skill_no_raise(self):
        passed, violations = enforce_gate("", "", "", raise_on_fail=False)
        assert passed is False
        assert len(violations) > 0

    def test_automated_flag_logged(self, caplog):
        """Automated failures include 'automated' in the log."""
        import logging
        with caplog.at_level(logging.WARNING):
            enforce_gate("", "", "", automated=True, raise_on_fail=False)
        assert "automated" in caplog.text.lower()


# ---------------------------------------------------------------------------
# enforce_cli_gate
# ---------------------------------------------------------------------------

class TestEnforceCliGate:
    """Test the CLI-specific gate."""

    def test_with_ack_passes(self):
        passed, error = enforce_cli_gate(True)
        assert passed is True
        assert error is None

    def test_without_ack_fails(self):
        passed, error = enforce_cli_gate(False)
        assert passed is False
        assert error is not None
        assert "BLOCKED" in error["error"]
        assert "skill-creator" in error["instructions"].lower()

    def test_error_payload_structure(self):
        _, error = enforce_cli_gate(False)
        assert "error" in error
        assert "gate" in error
        assert "instructions" in error


# ---------------------------------------------------------------------------
# enforce_live_gate
# ---------------------------------------------------------------------------

VALID_PROCEDURE = (
    "1. Check the logs for errors\n"
    "2. Trace the call stack backward\n"
    "3. Identify the root cause and apply the fix"
)


class TestEnforceLiveGate:
    """Test the live agent skill creation gate."""

    def test_user_requested_valid_skill_passes_non_provisional(self):
        passed, violations, provisional = enforce_live_gate(
            "write-issues", "Write well-structured GitHub issues",
            VALID_PROCEDURE, user_requested=True,
        )
        assert passed is True
        assert violations == []
        assert provisional is False

    def test_agent_initiated_valid_skill_passes_provisional(self):
        passed, violations, provisional = enforce_live_gate(
            "write-issues", "Write well-structured GitHub issues",
            VALID_PROCEDURE, user_requested=False,
        )
        assert passed is True
        assert violations == []
        assert provisional is True

    def test_invalid_skill_blocked(self):
        passed, violations, provisional = enforce_live_gate(
            "", "", "", user_requested=True,
        )
        assert passed is False
        assert len(violations) > 0

    def test_user_requested_applies_strict_mode(self):
        """User-requested skills get strict validation (must have structure)."""
        unstructured = "This procedure has no steps or bullets just a blob of text that explains things without any clear structure at all."
        passed, violations, _ = enforce_live_gate(
            "my-skill", "A good description", unstructured, user_requested=True,
        )
        assert any("lacks structure" in v for v in violations)

    def test_agent_initiated_no_strict_mode(self):
        """Agent-initiated skills use lighter validation (no structure requirement)."""
        unstructured = "This procedure has no steps or bullets just a blob of text that explains things without any clear structure at all."
        passed, violations, _ = enforce_live_gate(
            "my-skill", "A good description", unstructured, user_requested=False,
        )
        assert passed is True
        assert violations == []

    def test_quality_gate_still_blocks_garbage(self):
        """Even agent-initiated skills can't bypass quality checks."""
        passed, violations, _ = enforce_live_gate(
            "x", "hi", "do stuff", user_requested=False,
        )
        assert passed is False

    def test_vague_language_blocked(self):
        passed, violations, _ = enforce_live_gate(
            "my-skill", "A good description",
            "When working on this task, just try hard and do your best to handle appropriately. " * 3,
            user_requested=False,
        )
        assert any("Vague language" in v for v in violations)

    def test_logs_on_failure(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            enforce_live_gate("", "", "", user_requested=True)
        assert "user-requested" in caplog.text.lower()

    def test_logs_agent_initiated_on_failure(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            enforce_live_gate("", "", "", user_requested=False)
        assert "agent-initiated" in caplog.text.lower()

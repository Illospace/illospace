"""
Centralized skill-creator gate — enforces quality standards for ALL skill creation paths.

Every code path that creates or modifies a skill MUST call one of:
- validate_skill_structure() — for automated paths (nightly reflect, meta_skills, etc.)
- enforce_cli_gate() — for interactive CLI paths (requires --skill-creator-ack)
- enforce_live_gate() — for agent-initiated creation during cortex runs

This prevents skills from being created through bypass paths that skip the
skill-creator consultation gate in cli/skills.py.

Public release note: internal issue links were removed from source comments.
"""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Structural validation (used by all paths)
# ---------------------------------------------------------------------------

# Minimum quality thresholds for skill authoring.
MIN_NAME_LENGTH = 2
MAX_NAME_LENGTH = 64
NAME_PATTERN = re.compile(r'^[a-z0-9][a-z0-9-]*$')
MIN_PROCEDURE_LENGTH = 50  # chars — a real procedure has substance
MIN_DESCRIPTION_LENGTH = 10

VAGUE_PHRASES = [
    "do good work", "try hard", "be careful", "do your best",
    "make it work", "handle appropriately", "do the right thing",
    "use best practices", "follow standards",
]


class SkillGateError(Exception):
    """Raised when a skill fails the gate validation."""

    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__(f"Skill gate blocked: {'; '.join(violations)}")


def validate_skill_structure(
    name: str,
    description: Optional[str],
    procedure: Optional[str],
    *,
    strict: bool = False,
) -> list[str]:
    """Validate that a skill meets minimum quality standards.

    Args:
        name: Skill name (must be lowercase-hyphenated).
        description: Skill description.
        procedure: Skill procedure text.
        strict: If True, applies stricter checks (for CLI/manual creation).
                If False, lighter checks for automated/emerged skills.

    Returns:
        List of violation strings. Empty list = passed.
    """
    violations = []

    # --- Name checks ---
    if not name or len(name.strip()) < MIN_NAME_LENGTH:
        violations.append(f"Name too short (min {MIN_NAME_LENGTH} chars)")
    elif len(name) > MAX_NAME_LENGTH:
        violations.append(f"Name too long (max {MAX_NAME_LENGTH} chars)")
    elif not NAME_PATTERN.match(name):
        violations.append(
            f"Name '{name}' must be lowercase letters, digits, and hyphens only "
            f"(e.g. 'my-skill-name')"
        )

    # --- Description checks ---
    desc = (description or "").strip()
    if len(desc) < MIN_DESCRIPTION_LENGTH:
        violations.append(
            f"Description too short ({len(desc)} chars, min {MIN_DESCRIPTION_LENGTH}). "
            f"Describe what the skill does and when to use it."
        )

    # --- Procedure checks ---
    proc = (procedure or "").strip()
    if len(proc) < MIN_PROCEDURE_LENGTH:
        violations.append(
            f"Procedure too short ({len(proc)} chars, min {MIN_PROCEDURE_LENGTH}). "
            f"A real procedure needs concrete, actionable steps."
        )
    else:
        # Check for vague language
        proc_lower = proc.lower()
        for phrase in VAGUE_PHRASES:
            if phrase in proc_lower:
                violations.append(f"Vague language in procedure: '{phrase}'. Use specific actions.")

        if strict:
            # Strict mode: procedure should have multiple steps/sections
            # Look for numbered steps, bullet points, or markdown headers
            has_structure = any([
                re.search(r'^\d+[\.\)]', proc, re.MULTILINE),  # numbered steps
                re.search(r'^[-*]\s', proc, re.MULTILINE),      # bullet points
                re.search(r'^#{1,3}\s', proc, re.MULTILINE),    # markdown headers
            ])
            if not has_structure:
                violations.append(
                    "Procedure lacks structure. Use numbered steps, bullet points, "
                    "or markdown headers for clarity."
                )

    return violations


def enforce_gate(
    name: str,
    description: Optional[str],
    procedure: Optional[str],
    *,
    automated: bool = False,
    strict: bool = False,
    raise_on_fail: bool = True,
) -> tuple[bool, list[str]]:
    """Universal skill-creator gate. Call this before any skill INSERT.

    Args:
        name: Skill name.
        description: Skill description.
        procedure: Skill procedure text.
        automated: If True, this is an automated path (nightly reflect, auto-create).
                   Automated paths use structural validation only.
        strict: If True, apply stricter validation (for manual CLI creation).
        raise_on_fail: If True, raises SkillGateError on failure.

    Returns:
        (passed: bool, violations: list[str])
    """
    violations = validate_skill_structure(
        name, description, procedure, strict=strict,
    )

    passed = len(violations) == 0

    if not passed:
        source = "automated" if automated else "manual"
        logger.warning(
            "Skill gate BLOCKED %s creation of '%s': %s",
            source, name, "; ".join(violations),
        )
        if raise_on_fail:
            raise SkillGateError(violations)

    return passed, violations


def enforce_cli_gate(skill_creator_ack: bool) -> tuple[bool, Optional[dict]]:
    """Gate for interactive CLI paths — requires --skill-creator-ack flag.

    Returns:
        (passed: bool, error_payload: Optional[dict])
        If not passed, error_payload contains the JSON-serializable error.
    """
    if not skill_creator_ack:
        return False, {
            "error": "BLOCKED: Skill authoring requires skill-creator consultation",
            "gate": "skill-creator-enforcement",
            "instructions": (
                "Before creating or modifying any skill, you MUST: "
                "1) Read the skill creation guidelines from brain_skills tool, "
                "2) Follow the skill structure and principles (name, procedure, pitfalls, thinking_tier), "
                "3) Re-run this command with --skill-creator-ack to confirm compliance. "
                "This gate is mandatory and cannot be bypassed."
            ),
        }
    return True, None


def enforce_live_gate(
    name: str,
    description: Optional[str],
    procedure: Optional[str],
    *,
    user_requested: bool = False,
) -> tuple[bool, list[str], bool]:
    """Gate for live agent skill creation during cortex runs.

    Two modes:
    - user_requested=True: user explicitly asked for the skill.
      Quality gate applies (structure validation) but no CLI ceremony.
      Skill is created as non-provisional.
    - user_requested=False: agent self-identified a gap.
      Quality gate applies. Skill is created as provisional (flagged for review).

    Returns:
        (passed: bool, violations: list[str], provisional: bool)
    """
    # Always enforce structural quality — no garbage skills
    violations = validate_skill_structure(
        name, description, procedure, strict=user_requested,
    )

    passed = len(violations) == 0
    provisional = not user_requested

    if not passed:
        source = "user-requested" if user_requested else "agent-initiated"
        logger.warning(
            "Live skill gate BLOCKED %s creation of '%s': %s",
            source, name, "; ".join(violations),
        )

    return passed, violations, provisional

from brain.systems.skills.gate import (
    MAX_NAME_LENGTH,
    MIN_DESCRIPTION_LENGTH,
    MIN_NAME_LENGTH,
    MIN_PROCEDURE_LENGTH,
    validate_skill_structure,
)


def test_skill_gate_accepts_only_stable_lowercase_hyphenated_names():
    valid_name = "test-suite-audit"
    invalid_names = [
        "",
        "A",
        "Test Suite",
        "test_suite",
        "-leading",
        "trailing-",
        "x" * (MAX_NAME_LENGTH + 1),
    ]

    assert not validate_skill_structure(
        valid_name,
        "Audits test suites.",
        "1. Inspect journeys.\n2. Inspect contracts.\n3. Replace duplicate checks with better behaviour tests.",
        strict=True,
    )
    for name in invalid_names:
        violations = validate_skill_structure(
            name,
            "Audits test suites.",
            "1. Inspect journeys.\n2. Inspect contracts.\n3. Replace duplicate checks with better behaviour tests.",
            strict=True,
        )
        assert any("Name" in violation for violation in violations)


def test_skill_gate_rejects_vague_or_unstructured_procedures():
    short_description = "x" * (MIN_DESCRIPTION_LENGTH - 1)
    short_procedure = "x" * (MIN_PROCEDURE_LENGTH - 1)

    violations = validate_skill_structure("audit-tests", short_description, short_procedure, strict=True)

    assert any("Description too short" in violation for violation in violations)
    assert any("Procedure too short" in violation for violation in violations)

    vague = validate_skill_structure(
        "audit-tests",
        "Audits test suites.",
        "- Read the suite.\n- Use best practices.\n- Report what to improve.",
        strict=True,
    )
    assert any("Vague language" in violation for violation in vague)

    unstructured = validate_skill_structure(
        "audit-tests",
        "Audits test suites.",
        "Read the suite carefully and identify behaviour-level survivability gaps before editing.",
        strict=True,
    )
    assert any("Procedure lacks structure" in violation for violation in unstructured)

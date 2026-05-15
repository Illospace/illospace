from __future__ import annotations

from pathlib import Path

from brain.systems.quality.survivability import (
    assess_survivability,
    load_survivability_config,
)


def test_survivability_scores_evidence_presence(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "vault.py").write_text("def f(): pass\n")
    (tmp_path / "tests" / "test_vault.py").write_text("def test_f(): pass\n")

    config = {
        "category_weights": {
            "critical_invariants": 0.5,
            "user_journeys": 0.5,
        },
        "capabilities": [
            {
                "id": "vault",
                "name": "Vault",
                "criticality": 5,
                "paths": ["src/vault.py"],
                "evidence": {
                    "critical_invariants": ["tests/test_vault.py"],
                    "user_journeys": ["frontend/e2e/vault.spec.ts"],
                },
            }
        ],
    }

    report = assess_survivability(config, tmp_path, changed_files=["src/vault.py"])
    capability = report.capabilities[0]

    assert capability.impacted is True
    assert capability.score == 0.5
    assert report.impacted_score == 0.5
    assert "user_journeys: frontend/e2e/vault.spec.ts" in capability.missing_patterns


def test_survivability_ignores_empty_evidence_files(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_vault.py").write_text("")

    config = {
        "category_weights": {"critical_invariants": 1.0},
        "capabilities": [
            {
                "id": "vault",
                "name": "Vault",
                "criticality": 5,
                "paths": ["src/vault.py"],
                "evidence": {"critical_invariants": ["tests/test_vault.py"]},
            }
        ],
    }

    report = assess_survivability(config, tmp_path)

    evidence = report.capabilities[0].categories[0].evidence[0]
    assert evidence.present is False
    assert evidence.matches == ()


def test_survivability_ignores_python_evidence_without_collectable_tests(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_vault.py").write_text(
        "def helper_only():\n"
        "    return True\n"
    )

    config = {
        "category_weights": {"critical_invariants": 1.0},
        "capabilities": [
            {
                "id": "vault",
                "name": "Vault",
                "criticality": 5,
                "paths": ["src/vault.py"],
                "evidence": {"critical_invariants": ["tests/test_vault.py"]},
            }
        ],
    }

    report = assess_survivability(config, tmp_path)

    assert report.capabilities[0].score == 0.0
    assert report.capabilities[0].missing_patterns == [
        "critical_invariants: tests/test_vault.py",
    ]


def test_survivability_ignores_module_level_skipped_python_evidence(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_vault.py").write_text(
        "import pytest\n"
        "pytest.skip('disabled', allow_module_level=True)\n"
        "\n"
        "def test_vault_survives():\n"
        "    assert True\n"
    )

    config = {
        "category_weights": {"critical_invariants": 1.0},
        "capabilities": [
            {
                "id": "vault",
                "name": "Vault",
                "criticality": 5,
                "paths": ["src/vault.py"],
                "evidence": {"critical_invariants": ["tests/test_vault.py"]},
            }
        ],
    }

    report = assess_survivability(config, tmp_path)

    assert report.capabilities[0].score == 0.0


def test_survivability_counts_conditionally_skipped_ci_evidence(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_vault.py").write_text(
        "import os\n"
        "import pytest\n"
        "pytestmark = [pytest.mark.skipif(not os.environ.get('TEST_DB_URL'), reason='needs db')]\n"
        "\n"
        "def test_vault_survives():\n"
        "    assert True\n"
    )

    config = {
        "category_weights": {"critical_invariants": 1.0},
        "capabilities": [
            {
                "id": "vault",
                "name": "Vault",
                "criticality": 5,
                "paths": ["src/vault.py"],
                "evidence": {"critical_invariants": ["tests/test_vault.py"]},
            }
        ],
    }

    report = assess_survivability(config, tmp_path)

    assert report.capabilities[0].score == 1.0


def test_survivability_ignores_config_evidence_without_signal(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "empty.json").write_text("{}\n")

    config = {
        "category_weights": {"contracts": 1.0},
        "capabilities": [
            {
                "id": "vault",
                "name": "Vault",
                "criticality": 5,
                "paths": ["src/vault.py"],
                "evidence": {"contracts": ["docs/empty.json"]},
            }
        ],
    }

    report = assess_survivability(config, tmp_path)

    assert report.capabilities[0].score == 0.0


def test_survivability_supports_python_evidence_selectors(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "vault.py").write_text("def f(): pass\n")
    (tmp_path / "tests" / "test_vault.py").write_text(
        "def test_existing_invariant():\n"
        "    assert True\n"
    )

    config = {
        "category_weights": {
            "critical_invariants": 0.5,
            "contracts": 0.5,
        },
        "capabilities": [
            {
                "id": "vault",
                "name": "Vault",
                "criticality": 5,
                "paths": ["src/vault.py"],
                "evidence": {
                    "critical_invariants": [
                        "tests/test_vault.py::test_existing_invariant"
                    ],
                    "contracts": [
                        "tests/test_vault.py::test_missing_contract"
                    ],
                },
            }
        ],
    }

    report = assess_survivability(
        config,
        tmp_path,
        changed_files=["tests/test_vault.py"],
    )
    capability = report.capabilities[0]

    assert capability.score == 0.5
    assert capability.impacted is True
    assert capability.changed_files == ("tests/test_vault.py",)
    assert (
        "contracts: tests/test_vault.py::test_missing_contract"
        in capability.missing_patterns
    )


def test_survivability_scores_unmatched_changes_as_not_impacted(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "README.md").write_text("docs\n")

    config = {
        "capabilities": [
            {
                "id": "memory",
                "name": "Memory",
                "criticality": 3,
                "paths": ["brain/systems/memory/**"],
                "evidence": {"critical_invariants": []},
            }
        ],
    }

    report = assess_survivability(config, tmp_path, changed_files=["docs/README.md"])

    assert report.capabilities[0].impacted is False
    assert report.impacted_score is None
    assert report.unmapped_changed_files == ("docs/README.md",)


def test_survivability_reports_no_unmapped_files_when_changes_match_capability(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "memory.py").write_text("def f(): pass\n")
    (tmp_path / "tests" / "test_memory.py").write_text("def test_f(): pass\n")

    config = {
        "capabilities": [
            {
                "id": "memory",
                "name": "Memory",
                "criticality": 3,
                "paths": ["src/**"],
                "evidence": {"critical_invariants": ["tests/test_memory.py"]},
            }
        ],
    }

    report = assess_survivability(config, tmp_path, changed_files=["src/memory.py"])

    assert report.impacted_score == 0.3
    assert report.unmapped_changed_files == ()


def test_evidence_file_changes_impact_protected_capability(tmp_path: Path):
    (tmp_path / "brain").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "brain" / "auth.py").write_text("def authenticate(): pass\n")
    (tmp_path / "tests" / "test_auth.py").write_text("def test_authenticate(): pass\n")

    config = {
        "capabilities": [
            {
                "id": "auth",
                "name": "Auth",
                "criticality": 5,
                "paths": ["brain/auth.py"],
                "evidence": {"critical_invariants": ["tests/test_auth.py"]},
            }
        ],
    }

    report = assess_survivability(config, tmp_path, changed_files=["tests/test_auth.py"])

    assert report.capabilities[0].impacted is True
    assert report.capabilities[0].changed_files == ("tests/test_auth.py",)
    assert report.unmapped_changed_files == ()


def test_repo_survivability_config_is_loadable_and_maps_vault_changes():
    repo_root = Path(__file__).resolve().parents[1]
    config = load_survivability_config(repo_root / "docs" / "survivability-capabilities.json")

    report = assess_survivability(
        config,
        repo_root,
        changed_files=["brain/systems/vault/__init__.py"],
    )

    assert 0.0 <= report.overall_score <= 1.0
    impacted = [capability.capability_id for capability in report.impacted_capabilities]
    assert impacted == ["vault_secrets"]


def test_repo_survivability_config_maps_test_infrastructure_changes():
    repo_root = Path(__file__).resolve().parents[1]
    config = load_survivability_config(repo_root / "docs" / "survivability-capabilities.json")

    report = assess_survivability(
        config,
        repo_root,
        changed_files=["Makefile", ".github/workflows/brain-ci.yml"],
    )

    impacted = [capability.capability_id for capability in report.impacted_capabilities]
    assert impacted == ["test_suite_operability"]

"""Static contracts for CI quality gates."""

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


def _workflow() -> dict:
    return yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "brain-ci.yml").read_text())


def _job_commands() -> str:
    jobs = _workflow()["jobs"]
    commands: list[str] = []
    for job in jobs.values():
        for step in job.get("steps", []):
            command = step.get("run")
            if command:
                commands.append(str(command))
    return "\n".join(commands)


def test_ci_test_jobs_are_not_softened_with_continue_on_error():
    for job_name, job in _workflow()["jobs"].items():
        assert job.get("continue-on-error") is not True, f"{job_name} softens the whole job"
        for step in job.get("steps", []):
            assert step.get("continue-on-error") is not True, f"{job_name}/{step.get('name')} is soft-failing"


def test_ci_runs_survivability_and_core_test_gates():
    commands = _job_commands()

    assert "make survivability-pr" in commands
    assert "make test-fast" in commands
    assert "make test-frontend" in commands
    assert "tests/test_schema_contract.py" in commands
    assert "tests/test_core_product_journeys.py" in commands


def test_pytest_config_makes_unexpected_xpass_failures_visible():
    config = (REPO_ROOT / "pytest.ini").read_text()

    assert "xfail_strict = true" in config

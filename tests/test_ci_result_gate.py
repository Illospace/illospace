import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "ci_result_gate.sh"


def _run_gate(
    needs: dict[str, Any] | str | None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("NEEDS_JSON", None)
    if needs is not None:
        env["NEEDS_JSON"] = (
            json.dumps(needs, indent=2) if isinstance(needs, dict) else needs
        )

    return subprocess.run(
        ["bash", str(GATE)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.mark.parametrize("changes", ["cancelled", "failure", "skipped"])
def test_incomplete_job_selection_fails_when_suites_are_skipped(changes: str):
    result = _run_gate(
        {
            "changes": {"result": changes},
            "backend-fast": {"result": "skipped"},
            "frontend": {"result": "skipped"},
            "db-contract": {"result": "skipped"},
        }
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.splitlines()[:-1] == [
        "backend-fast=skipped",
        f"changes={changes}",
        "db-contract=skipped",
        "frontend=skipped",
    ]
    failure = result.stdout.splitlines()[-1]
    assert f"changes={changes}" in failure
    assert "must be exactly success" in failure


def test_successful_selection_allows_deliberately_skipped_suite():
    result = _run_gate(
        {
            "changes": {"result": "success"},
            "backend-fast": {"result": "success"},
            "frontend": {"result": "skipped"},
            "db-contract": {"result": "success"},
        }
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "backend-fast=success",
        "changes=success",
        "db-contract=success",
        "frontend=skipped",
        "Selected CI jobs passed.",
    ]


def test_failed_suite_fails_after_successful_selection():
    result = _run_gate(
        {
            "changes": {"result": "success"},
            "backend-fast": {"result": "failure"},
            "frontend": {"result": "success"},
            "db-contract": {"result": "success"},
        }
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.splitlines()[:-1] == [
        "backend-fast=failure",
        "changes=success",
        "db-contract=success",
        "frontend=success",
    ]
    failure = result.stdout.splitlines()[-1]
    assert "backend-fast=failure" in failure
    assert "must be exactly success or skipped" in failure


@pytest.mark.parametrize("backend_fast", ["", "cancelled", "timed_out"])
def test_unaccepted_suite_result_fails(backend_fast: str):
    result = _run_gate(
        {
            "changes": {"result": "success"},
            "backend-fast": {"result": backend_fast},
            "frontend": {"result": "success"},
            "db-contract": {"result": "success"},
        }
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.splitlines()[:-1] == [
        f"backend-fast={backend_fast}",
        "changes=success",
        "db-contract=success",
        "frontend=success",
    ]
    failure = result.stdout.splitlines()[-1]
    assert f"backend-fast={backend_fast}" in failure
    assert "must be exactly success or skipped" in failure


def test_new_job_is_checked_without_adding_its_name_to_the_gate():
    result = _run_gate(
        {
            "changes": {"result": "success"},
            "backend-fast": {"result": "success"},
            "frontend": {"result": "skipped"},
            "db-contract": {"result": "success"},
            "new-suite": {"result": "failure"},
        }
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.splitlines()[:-1] == [
        "backend-fast=success",
        "changes=success",
        "db-contract=success",
        "frontend=skipped",
        "new-suite=failure",
    ]
    failure = result.stdout.splitlines()[-1]
    assert "new-suite=failure" in failure
    assert "must be exactly success or skipped" in failure


def test_missing_changes_job_fails():
    result = _run_gate(
        {
            "backend-fast": {"result": "success"},
            "frontend": {"result": "skipped"},
            "db-contract": {"result": "success"},
        }
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.splitlines()[:-1] == [
        "backend-fast=success",
        "db-contract=success",
        "frontend=skipped",
    ]
    failure = result.stdout.splitlines()[-1]
    assert "changes=<missing>" in failure
    assert "must include" in failure


@pytest.mark.parametrize(
    ("needs", "value"),
    [
        (None, "<unset>"),
        ("", "<empty>"),
        ("not valid JSON", "<invalid>"),
        ('{"changes": {"result": "success"}}{}', "<invalid>"),
    ],
)
def test_missing_empty_or_malformed_needs_json_fails(
    needs: str | None,
    value: str,
):
    result = _run_gate(needs)

    assert result.returncode == 1, result.stdout + result.stderr
    failure = result.stdout.splitlines()[-1]
    assert "NEEDS_JSON" in failure
    assert value in failure
    assert "valid JSON object" in failure

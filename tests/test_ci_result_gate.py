import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "ci_result_gate.sh"


def _run_gate(
    changes: str,
    backend_fast: str,
    frontend: str,
    db_contract: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(GATE), changes, backend_fast, frontend, db_contract],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("changes", ["cancelled", "failure", "skipped"])
def test_incomplete_job_selection_fails_when_suites_are_skipped(changes: str):
    result = _run_gate(changes, "skipped", "skipped", "skipped")

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.splitlines()[:4] == [
        f"changes={changes}",
        "backend-fast=skipped",
        "frontend=skipped",
        "db-contract=skipped",
    ]
    assert (
        f"CI result failed: changes={changes}; rule: changes must be exactly "
        "success because job selection must complete."
    ) in result.stdout


def test_successful_selection_allows_deliberately_skipped_suite():
    result = _run_gate("success", "success", "skipped", "success")

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "changes=success",
        "backend-fast=success",
        "frontend=skipped",
        "db-contract=success",
        "Selected CI jobs passed.",
    ]


def test_failed_suite_fails_after_successful_selection():
    result = _run_gate("success", "failure", "success", "success")

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.splitlines()[:4] == [
        "changes=success",
        "backend-fast=failure",
        "frontend=success",
        "db-contract=success",
    ]
    assert (
        "CI result failed: backend-fast=failure; rule: suite result must be "
        "exactly success or skipped."
    ) in result.stdout


@pytest.mark.parametrize("backend_fast", ["", "cancelled", "timed_out"])
def test_unaccepted_suite_result_fails(backend_fast: str):
    result = _run_gate("success", backend_fast, "success", "success")

    assert result.returncode == 1, result.stdout + result.stderr
    assert (
        f"CI result failed: backend-fast={backend_fast}; rule: suite result must "
        "be exactly success or skipped."
    ) in result.stdout

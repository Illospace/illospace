from __future__ import annotations

from pathlib import Path


def test_enabled_cycle_fixture_gate_passes_with_healthy_catalog(monkeypatch):
    from brain.jobs.check_cycle_context_admission import evaluate_specs, load_fixture_specs

    monkeypatch.delenv("AGENT_MODEL_CONTEXT_WINDOW_TOKENS", raising=False)
    report = evaluate_specs(load_fixture_specs())

    assert report["ok"] is True
    assert {item["cycle_id"] for item in report["results"]} == {2, 8, 9}
    assert all(item["status"] == "passed" for item in report["results"])
    assert all(item["tools"] == 93 for item in report["results"])


def test_enabled_cycle_fixture_gate_names_cycles_killed_by_128k_regression(monkeypatch):
    from brain.jobs.check_cycle_context_admission import evaluate_specs, load_fixture_specs

    monkeypatch.setenv("AGENT_MODEL_CONTEXT_WINDOW_TOKENS", "128000")
    report = evaluate_specs(load_fixture_specs())

    assert report["ok"] is False
    failures = {
        item["cycle_id"]: item
        for item in report["results"]
        if item["status"] == "failed"
    }
    assert set(failures) == {2, 9}
    assert all("floor=" in item["diagnostic"] for item in failures.values())
    assert "ceiling=50486" in failures[2]["diagnostic"]
    assert "ceiling=57859" in failures[9]["diagnostic"]
    assert all("tools=93" in item["diagnostic"] for item in failures.values())


def test_compose_upgrade_runs_live_cycle_gate_after_doctor():
    source = Path("deploy/scripts/upgrade.sh").read_text()

    doctor = source.index('"$SCRIPT_DIR/doctor.sh"')
    live_gate = source.index(
        "compose exec -T api python3 -m brain.jobs.check_cycle_context_admission --live"
    )
    assert live_gate > doctor

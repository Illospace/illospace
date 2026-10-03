from __future__ import annotations

from pathlib import Path


def test_enabled_cycle_fixture_gate_passes_with_healthy_catalog(monkeypatch):
    from brain.jobs.check_cycle_context_admission import evaluate_specs, load_fixture_specs

    monkeypatch.delenv("AGENT_MODEL_CONTEXT_WINDOW_TOKENS", raising=False)
    report = evaluate_specs(load_fixture_specs())

    assert report["ok"] is True
    assert {item["cycle_id"] for item in report["results"]} == {2, 8, 9}
    assert all(item["status"] == "passed" for item in report["results"])
    assert all(item["tools"] == 94 for item in report["results"])


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
    assert all("tools=94" in item["diagnostic"] for item in failures.values())


def test_compose_upgrade_runs_live_cycle_gate_after_doctor():
    source = Path("deploy/scripts/upgrade.sh").read_text()

    doctor = source.index('"$SCRIPT_DIR/doctor.sh"')
    live_gate = source.index(
        "compose exec -T api python3 -m brain.jobs.check_cycle_context_admission --live"
    )
    assert live_gate > doctor


def test_large_sol_cycle_preserves_guidance_without_replaying_audit_snapshots(monkeypatch):
    from types import SimpleNamespace
    from brain.jobs.check_cycle_context_admission import CycleAdmissionSpec, check_cycle_context_admission
    from brain.systems.cycles.prompts import cycle_memory_payload

    monkeypatch.delenv("AGENT_AUTO_COMPACT_TOKEN_LIMIT", raising=False)
    guidance = [{"id": 1, "guidance": "G" * 1_100_000}]
    audit = {
        "id": 12, "version": 3, "rationale": "Update routing",
        "changed_fields": ["thinking_override"],
        "before_snapshot": {"guidance": "B" * 1_000_000},
        "after_snapshot": {"guidance": "A" * 1_100_000},
    }
    context = {"behavior_change": audit, "revision": {"id": 42}}
    spec = CycleAdmissionSpec(
        cycle_id=2, name="Large coordinator", prompt="Review workspace",
        model="openai/gpt-6.1-sol", thinking="medium",
        guidance_snapshot=guidance, context_snapshot=context,
    )
    report = check_cycle_context_admission(spec)
    assert report["status"] == "passed"
    assert 275_000 < report["floor"] < 350_000
    assert report["floor"] < report["compaction_threshold"] < 400_000
    assert report["compaction_threshold"] < report["ceiling"]

    payload = cycle_memory_payload(SimpleNamespace(
        guidance_snapshot=guidance, context_snapshot=context, output_targets_snapshot=[],
    ))
    assert payload["guidance"] == guidance
    assert payload["context"]["behavior_change"] == {
        k: v for k, v in audit.items() if k not in {"before_snapshot", "after_snapshot"}
    }
    assert context["behavior_change"]["before_snapshot"]["guidance"] == "B" * 1_000_000
    assert context["behavior_change"]["after_snapshot"]["guidance"] == "A" * 1_100_000

    monkeypatch.setenv("AGENT_AUTO_COMPACT_TOKEN_LIMIT", "240000")
    assert check_cycle_context_admission(spec)["status"] == "failed"

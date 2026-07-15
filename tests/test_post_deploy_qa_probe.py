from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def _load_probe_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "post_deploy_qa_probe.py"
    spec = importlib.util.spec_from_file_location("post_deploy_qa_probe", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_offline_self_test_exercises_all_four_symptoms(capsys):
    probe = _load_probe_module()

    exit_code = probe.main(["--self-test"])

    output = capsys.readouterr().out
    assert exit_code == 0
    for symptom in probe.SYMPTOMS:
        assert f"#{symptom} PASS — evidence:" in output
    assert "REQUIRES LIVE RUNTIME" not in output


def test_dry_run_labels_every_check_as_requiring_live_runtime(capsys):
    probe = _load_probe_module()

    exit_code = probe.main(["--dry-run"])

    output = capsys.readouterr().out
    assert exit_code == 0
    for symptom in probe.SYMPTOMS:
        assert f"#{symptom} REQUIRES LIVE RUNTIME — evidence:" in output
    assert "run.get" in output
    assert "status_code=403" in output
    assert "next_page" in output
    assert "assignee spelling" in output


def test_pr_probe_fails_on_403_with_clear_evidence():
    probe = _load_probe_module()
    bundle = {
        "root": {
            "run": {"run_id": 1, "status": "completed"},
            "tool_events": [
                probe._event(
                    "read_github_source",
                    {"error": "Resource not accessible", "status_code": 403},
                )
            ],
        }
    }

    result = probe.check_306(bundle)

    assert result.status == probe.FAIL
    assert "403" in result.evidence


def test_worker_probe_fails_on_materialization_error():
    probe = _load_probe_module()
    bundle = probe.self_test_bundle()
    bundle["child_runs"]["901"]["artifacts"][0]["text"] = (
        "GITHUB_TOKEN could not be materialized for project context."
    )

    result = probe.check_311(bundle)

    assert result.status == probe.FAIL
    assert "materialization error" in result.evidence


def test_tracker_probe_fails_when_duplicate_create_forks_active_row():
    probe = _load_probe_module()
    bundle = probe.self_test_bundle()
    manage_events = [
        event
        for event in bundle["root"]["tool_events"]
        if event["payload"]["tool_name"] == "manage_domain"
    ]
    manage_events[1] = probe._event(
        "manage_domain",
        {"record": {"id": 78, "data": {"external_id": "PR-318", "assignee": "reda"}}},
    )
    bundle["root"]["tool_events"] = [
        event
        for event in bundle["root"]["tool_events"]
        if event["payload"]["tool_name"] != "manage_domain"
    ] + manage_events

    result = probe.check_290(bundle)

    assert result.status == probe.FAIL
    assert "forked record ids" in result.evidence

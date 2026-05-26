from __future__ import annotations

from scripts.eval_app_capsules import EvalParams, build_scenario_payload, compile_and_validate, run_eval_case, static_score


def test_app_capsule_eval_static_gate_accepts_crm_scenario():
    payload = build_scenario_payload(EvalParams("crm_simple_table", 5, (1440, 900), 0))
    compiled, report = compile_and_validate(payload)
    scores = static_score(payload, compiled, report)

    assert compiled.renderer_key == "app-capsule"
    assert compiled.source_kind == "html"
    assert report["status"] == "passed"
    assert scores["uses_app_capsule"] == 1
    assert scores["contract_pass"] == 1
    assert scores["capability_bindings"] == ["people"]
    assert scores["legacy_color_hits"] == 0


def test_app_capsule_eval_static_gate_checks_requested_tabs():
    payload = build_scenario_payload(EvalParams("crm_tabs", 5, (1440, 900), 0))
    compiled, report = compile_and_validate(payload)
    scores = static_score(payload, compiled, report)

    assert report["status"] == "passed"
    assert scores["tabs_requested"] is True
    assert scores["tab_markup_hits"] >= 3
    assert 'role="tabpanel"' in compiled.source_code


def test_app_capsule_eval_can_run_without_browser_for_ci():
    result = run_eval_case(
        EvalParams("crm_simple_table", 3, (390, 844), 0),
        chrome_path=None,
        timeout_ms=1000,
        skip_browser=True,
        screenshot_dir=None,
    )

    assert result["passed"] is True
    assert result["scores"]["skipped"] is True
    assert result["scores"]["contract_pass"] == 1

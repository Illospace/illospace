#!/usr/bin/env python3
"""Local App Capsule Lab for pre-merge workspace-app quality checks."""
from __future__ import annotations

import argparse
import itertools
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from brain.systems.workspace_apps.compiler import compile_workspace_app_input
from brain.systems.workspace_apps.contracts import build_contract_validation_report


HARNESS = REPO_ROOT / "scripts" / "app_capsule_browser_harness.mjs"
LEGACY_SURFACE_MARKERS = ("#57CFA0", "rgba(252, 248", "amber", "taupe")


@dataclass(frozen=True)
class EvalParams:
    scenario: str
    rows: int
    viewport: tuple[int, int]
    api_latency_ms: int
    run_index: int = 1


def parse_csv_ints(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def parse_viewports(value: str) -> list[tuple[int, int]]:
    viewports: list[tuple[int, int]] = []
    for item in value.split(","):
        text = item.strip().lower()
        if not text:
            continue
        width, height = text.split("x", 1)
        viewports.append((int(width), int(height)))
    return viewports


def crm_people(row_count: int) -> list[dict[str, Any]]:
    companies = ["Ardene", "La Maison Simons", "KANUK", "Groupe Marcelle", "SSENSE", "m0851"]
    roles = ["operator", "decision_maker", "influencer"]
    titles = ["Head of Marketing", "E-commerce Manager", "Director of Ecommerce", "Coordinator Web"]
    first_names = ["Sandra", "Sarah", "Cristina", "Marie", "Philippe", "Kelly", "Jezebel", "Reda"]
    last_names = ["Mathieu", "Deschenes", "Farinelli", "Carriere", "Benoit", "Solti", "Mjahed"]
    rows = []
    for index in range(row_count):
        rows.append(
            {
                "id": index + 1,
                "title": f"{first_names[index % len(first_names)]} {last_names[index % len(last_names)]}",
                "data": {
                    "name": f"{first_names[index % len(first_names)]} {last_names[index % len(last_names)]}",
                    "company": companies[index % len(companies)],
                    "job_title": titles[index % len(titles)],
                    "role": roles[index % len(roles)],
                    "linkedin_status": "sent" if index % 5 else "pending",
                    "notes": "",
                },
                "version": 1,
            }
        )
    return rows


def crm_capsule_source() -> str:
    return """
<main class="illo-app" data-test="crm-app">
  <section class="illo-panel illo-stack">
    <div class="illo-toolbar">
      <div>
        <h1 class="illo-title">Uwear CRM</h1>
        <p class="illo-muted">One table, fast edits, no stacked views.</p>
      </div>
      <button class="illo-button" id="refresh" type="button">Refresh</button>
    </div>
    <input class="illo-input" id="filter" placeholder="Filter loaded rows..." />
    <div class="illo-table-wrap">
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Company</th>
            <th>Job title</th>
            <th>Role</th>
            <th>LinkedIn status</th>
            <th>Notes</th>
            <th></th>
          </tr>
        </thead>
        <tbody id="people-body"></tbody>
      </table>
    </div>
    <p class="illo-muted" id="status">Loading...</p>
  </section>
</main>
<script>
  const people = window.illo.data('people');
  let rows = [];

  function cell(value) {
    return String(value ?? '').replace(/[&<>"']/g, (char) => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;'
    }[char]));
  }

  function recordData(row, key) {
    return row && row.data ? row.data[key] : row[key];
  }

  function render() {
    const query = document.getElementById('filter').value.trim().toLowerCase();
    const visible = rows.filter((row) => {
      const text = [recordData(row, 'name'), recordData(row, 'company'), recordData(row, 'job_title'), recordData(row, 'role')].join(' ').toLowerCase();
      return !query || text.includes(query);
    });
    document.getElementById('people-body').innerHTML = visible.map((row) => `
      <tr data-record-row="${row.id}">
        <td>${cell(recordData(row, 'name') || row.title)}</td>
        <td>${cell(recordData(row, 'company'))}</td>
        <td>${cell(recordData(row, 'job_title'))}</td>
        <td>${cell(recordData(row, 'role'))}</td>
        <td>${cell(recordData(row, 'linkedin_status'))}</td>
        <td><input class="illo-input" data-note-input="${row.id}" value="${cell(recordData(row, 'notes'))}" /></td>
        <td><button class="illo-button" data-note-save="${row.id}" type="button">Save</button></td>
      </tr>
    `).join('');
    document.getElementById('status').textContent = `${visible.length} of ${rows.length} loaded rows`;
  }

  async function load() {
    document.getElementById('status').textContent = 'Loading...';
    rows = await people.list({ limit: 500 });
    render();
  }

  async function saveNote(recordId) {
    const input = document.querySelector(`[data-note-input="${recordId}"]`);
    const note = input ? input.value : '';
    const updated = await people.update(Number(recordId), { notes: note });
    rows = rows.map((row) => row.id === updated.id ? updated : row);
    render();
  }

  document.getElementById('filter').addEventListener('input', render);
  document.getElementById('refresh').addEventListener('click', () => load().catch(showError));
  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-note-save]');
    if (!button) return;
    saveNote(button.dataset.noteSave).catch(showError);
  });
  function showError(error) {
    document.getElementById('status').textContent = error && error.message ? error.message : String(error);
  }
  load().catch(showError);
</script>
""".strip()


def build_scenario_payload(params: EvalParams) -> dict[str, Any]:
    if params.scenario != "crm_simple_table":
        raise ValueError(f"Unknown scenario: {params.scenario}")
    records = crm_people(params.rows)
    manifest = {
        "contract_version": 1,
        "data_plan": {
            "mode": "capability",
            "bindings": {
                "people": {
                    "kind": "domain",
                    "domain_id": 1,
                    "object_key": "person",
                    "operations": ["schema", "list", "query", "get", "update", "aggregate"],
                }
            },
        },
        "design_contract": {
            "kit": "constellation-app-kit",
            "theme_modes": ["dark", "light"],
        },
    }
    visual_spec = {
        "thumbnail": {
            "label": "Uwear CRM",
            "value": str(params.rows),
            "secondary": "App capsule eval",
        }
    }
    return {
        "scenario": params.scenario,
        "name": "Uwear CRM Simple Tables",
        "description": "Editable CRM table capsule for outreach contacts.",
        "renderer_key": "app-capsule",
        "source_kind": "html",
        "source_code": crm_capsule_source(),
        "manifest": manifest,
        "visual_spec": visual_spec,
        "records": records,
        "viewport": {"width": params.viewport[0], "height": params.viewport[1]},
        "api_latency_ms": params.api_latency_ms,
    }


def compile_and_validate(payload: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    compiled = compile_workspace_app_input(
        action="create",
        name=payload["name"],
        renderer_key=payload["renderer_key"],
        source_kind=payload["source_kind"],
        source_code=payload["source_code"],
        manifest=payload["manifest"],
        visual_spec=payload["visual_spec"],
    )
    report = build_contract_validation_report(
        renderer_key=compiled.renderer_key,
        source_kind=compiled.source_kind,
        source_code=compiled.source_code,
        manifest=compiled.manifest,
        visual_spec=compiled.visual_spec,
        metadata=compiled.metadata,
    )
    return compiled, report


def static_score(payload: dict[str, Any], compiled: Any, report: dict[str, Any]) -> dict[str, Any]:
    source = compiled.source_code or ""
    manifest = compiled.manifest or {}
    bindings = ((manifest.get("data_plan") or {}).get("bindings") or {})
    return {
        "one_tool_call": 1,
        "uses_app_capsule": int(compiled.renderer_key == "app-capsule" and compiled.source_kind == "html"),
        "contract_pass": int(report["status"] == "passed"),
        "capability_bindings": sorted(bindings.keys()),
        "legacy_color_hits": sum(source.count(marker) for marker in LEGACY_SURFACE_MARKERS),
        "source_bytes": len(source.encode("utf-8")),
        "repairs": list(compiled.repairs),
        "contract_errors": list(report.get("errors") or []),
    }


def run_browser_harness(
    payload: dict[str, Any],
    compiled: Any,
    *,
    chrome_path: str | None,
    timeout_ms: int,
    screenshot_path: Path | None,
) -> dict[str, Any]:
    browser_payload = {
        "app": {"id": "eval-app", "key": "uwear-crm-simple-tables", "name": payload["name"], "description": payload["description"]},
        "manifest": compiled.manifest,
        "source_code": compiled.source_code,
        "records": payload["records"],
        "viewport": payload["viewport"],
        "api_latency_ms": payload["api_latency_ms"],
        "timeout_ms": timeout_ms,
        "chrome_path": chrome_path,
        "screenshot_path": str(screenshot_path) if screenshot_path else None,
    }
    with tempfile.TemporaryDirectory(prefix="app-capsule-eval-") as tmp:
        input_path = Path(tmp) / "payload.json"
        input_path.write_text(json.dumps(browser_payload), encoding="utf-8")
        command = ["node", str(HARNESS), "--input", str(input_path)]
        completed = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
        if completed.returncode != 0:
            return {
                "browser_pass": 0,
                "browser_error": completed.stderr.strip() or completed.stdout.strip() or f"node exited {completed.returncode}",
            }
        return json.loads(completed.stdout)


def run_eval_case(
    params: EvalParams,
    *,
    chrome_path: str | None,
    timeout_ms: int,
    skip_browser: bool,
    screenshot_dir: Path | None = None,
) -> dict[str, Any]:
    payload = build_scenario_payload(params)
    compiled, report = compile_and_validate(payload)
    scores = static_score(payload, compiled, report)
    browser_result = {"browser_pass": 1, "skipped": True} if skip_browser else run_browser_harness(
        payload,
        compiled,
        chrome_path=chrome_path,
        timeout_ms=timeout_ms,
        screenshot_path=_screenshot_path(screenshot_dir, params),
    )
    scores.update(browser_result)
    passed = bool(
        scores["uses_app_capsule"]
        and scores["contract_pass"]
        and scores.get("browser_pass")
        and scores.get("console_errors", 0) == 0
        and not scores.get("horizontal_overflow", False)
        and scores.get("note_update_passed", True)
        and scores["legacy_color_hits"] == 0
    )
    return {
        "scenario": params.scenario,
        "rows": params.rows,
        "viewport": f"{params.viewport[0]}x{params.viewport[1]}",
        "api_latency_ms": params.api_latency_ms,
        "run_index": params.run_index,
        "passed": passed,
        "scores": scores,
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    ready_values = [result["scores"].get("bridge_ready_ms") for result in results if result["scores"].get("bridge_ready_ms") is not None]
    mount_values = [result["scores"].get("mount_ms") for result in results if result["scores"].get("mount_ms") is not None]
    return {
        "total": len(results),
        "passed": sum(1 for result in results if result["passed"]),
        "failed": sum(1 for result in results if not result["passed"]),
        "avg_bridge_ready_ms": round(mean(ready_values), 2) if ready_values else None,
        "avg_mount_ms": round(mean(mount_values), 2) if mount_values else None,
    }


def _screenshot_path(screenshot_dir: Path | None, params: EvalParams) -> Path | None:
    if screenshot_dir is None:
        return None
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    viewport = f"{params.viewport[0]}x{params.viewport[1]}"
    return screenshot_dir / f"{params.scenario}-rows{params.rows}-{viewport}-latency{params.api_latency_ms}-run{params.run_index}.png"


def compare_reports(left_path: Path, right_path: Path) -> dict[str, Any]:
    left = json.loads(left_path.read_text(encoding="utf-8"))
    right = json.loads(right_path.read_text(encoding="utf-8"))
    return {
        "left": left.get("summary"),
        "right": right.get("summary"),
        "delta": {
            "passed": (right.get("summary") or {}).get("passed", 0) - (left.get("summary") or {}).get("passed", 0),
            "avg_bridge_ready_ms": _delta((left.get("summary") or {}).get("avg_bridge_ready_ms"), (right.get("summary") or {}).get("avg_bridge_ready_ms")),
            "avg_mount_ms": _delta((left.get("summary") or {}).get("avg_mount_ms"), (right.get("summary") or {}).get("avg_mount_ms")),
        },
    }


def _delta(left: Any, right: Any) -> float | None:
    if left is None or right is None:
        return None
    return round(float(right) - float(left), 2)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run local app-capsule eval scenarios.")
    parser.add_argument("--scenario", default="crm_simple_table")
    parser.add_argument("--rows", default="350", help="Comma-separated row counts, e.g. 50,350,1000")
    parser.add_argument("--viewport", default="1440x900", help="Comma-separated viewports, e.g. 390x844,1440x900")
    parser.add_argument("--api-latency-ms", default="0", help="Comma-separated simulated binding latency values")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--timeout-ms", type=int, default=10000)
    parser.add_argument("--chrome-path")
    parser.add_argument("--skip-browser", action="store_true")
    parser.add_argument("--screenshot-dir", help="Directory for browser screenshots from each measured case")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out")
    parser.add_argument("--compare", help="Compare two JSON reports: before.json,after.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.compare:
        left, right = [Path(item.strip()) for item in args.compare.split(",", 1)]
        print(json.dumps(compare_reports(left, right), indent=2))
        return 0

    results = []
    matrix = itertools.product(
        parse_csv_ints(args.rows),
        parse_viewports(args.viewport),
        parse_csv_ints(args.api_latency_ms),
        range(1, args.runs + 1),
    )
    for rows, viewport, latency, run_index in matrix:
        results.append(
            run_eval_case(
                EvalParams(args.scenario, rows, viewport, latency, run_index),
                chrome_path=args.chrome_path,
                timeout_ms=args.timeout_ms,
                skip_browser=args.skip_browser,
                screenshot_dir=Path(args.screenshot_dir) if args.screenshot_dir else None,
            )
        )

    report = {
        "suite": "app-capsule-lab-v1",
        "summary": summarize(results),
        "results": results,
    }
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"app-capsule-lab-v1: {report['summary']['passed']}/{report['summary']['total']} passed")
        for result in results:
            scores = result["scores"]
            print(
                f"- {result['scenario']} rows={result['rows']} viewport={result['viewport']} "
                f"latency={result['api_latency_ms']}ms pass={result['passed']} "
                f"ready={scores.get('bridge_ready_ms')}ms mount={scores.get('mount_ms')}ms "
                f"calls={scores.get('data_call_count')} errors={scores.get('console_errors')}"
            )
            if not result["passed"]:
                print(json.dumps(scores, indent=2))
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

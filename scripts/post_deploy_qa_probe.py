#!/usr/bin/env python3
"""Verify the runtime symptoms covered by Illo-QA #290/#306/#311/#312.

The live path talks only to Illo's hosted MCP bridge. The coordinator under test
must exercise its registered spawn_worker, GitHub, workspace-data, and Domain
tools; this script reads the resulting run ledger and asserts their behavior.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Iterable, Mapping


REPO_ROOT = Path(__file__).resolve().parents[1]
MCP_CLIENT_ROOT = REPO_ROOT / "tools" / "illo-personal-agent-mcp"
if str(MCP_CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(MCP_CLIENT_ROOT))

from illo_personal_agent_mcp.server import (  # noqa: E402
    IlloBridgeClient,
    IlloBridgeConfig,
    IlloBridgeError,
)


PASS = "PASS"
FAIL = "FAIL"
REQUIRES_LIVE_RUNTIME = "REQUIRES LIVE RUNTIME"
SYMPTOMS = ("311", "306", "312", "290")
TERMINAL_RUN_STATUSES = {"completed", "failed", "canceled", "degraded", "auth_blocked"}


MANUAL_CHECKS = {
    "311": (
        "In a coordinator run, call spawn_worker for a child repo reader; use illo_read "
        "run.get on the returned child_run_id and verify a readable final summary with no "
        "GITHUB_TOKEN/project-context materialization error."
    ),
    "306": (
        "In a coordinator run, call read_github_source(action='get_pull_request') for one "
        "open PR and verify pull_request.mergeable/mergeable_state plus checks or "
        "combined_status are returned without status_code=403."
    ),
    "312": (
        "In a coordinator run, perform one broad query_workspace_data Tracker/Event read; "
        "follow every next_page cursor and verify the final page reports "
        "evidence_health.completeness='complete' without a visible-output-limit error or "
        "compensating time slices."
    ),
    "290": (
        "In a coordinator run, call manage_domain create_record twice for the same tracker "
        "external_id while varying assignee spelling, then query active records for that "
        "external_id and verify both creates return the same record id and exactly one active "
        "row exists."
    ),
}


@dataclass(frozen=True)
class ProbeResult:
    symptom: str
    status: str
    evidence: str

    def render(self) -> str:
        return f"#{self.symptom} {self.status} — evidence: {self.evidence}"


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list | tuple) else []


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _decode_json_values(value: Any) -> list[Any]:
    """Walk one tool event, decoding JSON strings without losing outer evidence."""

    values = [value]
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return values
        values.extend(_decode_json_values(decoded))
    elif isinstance(value, Mapping):
        for child in value.values():
            values.extend(_decode_json_values(child))
    elif isinstance(value, list | tuple):
        for child in value:
            values.extend(_decode_json_values(child))
    return values


def _run_payload(bundle: Mapping[str, Any]) -> dict[str, Any]:
    root = bundle.get("root")
    return _dict(root) if isinstance(root, Mapping) else dict(bundle)


def _run_id(run_payload: Mapping[str, Any]) -> int | None:
    run = _dict(run_payload.get("run"))
    value = run.get("run_id", run.get("id"))
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _tool_name(event: Mapping[str, Any]) -> str:
    payload = _dict(event.get("payload"))
    return _text(payload.get("tool_name") or payload.get("tool")).lower()


def _tool_events(run_payload: Mapping[str, Any], tool_names: Iterable[str]) -> list[dict[str, Any]]:
    expected = {name.lower() for name in tool_names}
    return [
        dict(event)
        for event in _list(run_payload.get("tool_events"))
        if isinstance(event, Mapping) and _tool_name(event) in expected
    ]


def _event_objects(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for event in events:
        for value in _decode_json_values(_dict(event).get("payload")):
            if isinstance(value, Mapping):
                objects.append(dict(value))
    return objects


def _contains_error_blob(value: Any, terms: Iterable[str]) -> bool:
    blob = json.dumps(value, ensure_ascii=False, default=str).lower()
    return any(term.lower() in blob for term in terms)


def _artifact_summary(run_payload: Mapping[str, Any]) -> str:
    for artifact in reversed(_list(run_payload.get("artifacts"))):
        if not isinstance(artifact, Mapping):
            continue
        if _text(artifact.get("artifact_type")).lower() != "final_answer":
            continue
        text = _text(artifact.get("text"))
        if text:
            return text
    return ""


def check_311(bundle: Mapping[str, Any]) -> ProbeResult:
    root = _run_payload(bundle)
    events = _tool_events(root, {"spawn_worker"})
    objects = _event_objects(events)
    child_id = next(
        (
            value.get("child_run_id") or value.get("run_id")
            for value in objects
            if value.get("child_run_id") or value.get("run_id")
        ),
        None,
    )
    if not events or child_id is None:
        return ProbeResult("311", FAIL, "no spawn_worker event with a child_run_id")

    children = _dict(bundle.get("child_runs"))
    child = _dict(children.get(str(child_id), children.get(child_id)))
    if not child:
        return ProbeResult("311", FAIL, f"child run {child_id} was not available through run.get")
    summary = _artifact_summary(child)
    forbidden = (
        "github_token materialization",
        "github_token could not be materialized",
        "project_context_materialization",
    )
    if _contains_error_blob(child, forbidden):
        return ProbeResult("311", FAIL, f"child run {child_id} contains a credential materialization error")
    status = _text(_dict(child.get("run")).get("status")).lower()
    if status != "completed" or len(summary) < 20:
        return ProbeResult(
            "311",
            FAIL,
            f"child run {child_id} status={status or 'unknown'} readable_summary={bool(summary)}",
        )
    return ProbeResult("311", PASS, f"child run {child_id} completed; summary={summary[:180]}")


def check_306(bundle: Mapping[str, Any]) -> ProbeResult:
    root = _run_payload(bundle)
    events = _tool_events(root, {"read_github_source"})
    objects = _event_objects(events)
    candidates = [
        value
        for value in objects
        if isinstance(value.get("pull_request"), Mapping)
        and (isinstance(value.get("checks"), Mapping) or "combined_status" in value)
    ]
    if not candidates:
        denied = next((value for value in objects if value.get("status_code") == 403), None)
        detail = f"GitHub returned 403: {_text(denied.get('error'))}" if denied else "no PR detail + CI payload"
        return ProbeResult("306", FAIL, detail)
    payload = candidates[-1]
    if payload.get("status_code") == 403 or payload.get("error"):
        return ProbeResult("306", FAIL, f"PR read failed: {_text(payload.get('error')) or 'HTTP 403'}")
    pull_request = _dict(payload.get("pull_request"))
    checks = _dict(payload.get("checks"))
    if "mergeable" not in pull_request and "mergeable_state" not in pull_request:
        return ProbeResult("306", FAIL, "PR payload omitted mergeability")
    ci_status = payload.get("combined_status", checks.get("status"))
    if ci_status is None:
        return ProbeResult("306", FAIL, "PR payload omitted CI/combined status")
    return ProbeResult(
        "306",
        PASS,
        f"PR #{pull_request.get('number', '?')} mergeable={pull_request.get('mergeable')} "
        f"mergeable_state={pull_request.get('mergeable_state')} CI={ci_status}",
    )


def _is_tracker_event_page(value: Mapping[str, Any]) -> bool:
    sources = _dict(value.get("sources"))
    return {"domain_records", "domain_events"}.issubset(sources)


def check_312(bundle: Mapping[str, Any]) -> ProbeResult:
    root = _run_payload(bundle)
    events = _tool_events(root, {"query_workspace_data"})
    objects = [value for value in _event_objects(events) if _is_tracker_event_page(value)]
    if not objects:
        return ProbeResult("312", FAIL, "no broad Tracker/Event reader page was recorded")
    if _contains_error_blob(
        objects,
        ("visible-output limit", "visible output limit", "output too large", "compensating slice"),
    ):
        return ProbeResult("312", FAIL, "reader hit the visible-output limit or needed compensating slices")
    completed = [
        value
        for value in objects
        if value.get("next_page") in {None, ""}
        and _dict(value.get("evidence_health")).get("status") == "ok"
        and _dict(value.get("evidence_health")).get("completeness") == "complete"
        and not value.get("error")
    ]
    if not completed:
        return ProbeResult("312", FAIL, f"{len(objects)} page(s) recorded but no complete final page")
    total = sum(
        int(value.get("total_count") or value.get("returned") or 0)
        for value in completed[-1:]
    )
    return ProbeResult(
        "312",
        PASS,
        f"{len(objects)} Tracker/Event page(s) completed; final completeness=complete count={total}",
    )


def _record_from_object(value: Mapping[str, Any]) -> dict[str, Any] | None:
    record = value.get("record")
    return dict(record) if isinstance(record, Mapping) else None


def check_290(bundle: Mapping[str, Any]) -> ProbeResult:
    root = _run_payload(bundle)
    events = _tool_events(root, {"manage_domain"})
    objects = _event_objects(events)
    created = [record for value in objects if (record := _record_from_object(value))]
    record_ids = [record.get("id") for record in created if record.get("id") is not None]
    query_pages = [value for value in objects if isinstance(value.get("records"), list)]
    if len(record_ids) < 2:
        return ProbeResult("290", FAIL, "fewer than two create_record receipts were recorded")
    first_data = _dict(created[0].get("data"))
    second_data = _dict(created[1].get("data"))
    external_ids = [_text(first_data.get("external_id")), _text(second_data.get("external_id"))]
    assignees = [_text(first_data.get("assignee")), _text(second_data.get("assignee"))]
    if not all(external_ids) or external_ids[0].casefold() != external_ids[1].casefold():
        return ProbeResult("290", FAIL, f"create receipts did not share one external_id: {external_ids}")
    if not all(assignees) or assignees[0] == assignees[1]:
        return ProbeResult("290", FAIL, f"create receipts did not exercise spelling variance: {assignees}")
    if len(set(record_ids[:2])) != 1:
        return ProbeResult("290", FAIL, f"duplicate creates forked record ids {record_ids[:2]}")
    if not query_pages:
        return ProbeResult("290", FAIL, "no active-row query followed the duplicate creates")
    active_records = [record for record in query_pages[-1]["records"] if isinstance(record, Mapping)]
    if len(active_records) != 1 or active_records[0].get("id") != record_ids[0]:
        return ProbeResult(
            "290",
            FAIL,
            f"active-row query returned ids {[record.get('id') for record in active_records]}",
        )
    active_external_id = _text(_dict(active_records[0].get("data")).get("external_id"))
    if active_external_id.casefold() != external_ids[0].casefold():
        return ProbeResult(
            "290",
            FAIL,
            f"active-row query returned external_id={active_external_id or 'missing'}",
        )
    assignee = _dict(active_records[0].get("data")).get("assignee")
    return ProbeResult(
        "290",
        PASS,
        f"both creates reused record {record_ids[0]}; active rows=1; assignee={assignee}",
    )


CHECKERS = {"311": check_311, "306": check_306, "312": check_312, "290": check_290}


def evaluate_probe(symptom: str, bundle: Mapping[str, Any]) -> ProbeResult:
    result = CHECKERS[symptom](bundle)
    root_id = _run_id(_run_payload(bundle))
    evidence = f"run {root_id}; {result.evidence}" if root_id is not None else result.evidence
    return ProbeResult(result.symptom, result.status, evidence)


def _event(tool: str, result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_type": "run.tool_completed",
        "payload": {"tool_name": tool, "result": json.dumps(result)},
    }


def self_test_bundle() -> dict[str, Any]:
    """Synthetic run.get receipts that exercise every assertion without network access."""

    return {
        "root": {
            "run": {"run_id": 900, "status": "completed"},
            "tool_events": [
                _event("spawn_worker", {"ok": True, "child_run_id": 901}),
                _event(
                    "read_github_source",
                    {
                        "pull_request": {
                            "number": 318,
                            "mergeable": True,
                            "mergeable_state": "clean",
                        },
                        "checks": {"status": "success", "total": 4},
                        "combined_status": "success",
                    },
                ),
                _event(
                    "query_workspace_data",
                    {
                        "sources": {"domain_records": [{"id": 1}], "domain_events": [{"id": 2}]},
                        "total_count": 2,
                        "next_page": None,
                        "evidence_health": {"status": "ok", "completeness": "complete"},
                    },
                ),
                _event("manage_domain", {"record": {"id": 77, "data": {"external_id": "PR-318", "assignee": "Reda"}}}),
                _event("manage_domain", {"record": {"id": 77, "data": {"external_id": "PR-318", "assignee": "reda"}}}),
                _event(
                    "manage_domain",
                    {
                        "records": [
                            {"id": 77, "data": {"external_id": "PR-318", "assignee": "reda"}}
                        ],
                        "next_page": None,
                        "evidence_health": {"status": "ok", "completeness": "complete"},
                    },
                ),
            ],
            "artifacts": [{"artifact_type": "final_answer", "text": "Root probe completed."}],
        },
        "child_runs": {
            "901": {
                "run": {"run_id": 901, "status": "completed"},
                "tool_events": [],
                "artifacts": [
                    {
                        "artifact_type": "final_answer",
                        "text": "Readable child summary from the repository reader.",
                    }
                ],
            }
        },
    }


def _prompt_for(symptom: str, args: argparse.Namespace) -> str:
    common = (
        f"Run the [Illo-QA] #{symptom} post-deploy behavior probe. This is a runtime "
        "verification, not a code review. Use the named coordinator tools and leave their "
        "receipts in this AgentRun so run.get can verify them. Do not claim PASS without the "
        "tool evidence. "
    )
    if symptom == "311":
        return common + (
            "Call spawn_worker once with a child repo-reader objective for "
            f"{args.repo}. After it finishes, read the child run and summarize one repository fact."
        )
    if symptom == "306":
        return common + (
            "Call read_github_source(action='get_pull_request') for "
            f"repo={args.repo}, pull_number={args.pull_number}. Report mergeability and CI status."
        )
    if symptom == "312":
        return common + (
            "Call query_workspace_data for a broad Tracker/Event read covering domain_records "
            "and domain_events. Follow next_page until it is null. Do not replace pagination "
            "with time slices; report the final evidence_health completeness."
        )
    return common + (
        "Using manage_domain, call create_record twice in the existing tracker with "
        f"domain_id={args.tracker_domain_id}, object_key={args.tracker_object_key}, "
        f"external_id={args.external_id!r}. Use assignee={args.assignee_a!r} first and "
        f"assignee={args.assignee_b!r} second, keeping all other required fields valid and "
        "identical. Then query active records for that exact external_id and leave the two "
        "create receipts plus the query receipt in this run."
    )


def _missing_live_arguments(symptom: str, args: argparse.Namespace) -> list[str]:
    required: dict[str, tuple[str, ...]] = {
        "311": ("repo",),
        "306": ("repo", "pull_number"),
        "312": (),
        "290": (
            "tracker_domain_id",
            "tracker_object_key",
            "external_id",
            "assignee_a",
            "assignee_b",
        ),
    }
    return [name.replace("_", "-") for name in required[symptom] if not getattr(args, name)]


def _trigger_run(client: IlloBridgeClient, symptom: str, args: argparse.Namespace) -> int:
    response = client.act(
        "thread.post_message",
        arguments={
            "thread_id": args.thread_id,
            "body": _prompt_for(symptom, args),
            "trigger_illo": True,
        },
        reason=f"Illo-QA #{symptom} post-deploy behavior verification",
        idempotency_key=f"illo-qa-{symptom}-{int(time.time())}",
        metadata={"source": "post_deploy_qa_probe", "symptom": symptom},
    )
    candidates = [response, _dict(response.get("trigger")), _dict(response.get("result"))]
    for candidate in candidates:
        value = candidate.get("run_id")
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    raise IlloBridgeError("thread trigger did not return a run_id")


def _read_run(client: IlloBridgeClient, run_id: int) -> dict[str, Any]:
    return client.read(
        "run.get",
        arguments={
            "run_id": run_id,
            "include_tool_events": True,
            "include_artifacts": True,
            "limit": 200,
        },
    )


def _wait_for_run(
    client: IlloBridgeClient,
    run_id: int,
    *,
    timeout_seconds: float,
    poll_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = _read_run(client, run_id)
        status = _text(_dict(latest.get("run")).get("status")).lower()
        if status in TERMINAL_RUN_STATUSES:
            return latest
        time.sleep(poll_seconds)
    status = _text(_dict(latest.get("run")).get("status")) or "unknown"
    raise IlloBridgeError(f"run {run_id} did not finish within {timeout_seconds}s (status={status})")


def _hydrate_child_runs(
    client: IlloBridgeClient,
    bundle: dict[str, Any],
    *,
    timeout_seconds: float,
    poll_seconds: float,
) -> None:
    events = _tool_events(_run_payload(bundle), {"spawn_worker"})
    child_ids = {
        int(value.get("child_run_id"))
        for value in _event_objects(events)
        if str(value.get("child_run_id") or "").isdigit()
    }
    children = bundle.setdefault("child_runs", {})
    for child_id in child_ids:
        children[str(child_id)] = _wait_for_run(
            client,
            child_id,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
        )


def _live_bundle(
    client: IlloBridgeClient,
    symptom: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    run_id = args.run_id or _trigger_run(client, symptom, args)
    root = _wait_for_run(
        client,
        run_id,
        timeout_seconds=args.wait_timeout,
        poll_seconds=args.poll_interval,
    )
    bundle: dict[str, Any] = {"root": root, "child_runs": {}}
    if symptom == "311":
        _hydrate_child_runs(
            client,
            bundle,
            timeout_seconds=args.wait_timeout,
            poll_seconds=args.poll_interval,
        )
    return bundle


def _selected_symptoms(args: argparse.Namespace) -> list[str]:
    return list(dict.fromkeys(args.symptom or SYMPTOMS))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symptom", action="append", choices=SYMPTOMS, help="Run one symptom; repeat as needed.")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run all assertions against offline synthetic run receipts.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print exact live checks without network access.")
    parser.add_argument(
        "--evidence-file",
        type=Path,
        help="Assert an exported run.get bundle instead of using the network.",
    )
    parser.add_argument("--target", help="Deployed Illo base URL (or set ILLO_BASE_URL).")
    parser.add_argument(
        "--token-env",
        default="ILLO_BRIDGE_TOKEN",
        help="Environment variable containing the bridge token.",
    )
    parser.add_argument("--thread-id", help="Existing thread to receive the live probe prompt.")
    parser.add_argument("--run-id", type=int, help="Inspect an existing verifying AgentRun instead of triggering one.")
    parser.add_argument("--repo", help="GitHub owner/repo used by #311 and #306.")
    parser.add_argument("--pull-number", type=int, help="Open PR number used by #306.")
    parser.add_argument("--tracker-domain-id", type=int, help="Existing Tracker Domain id used by #290.")
    parser.add_argument("--tracker-object-key", help="Tracker object key used by #290.")
    parser.add_argument("--external-id", help="Disposable or existing external_id used by #290.")
    parser.add_argument("--assignee-a", help="First assignee spelling used by #290.")
    parser.add_argument("--assignee-b", help="Second assignee spelling variant used by #290.")
    parser.add_argument("--wait-timeout", type=float, default=600.0, help="Seconds to wait for each live run.")
    parser.add_argument("--poll-interval", type=float, default=5.0, help="Seconds between run.get polls.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    symptoms = _selected_symptoms(args)
    if args.self_test:
        bundle = self_test_bundle()
        results = [evaluate_probe(symptom, bundle) for symptom in symptoms]
    elif args.evidence_file:
        bundle = json.loads(args.evidence_file.read_text(encoding="utf-8"))
        results = [evaluate_probe(symptom, _dict(bundle)) for symptom in symptoms]
    elif args.dry_run or not (args.target or os.environ.get("ILLO_BASE_URL")):
        results = [
            ProbeResult(symptom, REQUIRES_LIVE_RUNTIME, MANUAL_CHECKS[symptom])
            for symptom in symptoms
        ]
    else:
        target = _text(args.target or os.environ.get("ILLO_BASE_URL"))
        token = _text(os.environ.get(args.token_env))
        if not token:
            results = [
                ProbeResult(
                    symptom,
                    REQUIRES_LIVE_RUNTIME,
                    f"set {args.token_env}; {MANUAL_CHECKS[symptom]}",
                )
                for symptom in symptoms
            ]
        elif not args.run_id and not args.thread_id:
            results = [
                ProbeResult(
                    symptom,
                    REQUIRES_LIVE_RUNTIME,
                    f"pass --thread-id or --run-id; {MANUAL_CHECKS[symptom]}",
                )
                for symptom in symptoms
            ]
        else:
            client = IlloBridgeClient(
                IlloBridgeConfig(target, token, timeout_seconds=max(10.0, args.poll_interval * 2))
            )
            results = []
            for symptom in symptoms:
                missing = _missing_live_arguments(symptom, args) if not args.run_id else []
                if missing:
                    results.append(
                        ProbeResult(
                            symptom,
                            REQUIRES_LIVE_RUNTIME,
                            f"missing {', '.join('--' + item for item in missing)}; "
                            f"{MANUAL_CHECKS[symptom]}",
                        )
                    )
                    continue
                try:
                    results.append(
                        evaluate_probe(symptom, _live_bundle(client, symptom, args))
                    )
                except (IlloBridgeError, OSError, ValueError) as exc:
                    results.append(ProbeResult(symptom, FAIL, _text(exc)))

    for result in results:
        print(result.render())
    return 1 if any(result.status == FAIL for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())

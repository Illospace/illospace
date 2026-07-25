from __future__ import annotations

from unittest.mock import patch

import pytest


def _capture_manifests():
    records: list[dict] = []
    completions: list[dict] = []

    def record(manifest):
        from brain.systems.runs.actions import ActionManifestCreate

        assert isinstance(manifest, ActionManifestCreate)
        records.append(manifest.to_db_values())
        return len(records)

    def complete(manifest_id, **kwargs):
        completions.append({"manifest_id": manifest_id, **kwargs})

    return records, completions, record, complete


def _manifest_context():
    return {
        "actor": "worker-a",
        "actor_id": None,
        "actor_kind": "agent",
        "org_id": "org-1",
        "run_id": 42,
        "trace_id": "trace-42",
        "worker_name": "worker-a",
        "idea_id": "idea-42",
    }


def test_action_manifest_create_preserves_payload_shape():
    from brain.systems.runs.actions import ActionManifestCreate, ActionTarget

    manifest = ActionManifestCreate(
        actor="agent",
        actor_kind="agent",
        tool_name="write_file",
        target=ActionTarget({"path": "notes/audit.txt", "content_bytes": 8}),
        risk="medium",
        reversibility="reversible_with_version_control",
        expected_effect="write file contents",
        idempotency_key="a" * 64,
        metadata_={"run_id": "run-1", "argument_keys": ["path", "content"]},
    )

    payload = manifest.to_db_values()
    assert payload["target"] == {"path": "notes/audit.txt", "content_bytes": 8}
    assert payload["metadata_"] == {"run_id": "run-1", "argument_keys": ["path", "content"]}
    assert payload["approval_required"] is False
    assert payload["approval_requirement"] == "not_required_permissive_audit"
    assert payload["policy_result"] == "allow_audit"
    assert payload["policy_mode"] == "permissive_audit"
    assert payload["outcome_status"] == "started"
    assert "outcome_error" not in payload
    assert "completed_at" not in payload


def test_build_action_manifest_returns_validated_payload(monkeypatch):
    from brain.systems.runs.actions import ActionManifestCreate
    from brain.systems.runs import tool_handlers

    monkeypatch.setattr(
        tool_handlers,
        "_current_manifest_context",
        lambda: {
            "actor": "worker-a",
            "actor_id": None,
            "actor_kind": "agent",
            "org_id": None,
            "run_id": 42,
            "trace_id": "trace-42",
            "worker_name": "worker-a",
            "idea_id": "idea-42",
        },
    )

    manifest = tool_handlers._build_action_manifest("exec_command", ("pwd",), {})

    assert isinstance(manifest, ActionManifestCreate)
    assert manifest.to_db_values()["target"] == {
        "command": "pwd",
        "working_dir": None,
        "workspace": None,
    }
    assert manifest.policy_result == "allow_audit"


def test_action_manifest_model_registered():
    from brain.platform.db.base import Base
    from brain.platform.db.models.run import ActionManifest

    assert ActionManifest.__tablename__ == "action_manifests"
    assert "action_manifests" in Base.metadata.tables
    columns = Base.metadata.tables["action_manifests"].columns
    for column in (
        "actor",
        "org_id",
        "run_id",
        "tool_name",
        "target",
        "risk",
        "reversibility",
        "expected_effect",
        "approval_requirement",
        "idempotency_key",
        "policy_result",
    ):
        assert column in columns


def test_shell_command_records_action_manifest(tmp_path):
    from brain.systems.runs.tool_handlers import _get_tool_handlers

    records, completions, record, complete = _capture_manifests()
    handlers = _get_tool_handlers(workspace_root=str(tmp_path))

    with patch("brain.systems.runs.actions.record_action_manifest", side_effect=record), \
         patch("brain.systems.runs.actions.complete_action_manifest", side_effect=complete):
        result = handlers["exec_command"]("pwd")

    assert result["exit_code"] == 0
    assert records[0]["tool_name"] == "exec_command"
    assert records[0]["target"]["command"] == "pwd"
    assert records[0]["policy_result"] == "allow_audit"
    assert records[0]["approval_required"] is False
    assert completions == [{"manifest_id": 1, "outcome_status": "succeeded", "outcome_error": None}]


def test_file_write_records_action_manifest(tmp_path):
    from brain.systems.runs.tool_handlers import _get_tool_handlers

    records, completions, record, complete = _capture_manifests()
    handlers = _get_tool_handlers(workspace_root=str(tmp_path))

    with patch("brain.systems.runs.actions.record_action_manifest", side_effect=record), \
         patch("brain.systems.runs.actions.complete_action_manifest", side_effect=complete):
        result = handlers["write_file"]("notes/audit.txt", "audit me")

    assert result["written"] is True
    assert records[0]["tool_name"] == "write_file"
    assert records[0]["target"]["path"] == "notes/audit.txt"
    assert records[0]["target"]["content_bytes"] == len("audit me".encode())
    assert completions[0]["outcome_status"] == "succeeded"


def test_read_only_tool_does_not_record_manifest(tmp_path):
    from brain.systems.runs.tool_handlers import _get_tool_handlers

    target = tmp_path / "existing.txt"
    target.write_text("safe read\n")
    records, completions, record, complete = _capture_manifests()
    handlers = _get_tool_handlers(workspace_root=str(tmp_path))

    with patch("brain.systems.runs.actions.record_action_manifest", side_effect=record), \
         patch("brain.systems.runs.actions.complete_action_manifest", side_effect=complete):
        result = handlers["read_file"]("existing.txt")

    assert "safe read" in result["content"]
    assert records == []
    assert completions == []


def test_failed_side_effect_is_still_audited(tmp_path):
    from brain.systems.runs.tool_handlers import _get_tool_handlers

    records, completions, record, complete = _capture_manifests()
    handlers = _get_tool_handlers(workspace_root=str(tmp_path))

    with patch("brain.systems.runs.actions.record_action_manifest", side_effect=record), \
         patch("brain.systems.runs.actions.complete_action_manifest", side_effect=complete):
        result = handlers["write_file"]("../outside.txt", "blocked")

    assert "error" in result
    assert "escapes workspace" in result["error"]
    assert records[0]["tool_name"] == "write_file"
    assert records[0]["target"]["path"] == "../outside.txt"
    assert completions == [{
        "manifest_id": 1,
        "outcome_status": "failed",
        "outcome_error": result["error"],
    }]


def test_low_risk_action_executes_under_enforced_policy(monkeypatch):
    from brain.systems.runs.actions import wrap_action_manifest_audit

    monkeypatch.setenv("AGENT_ACTION_POLICY_MODE", "enforce")
    records, completions, record, complete = _capture_manifests()
    calls = []

    def handler(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "url": kwargs["url"]}

    wrapped = wrap_action_manifest_audit("web_fetch", handler, context_factory=_manifest_context)

    with patch("brain.systems.runs.actions.record_action_manifest", side_effect=record), \
         patch("brain.systems.runs.actions.complete_action_manifest", side_effect=complete):
        result = wrapped(url="https://example.com")

    assert result == {"ok": True, "url": "https://example.com"}
    assert calls == [{"url": "https://example.com"}]
    assert records[0]["tool_name"] == "web_fetch"
    assert records[0]["risk"] == "low"
    assert records[0]["policy_result"] == "allow_audit"
    assert records[0]["policy_mode"] == "enforce"
    assert records[0]["approval_required"] is False
    assert completions == [{"manifest_id": 1, "outcome_status": "succeeded", "outcome_error": None}]


@pytest.mark.asyncio
async def test_action_manifest_wrapper_offloads_sync_handler(monkeypatch):
    import asyncio
    import time

    from brain.systems.runs.actions import wrap_action_manifest_audit

    monkeypatch.setenv("AGENT_ACTION_POLICY_MODE", "enforce")
    records, completions, record, complete = _capture_manifests()
    stop_ticker = asyncio.Event()
    ticker_count = 0

    async def ticker():
        nonlocal ticker_count
        while not stop_ticker.is_set():
            ticker_count += 1
            await asyncio.sleep(0.01)

    def handler(**kwargs):
        time.sleep(0.1)
        return {"ok": True, **kwargs}

    wrapped = wrap_action_manifest_audit("web_fetch", handler, context_factory=_manifest_context)
    ticker_task = asyncio.create_task(ticker())
    try:
        with patch("brain.systems.runs.actions.record_action_manifest", side_effect=record), \
             patch("brain.systems.runs.actions.complete_action_manifest", side_effect=complete):
            result = await wrapped(url="https://example.com")
    finally:
        stop_ticker.set()
        await ticker_task

    assert result == {"ok": True, "url": "https://example.com"}
    assert ticker_count >= 3
    assert len(records) == 1
    assert completions == [{"manifest_id": 1, "outcome_status": "succeeded", "outcome_error": None}]


def test_denied_action_does_not_invoke_handler(monkeypatch):
    from brain.systems.runs.actions import wrap_action_manifest_audit

    monkeypatch.setenv("AGENT_ACTION_POLICY_MODE", "enforce")
    records, completions, record, complete = _capture_manifests()
    calls = []

    def handler(command):
        calls.append(command)
        return {"exit_code": 0}

    wrapped = wrap_action_manifest_audit("exec_command", handler, context_factory=_manifest_context)

    with patch("brain.systems.runs.actions.record_action_manifest", side_effect=record), \
         patch("brain.systems.runs.actions.complete_action_manifest", side_effect=complete):
        result = wrapped("git reset --hard")

    assert calls == []
    assert result["blocked"] is True
    assert result["policy_result"] == "deny"
    assert result["policy_mode"] == "enforce"
    assert result["approval_required"] is False
    assert "denied" in result["error"].lower()
    assert records[0]["tool_name"] == "exec_command"
    assert records[0]["policy_result"] == "deny"
    assert records[0]["approval_requirement"] == "denied_by_policy"
    assert completions == [{
        "manifest_id": 1,
        "outcome_status": "failed",
        "outcome_error": result["error"],
    }]


@pytest.mark.parametrize(
    "command",
    [
        "git reset --hard",
        "git checkout -- brain/app/api/main.py",
        "rm -rf /tmp/illo-danger",
        "kubectl delete deployment brain",
        "terraform destroy -auto-approve",
    ],
)
def test_destructive_shell_commands_are_denied_under_enforced_policy(monkeypatch, command):
    from brain.systems.runs.actions import evaluate_action_policy

    monkeypatch.setenv("AGENT_ACTION_POLICY_MODE", "enforce")

    decision = evaluate_action_policy("exec_command", (command,), {})

    assert decision is not None
    assert decision.result.value == "deny"
    assert decision.approval_requirement == "denied_by_policy"


def test_action_policy_defaults_to_enforce_outside_local_context(monkeypatch):
    from brain.systems.runs.actions import current_action_policy_mode

    monkeypatch.delenv("AGENT_ACTION_POLICY_MODE", raising=False)
    monkeypatch.delenv("ILLO_ACTION_POLICY_MODE", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("ILLO_ENV", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    assert current_action_policy_mode() == "enforce"


def test_unknown_action_policy_mode_fails_closed(monkeypatch):
    from brain.systems.runs.actions import current_action_policy_mode

    monkeypatch.setenv("AGENT_ACTION_POLICY_MODE", "definitely-not-a-mode")

    assert current_action_policy_mode() == "enforce"


def test_high_risk_action_is_audited_autonomously(monkeypatch):
    from brain.systems.runs.actions import wrap_action_manifest_audit

    monkeypatch.setenv("AGENT_ACTION_POLICY_MODE", "enforce")
    records, completions, record, complete = _capture_manifests()
    calls = []

    def handler(**kwargs):
        calls.append(kwargs)
        return {"clicked": True}

    wrapped = wrap_action_manifest_audit("browser", handler, context_factory=_manifest_context)

    with patch("brain.systems.runs.actions.record_action_manifest", side_effect=record), \
         patch("brain.systems.runs.actions.complete_action_manifest", side_effect=complete):
        result = wrapped(action="click", selector="#submit")

    assert result == {"clicked": True}
    assert calls == [{"action": "click", "selector": "#submit"}]
    assert records[0]["tool_name"] == "browser"
    assert records[0]["policy_result"] == "allow_audit"
    assert records[0]["approval_required"] is False
    assert records[0]["approval_requirement"] == "not_required_autonomous_policy"
    assert completions == [{
        "manifest_id": 1,
        "outcome_status": "succeeded",
        "outcome_error": None,
    }]


def test_permissive_audit_mode_allows_deny_class_actions_during_rollout(monkeypatch):
    from brain.systems.runs.actions import wrap_action_manifest_audit

    monkeypatch.setenv("AGENT_ACTION_POLICY_MODE", "permissive_audit")
    records, completions, record, complete = _capture_manifests()
    calls = []

    def handler(command):
        calls.append(command)
        return {"exit_code": 0}

    wrapped = wrap_action_manifest_audit("exec_command", handler, context_factory=_manifest_context)

    with patch("brain.systems.runs.actions.record_action_manifest", side_effect=record), \
         patch("brain.systems.runs.actions.complete_action_manifest", side_effect=complete):
        result = wrapped("git reset --hard")

    assert result == {"exit_code": 0}
    assert calls == ["git reset --hard"]
    assert records[0]["policy_result"] == "allow_audit"
    assert records[0]["policy_mode"] == "permissive_audit"
    assert records[0]["approval_required"] is False
    assert "permissive audit mode" in records[0]["metadata_"]["policy_reason"]
    assert completions == [{"manifest_id": 1, "outcome_status": "succeeded", "outcome_error": None}]


def test_manage_cycle_mutations_are_audited():
    from brain.systems.runs.tool_handlers import _get_tool_handlers

    records, completions, record, complete = _capture_manifests()
    handlers = _get_tool_handlers()

    with patch("brain.systems.runs.actions.record_action_manifest", side_effect=record), \
         patch("brain.systems.runs.actions.complete_action_manifest", side_effect=complete), \
         patch("brain.systems.runs.tool_handlers._handle_manage_cycle", return_value={"ok": True}):
        result = handlers["manage_cycle"](
            action="create",
            name="Morning review",
            schedule_expr="0 8 * * *",
        )

    assert result == {"ok": True}
    assert records[0]["tool_name"] == "manage_cycle"
    assert records[0]["risk"] == "high"
    assert records[0]["reversibility"] == "reversible"
    assert records[0]["expected_effect"] == "mutate a scheduled cycle"
    assert records[0]["target"]["action"] == "create"
    assert records[0]["target"]["schedule"] == "0 8 * * *"
    assert completions == [{"manifest_id": 1, "outcome_status": "succeeded", "outcome_error": None}]


def test_manage_cycle_list_is_not_audited():
    from brain.systems.runs.tool_handlers import _get_tool_handlers

    records, completions, record, complete = _capture_manifests()
    handlers = _get_tool_handlers()

    with patch("brain.systems.runs.actions.record_action_manifest", side_effect=record), \
         patch("brain.systems.runs.actions.complete_action_manifest", side_effect=complete), \
         patch(
             "brain.systems.runs.tool_handlers._handle_manage_cycle",
             return_value={"cycles": []},
         ):
        result = handlers["manage_cycle"](action="list")

    assert result == {"cycles": []}
    assert records == []
    assert completions == []


def test_manage_cycle_usage_summary_is_not_audited():
    from brain.systems.runs.tool_handlers import _get_tool_handlers

    records, completions, record, complete = _capture_manifests()
    handlers = _get_tool_handlers()

    with patch("brain.systems.runs.actions.record_action_manifest", side_effect=record), \
         patch("brain.systems.runs.actions.complete_action_manifest", side_effect=complete), \
         patch(
             "brain.systems.runs.tool_handlers._handle_manage_cycle",
             return_value={"usage_summary": {"cycles": []}},
         ):
        result = handlers["manage_cycle"](action="usage_summary", days=7)

    assert result == {"usage_summary": {"cycles": []}}
    assert records == []
    assert completions == []


def test_tool_registration_normalizes_legacy_policy_strings():
    from brain.systems.runs.tool_catalog.metadata import (
        ToolAvailability,
        ToolParallelSafety,
        ToolPermission,
        ToolRegistration,
        ToolReversibility,
        ToolRiskClass,
        ToolSideEffectClass,
    )

    registration = ToolRegistration(
        name="example_write",
        schema={},
        availability=("coordinator", "worker", "worker"),
        permission="write_workspace",
        risk_class="medium",
        side_effect_class="file_write",
        reversibility="reversible_with_version_control",
        parallel_safety="safe",
        action_manifest=True,
        expected_effect="write a workspace file",
    )

    assert registration.availability == (
        ToolAvailability.COORDINATOR,
        ToolAvailability.WORKER,
    )
    assert registration.permission is ToolPermission.WRITE_WORKSPACE
    assert registration.risk_class is ToolRiskClass.MEDIUM
    assert registration.side_effect_class is ToolSideEffectClass.FILE_WRITE
    assert registration.reversibility is ToolReversibility.REVERSIBLE_WITH_VERSION_CONTROL
    assert registration.parallel_safety is ToolParallelSafety.SAFE


def test_tool_registration_rejects_unknown_policy_values():
    from brain.systems.runs.tool_catalog.metadata import ToolRegistration

    with pytest.raises(ValueError, match="Invalid permission"):
        ToolRegistration(
            name="bad_tool",
            schema={},
            permission="god_mode",
        )


def test_registry_policy_metadata_is_typed_at_import():
    from brain.systems.runs.tool_catalog.metadata import (
        ToolAvailability,
        ToolParallelSafety,
        ToolPermission,
        ToolReversibility,
        ToolRiskClass,
        ToolSideEffectClass,
    )
    from brain.systems.runs.tool_catalog.registry import all_tool_registrations

    for registration in all_tool_registrations().values():
        assert registration.availability
        assert all(isinstance(role, ToolAvailability) for role in registration.availability)
        assert isinstance(registration.permission, ToolPermission)
        assert isinstance(registration.risk_class, ToolRiskClass)
        assert isinstance(registration.side_effect_class, ToolSideEffectClass)
        assert isinstance(registration.reversibility, ToolReversibility)
        assert isinstance(registration.parallel_safety, ToolParallelSafety)


def test_policy_serialization_preserves_plain_strings():
    from brain.systems.runs.tool_catalog.registry import action_policy_for_tool, get_tool_registration

    registration = get_tool_registration("write_file")
    assert registration is not None

    payload = registration.to_permission_payload()
    assert payload["availability"] == ["coordinator", "worker"]
    assert type(payload["availability"][0]) is str
    assert payload["permission"] == "write_workspace"
    assert type(payload["permission"]) is str
    assert payload["risk_class"] == "medium"
    assert type(payload["risk_class"]) is str
    assert payload["side_effect_class"] == "file_write"
    assert type(payload["side_effect_class"]) is str
    assert payload["reversibility"] == "reversible_with_version_control"
    assert type(payload["reversibility"]) is str
    assert payload["parallel_safety"] == "serial"
    assert type(payload["parallel_safety"]) is str

    policy = action_policy_for_tool("write_file")
    assert policy == {
        "risk": "medium",
        "reversibility": "reversible_with_version_control",
        "expected_effect": "write file contents",
    }
    assert all(type(value) is str for value in policy.values())


def test_parallel_and_action_surfaces_are_derived_from_typed_metadata():
    from brain.systems.runs.tool_catalog.metadata import ToolParallelSafety
    from brain.systems.runs.tool_catalog.registry import (
        action_manifest_tool_names,
        get_tool_registration,
        parallel_safe_tool_names,
    )

    read_file = get_tool_registration("read_file")
    assert read_file is not None
    assert read_file.parallel_safety is ToolParallelSafety.SAFE

    assert "read_file" in parallel_safe_tool_names(scope="batch")
    assert "write_file" in action_manifest_tool_names()

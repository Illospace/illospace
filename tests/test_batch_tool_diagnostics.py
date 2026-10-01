"""Headless Fast tool execution and its public failure diagnostics (issue #917)."""

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain.systems.runs.presentation import public_tool_event_payload
from brain.systems.runs.tools import ToolExecution
from tests.test_agent import _mock_llm_client
from tests.test_agent_run_runtime import _runtime


@pytest.mark.parametrize("text", [
    "Select a file from the list, then continue",
    "token count: 42",
    "token: 42 tokens used",
    "password: required",
    "with x as (a placeholder)",
])
def test_completed_event_preserves_normal_text(text):
    public = public_tool_event_payload({
        "tool_name": "read_file", "args": {"description": text}, "result": text,
    }, "run.tool_completed")

    assert public["result_preview"] == text
    assert public["args"]["description"] == text
    assert public["display"]["sensitive"] is False


def test_sqlalchemy_diagnostic_removes_statement_and_parameters():
    public = public_tool_event_payload({
        "tool_name": "parallel_tool_batch", "error_class": "Error",
        "error_message": "(psycopg.Error) boom\n[SQL: SELECT id FROM ideas WHERE id = %s]\n[parameters: ('abc',)]",
    }, "run.tool_failed")

    assert "boom" in public["error_message"]
    assert all(value not in public["error_message"] for value in ("SELECT", "ideas", "abc"))


async def test_first_headless_fast_batch_uses_native_context(monkeypatch):
    from brain.platform.integrations.providers import LLMResponse, TextContentBlock, ToolUseContentBlock, Usage
    from brain.systems.runs import direct_agent
    from brain.systems.runs.execution_context import current_agent_context
    from brain.systems.runs.recipes.fast import FastRecipe

    target = Path(direct_agent.__file__).with_name("execution_context.py")
    runtime = _runtime("fast", workspace_ref={"mode": "headless_submission", "source": "inbound"})
    runtime.request.metadata.update({"origin": "claude-code.submit", "source": "inbound"})
    runtime.store.runs[42] = runtime.run
    requests = []

    async def api_call(_provider, request, *_args, **_kwargs):
        requests.append(request)
        context = current_agent_context()
        assert context.org_id == "org-1"
        assert context.execution_metadata["profile"] == "fast"
        assert context.execution_metadata["origin"] == "claude-code.submit"
        assert context.workspace_ref == {"mode": "headless_submission", "source": "inbound"}
        assert not getattr(context, "workspace_root", None)
        if len(requests) == 1:
            assert any(tool["name"] == "parallel_tool_batch" for tool in request.tools)
            content = [ToolUseContentBlock(id="batch-1", name="parallel_tool_batch", input={
                "operations": [{"tool_name": "read_file", "args": {"path": str(target), "start_line": 1, "end_line": 1}}],
            })]
            stop_reason = "tool_use"
        else:
            assert len(requests) == 2
            results = [block for message in request.messages for block in message.get("content", [])
                       if isinstance(block, dict) and block.get("type") == "tool_result"]
            batch = json.loads(results[-1]["content"])
            assert batch["completed"] == 1
            assert batch["failed"] == 0
            assert "content" in batch["results"][0]["result"], batch
            assert target.read_text().splitlines()[0] in batch["results"][0]["result"]["content"]
            content = [TextContentBlock("Read complete.")]
            stop_reason = "end_turn"
        return LLMResponse(content=content, stop_reason=stop_reason,
                           usage=Usage(input_tokens=3, output_tokens=2), model="gpt-5.6-sol")

    # Exercise the real recipe, kernel, native context construction, handler,
    # thread boundary and event executor; replace only external I/O.
    monkeypatch.setattr(direct_agent, "_init_llm_async", AsyncMock(return_value=(
        _mock_llm_client(MagicMock(), provider="openai"), MagicMock(), {},
    )))
    monkeypatch.setattr(direct_agent, "_api_call_with_retry_async", api_call)
    monkeypatch.setattr(direct_agent, "_async_record_api_call", AsyncMock())
    monkeypatch.setattr(direct_agent, "_runtime_async_apply_agent_session_side_effects", AsyncMock())
    monkeypatch.setattr(direct_agent, "load_budget_notices_sent", AsyncMock(return_value=set()))
    monkeypatch.setattr(direct_agent, "load_due_budget_notices", AsyncMock(return_value=[]))
    monkeypatch.setattr(direct_agent._session_store, "async_load_session", AsyncMock(return_value=([], None)))
    monkeypatch.setattr(direct_agent._session_store, "async_load_session_handoff", AsyncMock(return_value=None))

    result = await FastRecipe().execute(runtime)

    assert result.status.value == "completed"
    assert len(requests) == 2
    events = [event for event in runtime.store.events if event.event_type.startswith("run.tool_")]
    assert [event.event_type for event in events] == ["run.tool_started", "run.tool_completed"]
    assert all(event.payload["tool_name"] == "parallel_tool_batch" for event in events)


async def _failed_event(message, *, args=None):
    runtime = _runtime("fast")

    def raise_error(**_kwargs):
        raise ValueError(message)

    with pytest.raises(ValueError):
        await runtime.tool_executor().execute(42, ToolExecution(
            name="parallel_tool_batch", args=args or {}, handler=raise_error,
        ))
    return next(event for event in runtime.store.events if event.event_type == "run.tool_failed")


async def test_raised_error_keeps_class_message_and_safe_failure():
    from brain.systems.runs.failures import DEFAULT_FAILED_RUN_MESSAGE

    event = await _failed_event("boom")
    public = public_tool_event_payload(event.payload, event.event_type)
    assert event.visibility.value == "public"
    assert event.payload["error_class"] == "ValueError"
    assert event.payload["error"] == "boom"
    assert event.payload["error_message"] == public["error_message"]
    assert public["error_class"] == "ValueError"
    assert public["error_message"] == "boom"
    assert public["failure"] == {"status": "failed", "category": "internal", "message": DEFAULT_FAILED_RUN_MESSAGE}
    assert public["error"] == DEFAULT_FAILED_RUN_MESSAGE


async def test_failure_diagnostic_is_bounded():
    event = await _failed_event("x" * 50_000)
    public = public_tool_event_payload(event.payload, event.event_type)
    assert len(event.payload["error"]) == 1000
    assert event.payload["error_message"] == public["error_message"]
    assert 0 < len(public["error_message"]) <= 500


async def test_empty_exception_message_still_has_failure_identity():
    event = await _failed_event("")
    public = public_tool_event_payload(event.payload, event.event_type)
    assert public["error_class"] == "ValueError"
    assert public["error_message"] == ""
    assert public["error"] == ""
    assert "failure" not in public
    assert public["display"]["status"] == "failed"


@pytest.mark.parametrize("error", [None, "", "   "])
def test_failed_event_without_summary_keeps_legacy_error(error):
    payload = {"tool_name": "read_file"}
    if error is not None:
        payload["error"] = error
    public = public_tool_event_payload(payload, "run.tool_failed")

    assert "failure" not in public
    assert "error_class" not in public
    assert "error_message" not in public
    if error is None:
        assert "error" not in public
    else:
        assert public["error"] == ""


def test_diagnostic_uses_persisted_public_copy():
    public = public_tool_event_payload({
        "tool_name": "read_file", "error": "current error", "error_message": "safe diagnostic",
    }, "run.tool_failed")
    assert public["error_message"] == "safe diagnostic"


@pytest.mark.parametrize("error_class", [None, "ValueError", "not a class!"])
def test_old_event_never_derives_diagnostic_from_raw_error(error_class):
    payload = {"error": "private-value"}
    if error_class is not None:
        payload["error_class"] = error_class
    public = public_tool_event_payload(payload, "run.tool_failed")
    assert "error_message" not in public
    if error_class is None:
        assert "error_class" not in public
    else:
        assert public["error_class"] == ("ValueError" if error_class == "ValueError" else "ToolError")


@pytest.mark.parametrize("statement", [
    "SELECT 'private-sql-value'",
    "SELECT id, name FROM users WHERE id = 7",
    "[SQL: SELECT id FROM users] [parameters: ('abc',)]",
    "[parameters: ('abc',)]",
    "SELECT * FROM users",
    "SELECT 42",
    "SELECT count(id) FROM users",
    "SELECT users.id AS user_id FROM users",
    "INSERT INTO users",
    "UPDATE users SET id = 7",
    "DELETE FROM users",
    "CREATE TABLE users (id int)",
    "ALTER TABLE users ADD name text",
    "DROP TABLE users",
    "TRUNCATE TABLE users",
])
async def test_sql_shaped_diagnostic_removes_whole_tail(statement):
    event = await _failed_event(f"query failed: {statement}\nprivate-tail")
    public = public_tool_event_payload(event.payload, event.event_type)
    assert public["error_message"] == "query failed: [redacted]"


@pytest.mark.parametrize("message", [
    "Select a file",
    "Select a file from the list, then continue",
    "Please select from the menu",
    "update the settings and retry",
    "with x as (a placeholder)",
])
def test_diagnostic_does_not_treat_bare_keywords_as_sql(message):
    public = public_tool_event_payload({"error_message": message}, "run.tool_failed")
    assert public["error_message"] == message


@pytest.mark.parametrize("message", [
    "Authorization: Bearer private-bearer-value",
    "query failed: SELECT password FROM accounts WHERE token = 'private-sql-value';",
    "database error\n[SQL: UPDATE accounts SET token = 'private-sql-value']\n[parameters: {'token': 'private-sql-value'}]",
    "provider failed token=private-token-value",
    "query failed: SELECT 'private-sql-value';",
    "query failed: INSERT INTO accounts (token) VALUES ('private-sql-value');",
    "query failed: UPDATE accounts SET token = 'private-sql-value';",
    "query failed: DELETE FROM accounts WHERE token = 'private-sql-value';",
])
async def test_failure_diagnostic_redacts_secrets_and_sql(message):
    event = await _failed_event(message)
    public = public_tool_event_payload(event.payload, event.event_type)
    assert message not in json.dumps(public)
    assert "private-" not in public["error_message"]
    assert "SELECT password" not in public["error_message"]
    assert "UPDATE accounts" not in public["error_message"]


async def test_failure_diagnostic_redacts_only_sensitive_argument_values():
    event = await _failed_event("could not read notes/plan.md with nested-private-value", args={
        "operations": [{
            "tool_name": "read_file",
            "args": {"path": "notes/plan.md", "api_key": {"value": "nested-private-value"}},
        }],
    })
    public = public_tool_event_payload(event.payload, event.event_type)
    assert public["error_message"] == "could not read notes/plan.md with [secret redacted]"
    assert event.payload["error_message"] == public["error_message"]


async def test_diagnostic_redacts_arguments_missing_from_safe_args():
    secret = "private-argument-value"
    event = await _failed_event(f"could not read {secret}", args={
        "operations": [{"padding": "x" * 400, "password": secret}],
    })
    assert secret not in json.dumps(event.payload["args"])
    public = public_tool_event_payload(event.payload, event.event_type)
    assert secret not in json.dumps(public)
    assert event.payload["error"] == f"could not read {secret}"


async def test_diagnostic_redacts_argument_crossing_storage_limit():
    secret = "private-argument-value" * 60
    event = await _failed_event(f"could not read {secret}", args={"password": secret})
    public = public_tool_event_payload(event.payload, event.event_type)
    assert "private-" not in public["error_message"]
    assert public["error_message"] == "could not read [secret redacted]"


async def test_quoted_secret_redacted_before_storage_limit():
    event = await _failed_event('secret="first private-value ' + 'x' * 2000 + '"')
    public = public_tool_event_payload(event.payload, event.event_type)
    assert len(event.payload["error"]) == 1000
    assert public["error_message"] == "[redacted]"
    assert "private-value" not in public["error_message"]


@pytest.mark.parametrize("values, message, expected", [
    (["abcdef", "defghijk"], "x abcdefghijk y", "x [secret redacted] y"),
    (["abcdef", "ghijk"], "x abcdefghijk y", "x [secret redacted] y"),
    (["aaaa"], "x aaaaa y", "x [secret redacted] y"),
    (["abc"], "x abc y", "x abc y"),
])
async def test_diagnostic_redacts_complete_argument_regions(values, message, expected):
    event = await _failed_event(message, args={"password": values})
    public = public_tool_event_payload(event.payload, event.event_type)
    assert event.payload["error_message"] == expected
    assert public["error_message"] == expected


@pytest.mark.parametrize("args", [
    {},
    {"operations": []},
    {"operations": [{}]},
    {"operations": [{"tool_name": "brain_recall"}]},
    {"operations": [{"tool_name": "search_knowledge"}]},
    {"operations": [{"tool_name": "read_workspace_overview"}]},
    {"operations": [{"tool_name": "exec_command", "args": {"command": "pwd"}}]},
    {"operations": [{"tool_name": "read_file"}], "max_parallel": "two"},
    {"operations": [{"tool_name": "read_file", "args": []}]},
])
async def test_invalid_batch_is_a_failed_event_with_readable_diagnostic(args):
    from brain.systems.runs.direct_loop.tool_execution import PendingToolCall, async_resolve_tool_call
    from brain.systems.runs.tool_surface import build_tool_handlers
    from brain.systems.runs.tools import wrap_tool_handlers

    runtime = _runtime("fast")
    handlers = wrap_tool_handlers(build_tool_handlers(workspace_root=None), executor=runtime.tool_executor(), run_id=42)
    result = await async_resolve_tool_call(PendingToolCall(
        block_id="invalid-batch", tool_name="parallel_tool_batch", tool_input=args,
        handler=handlers["parallel_tool_batch"],
    ))

    assert json.loads(result.result_text)["error"]
    assert result.outcome.failure is not None
    from brain.systems.runs.direct_loop.final_reply_evidence import ToolResultEvidence

    # Validation is a failure and cannot prove task success.
    evidence = ToolResultEvidence.capture(
        tool_name="parallel_tool_batch", arguments=args,
        is_error=True, result=result.result_value,
    )
    assert evidence.failed
    assert not any(event.event_type == "run.tool_completed" for event in runtime.store.events)
    failed = next(event for event in runtime.store.events if event.event_type == "run.tool_failed")
    public = public_tool_event_payload(failed.payload, failed.event_type)
    assert public["error_class"] == "ToolError"
    assert public["error_message"] == failed.payload["error_message"]
    assert public["error_message"]
    if not args:
        assert public["error_message"] == "operations must be a non-empty list"
    rejected = [op.get("tool_name") for op in args.get("operations", []) if isinstance(op, dict)]
    if rejected and rejected[0] in {"brain_recall", "search_knowledge", "read_workspace_overview", "exec_command"}:
        # The diagnostic must name the rejected tool; that is the point of it.
        assert f"Tool '{rejected[0]}' is not allowed" in public["error_message"]
    assert public["display"]["status"] == "failed"


async def test_run_get_returns_tool_failure_diagnostics():
    from brain.app.api.routers.agent_mcp import _read_run_get

    event = await _failed_event("boom")
    now = datetime.now(timezone.utc)
    runtime = _runtime("fast")
    run = SimpleNamespace(**{**vars(runtime.run), "metadata_": {}, "created_at": now, "updated_at": now})
    stored_event = SimpleNamespace(**{**vars(event), "id": 1, "sequence_no": 1})
    session = SimpleNamespace(
        get=AsyncMock(return_value=run),
        scalars=AsyncMock(side_effect=[
            SimpleNamespace(all=lambda: []), SimpleNamespace(all=lambda: [stored_event]),
        ]),
        scalar=AsyncMock(return_value=None),
    )
    result = await _read_run_get(session, SimpleNamespace(org_id="org-1"), {
        "run_id": 42, "include_tool_events": True, "include_artifacts": False,
    })
    payload = result["tool_events"][0]["payload"]
    assert payload["error_class"] == "ValueError"
    assert payload["error_message"] == "boom"
    assert payload["failure"]["category"] == "internal"
    assert result["tool_call_summary"]["last_write_tool_call_at"] is None

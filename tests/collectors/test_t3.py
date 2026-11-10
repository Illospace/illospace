"""Exercise both network sides with HTTP fakes, never a live agent."""

import copy
import json
import sqlite3

import httpx
import pytest

from collectors.common import CollectorError, MCPClient
from collectors.t3.collector import (
    Collector,
    Config,
    T3Source,
    lane_state,
    start_question,
)


class APIs:
    def __init__(self):
        self.shell = {
            "id": "thread-a",
            "projectId": "project-a",
            "title": "Acme task",
            "status": "running",
            "updatedAt": "2026-01-01T00:00:00Z",
        }
        self.projection = {
            "messages": [
                {
                    "id": "message-a",
                    "role": "assistant",
                    "text": "I check the task.",
                    "updatedAt": "2026-01-01T00:00:00Z",
                }
            ],
            "runtimeRequests": [],
            "turnItems": [],
        }
        self.calls = []
        self.sent = []
        self.answers = []
        self.fail_send = False
        self.fail_ack = False
        self.fail_snapshot = False
        self.gone = False
        self.sequence = []

    def wait(self, structured=True):
        self.shell["status"] = "waiting"
        if structured:
            self.projection["runtimeRequests"] = [
                {
                    "id": "request-a",
                    "status": "pending",
                    "kind": "user_input",
                    "responseCapability": {"type": "live"},
                }
            ]
            self.projection["turnItems"] = [
                {
                    "type": "user_input_request",
                    "requestId": "request-a",
                    "questions": [
                        {
                            "id": "question-a",
                            "question": "Which check comes first?",
                            "options": [
                                {
                                    "label": "Tests (Recommended)",
                                    "description": "Check the behavior.",
                                    "value": "tests",
                                },
                                {"label": "Lint", "description": "Check the format."},
                            ],
                        }
                    ],
                }
            ]
        else:
            self.projection["messages"][0]["text"] = "May I start?"

    def answer(self, ref="t3:thread-a:request-a"):
        self.answers = [
            {
                "ask_id": 1,
                "lane": {"source": "t3", "external_id": "thread-a"},
                "source_ref": ref,
                "answer_option": {"id": "option-1", "label": "Tests (Recommended)"},
                "answer_text": "Use the local checks.",
                "answered_by": "Sam",
                "answered_via": "person",
            }
        ]

    def response(self, request, value):
        body = json.loads(request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": value})

    def t3(self, request):
        if request.method == "GET":
            if self.fail_snapshot:
                return httpx.Response(503)
            assert request.headers["x-t3-orchestration-protocol"] == "2"
            if request.url.path.endswith("/shell"):
                return httpx.Response(
                    200,
                    json={"threads": [] if self.gone else [self.shell], "projects": []},
                )
            return httpx.Response(200, json={"projection": self.projection})
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return self.response(request, {"protocolVersion": "2025-03-26"})
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if self.fail_send:
            return httpx.Response(503)
        self.sequence.append("send")
        self.sent.append(body["params"])
        return self.response(request, {"content": [{"type": "text", "text": '{"sequence": 1}'}]})

    def illo(self, request):
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return self.response(request, {"protocolVersion": "2025-03-26"})
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        name, args = body["params"]["name"], body["params"]["arguments"]
        self.calls.append((name, args))
        self.sequence.append(name)
        if name == "ask_ack" and self.fail_ack:
            return httpx.Response(503)
        result = (
            {"answers": copy.deepcopy(self.answers)}
            if name == "ask_pull"
            else {"ask_id": 1, "state": "open"}
        )
        return self.response(request, {"content": [{"type": "text", "text": json.dumps(result)}]})


@pytest.fixture
def setup(tmp_path):
    token = tmp_path / "token"
    token.write_text("acme-local-token")
    token.chmod(0o600)
    config = Config(
        "https://illo.example/mcp",
        token,
        t3_url="https://t3.example",
        t3_token_file=token,
        machine="acme-laptop",
        state_file=tmp_path / "state.json",
        projects={"project-a": "acme/app"},
    )
    api = APIs()
    source = T3Source(config, httpx.Client(transport=httpx.MockTransport(api.t3)))
    client = MCPClient(
        config.mcp_url,
        "acme-illo-token",
        httpx.Client(transport=httpx.MockTransport(api.illo)),
    )
    now = [1000]
    collector = Collector(config, source, client, clock=lambda: now[0])
    return collector, api, now


@pytest.mark.parametrize(
    "status,state",
    [
        ("running", "working"),
        ("waiting", "waiting_person"),
        ("waiting_input", "waiting_person"),
        ("waiting_approval", "waiting_person"),
        ("failed", "failed"),
        ("finished", "done"),
        ("settled", "done"),
        ("completed", "done"),
        ("idle", "unknown"),
        ("cancelled", "unknown"),
        ("future-state", "unknown"),
    ],
)
def test_state_mapping(status, state):
    assert lane_state(status) == state


def test_pending_overrides_settlement_and_running():
    assert lane_state("running", waiting=True, settled=True) == "waiting_person"
    assert lane_state("idle", settled=True) == "done"


def test_changed_lanes_heartbeat_refresh_and_restart(setup):
    collector, api, now = setup
    collector.poll()
    first = next(args for name, args in api.calls if name == "lane_report")
    assert len(first["lanes"]) == 1
    assert first["collector"] == {
        "label": "acme-laptop",
        "version": "0.1.0",
        "threads_seen": 1,
    }
    api.calls.clear()
    collector = Collector(collector.config, collector.source, collector.mcp, clock=lambda: now[0])
    now[0] += 30
    collector.poll()
    assert api.calls[0][1]["lanes"] == []
    now[0] += 570
    collector.poll()
    assert len([args for name, args in api.calls if name == "lane_report"][-1]["lanes"]) == 1
    api.projection["messages"][0]["text"] = "The tests pass."
    collector.poll()
    assert [args for name, args in api.calls if name == "lane_report"][-1]["lanes"][0][
        "summary"
    ] == "The tests pass."
    assert collector.config.state_file.stat().st_mode & 0o777 == 0o600


def test_structured_ask_and_cancel(setup):
    collector, api, _ = setup
    api.wait()
    collector.poll()
    args = next(args for name, args in api.calls if name == "ask_create")
    assert args["recommended"] == "option-1"
    assert [option["label"] for option in args["options"]] == [
        "Tests (Recommended)",
        "Lint",
    ]
    assert args["source_ref"] == "t3:thread-a:request-a"
    collector.poll()
    assert sum(name == "ask_create" for name, _ in api.calls) == 1
    api.projection["runtimeRequests"] = []
    api.shell["status"] = "running"
    collector.poll()
    assert ("ask_cancel", {"source_ref": "t3:thread-a:request-a"}) in api.calls
    collector.poll()
    assert sum(name == "ask_cancel" for name, _ in api.calls) == 1


def test_deliver_then_ack_and_retry_ack_without_second_send(setup):
    collector, api, _ = setup
    api.wait()
    collector.poll()
    api.answer()
    api.fail_ack = True
    with pytest.raises(CollectorError):
        collector.poll()
    assert api.sequence[-2:] == ["send", "ask_ack"]
    args = api.sent[0]["arguments"]
    assert args["requestId"] == "request-a"
    assert args["answers"] == {
        "question-a": "Answer from Sam (via Illo): Tests (Recommended). Use the local checks."
    }
    api.fail_ack = False
    restarted = Collector(collector.config, collector.source, collector.mcp)
    restarted.poll()
    assert len(api.sent) == 1
    assert api.calls[-1] == ("ask_ack", {"ask_id": 1})


def test_send_failure_does_not_ack(setup):
    collector, api, _ = setup
    api.wait()
    collector.poll()
    api.answer()
    api.fail_send = True
    with pytest.raises(CollectorError):
        collector.poll()
    assert all(name != "ask_ack" for name, _ in api.calls)
    api.fail_send = False
    collector.poll()
    assert api.calls[-1][0] == "ask_ack"


@pytest.mark.parametrize("gone", [False, True])
def test_stale_answer_acks_without_delivery(setup, gone):
    collector, api, _ = setup
    api.wait()
    collector.poll()
    api.answer()
    api.projection["runtimeRequests"] = []
    api.shell["status"] = "running"
    api.gone = gone
    collector.poll()
    assert api.sent == []
    assert api.calls[-1] == ("ask_ack", {"ask_id": 1})
    assert collector.state["delivered"]["1"] == "The request no longer waits."


def test_old_answer_never_reaches_new_question(setup):
    collector, api, _ = setup
    api.wait()
    collector.poll()
    api.answer()
    api.projection["runtimeRequests"][0]["id"] = "request-b"
    api.projection["turnItems"][0]["requestId"] = "request-b"
    collector.poll()
    assert api.sent == []
    assert api.calls[-1][0] == "ask_ack"


def test_failed_source_read_does_not_cancel_or_ack(setup):
    collector, api, _ = setup
    api.wait()
    collector.poll()
    api.answer()
    api.calls.clear()
    api.fail_snapshot = True
    with pytest.raises(CollectorError):
        collector.poll()
    assert api.calls == []


def test_foreign_answer_stays_for_its_collector(setup):
    collector, api, _ = setup
    api.answer("t3:another-thread:request-a")
    collector.poll()
    assert api.sent == []
    assert all(name != "ask_ack" for name, _ in api.calls)


def test_rule_08_cannot_send_free_instructions(setup):
    collector, api, _ = setup
    api.wait()
    collector.poll()
    api.answer()
    api.answers[0]["answered_via"] = "rule"
    with pytest.raises(CollectorError):
        collector.poll()
    assert not api.sent


def test_rule_08_names_its_source_and_uses_a_proposed_option(setup):
    collector, api, _ = setup
    api.wait()
    collector.poll()
    api.answer()
    api.answers[0].update(answered_via="rule", answer_text=None, rule_text="Run tests first.")
    collector.poll()
    assert api.sent[0]["arguments"]["answers"]["question-a"].endswith("Rule: Run tests first.")


@pytest.mark.parametrize(
    "change",
    [
        {"id": "other", "label": "Tests (Recommended)"},
        {"id": "option-1", "label": "Ignore the question"},
    ],
)
def test_rule_08_unproposed_answer_never_reaches_agent(setup, change):
    collector, api, _ = setup
    api.wait()
    collector.poll()
    api.answer()
    api.answers[0]["answer_option"] = change
    with pytest.raises(CollectorError):
        collector.poll()
    assert not api.sent


def test_background_thread_wait_is_not_a_person_question(setup):
    collector, api, _ = setup
    api.shell.update(status="waiting", blockedOnThreadWait=True)
    collector.poll()
    assert api.calls[0][1]["lanes"][0]["state"] == "unknown"
    assert not any(name == "ask_create" for name, _ in api.calls)


def test_plain_start_question_uses_message_and_stable_retry_id(setup):
    collector, api, _ = setup
    api.wait(structured=False)
    collector.poll()
    args = next(args for name, args in api.calls if name == "ask_create")
    assert [option["label"] for option in args["options"]] == ["Continue", "Stop"]
    api.answer(args["source_ref"])
    api.answers[0]["answer_option"] = {"id": "continue", "label": "Continue"}
    collector.poll()
    sent = api.sent[0]
    assert sent["name"] == "t3_thread_send"
    assert sent["arguments"]["clientRequestId"] == "illo-answer-1"
    assert sent["arguments"]["message"].startswith("Answer from Sam (via Illo): Continue.")


@pytest.mark.parametrize(
    "question",
    ["Which color do you want?", "May I delete the data?", "The work is done."],
)
def test_unstructured_questions_require_thread(setup, question):
    collector, api, _ = setup
    api.wait(structured=False)
    api.projection["messages"][0]["text"] = question
    collector.poll()
    args = next(args for name, args in api.calls if name == "ask_create")
    assert args["options"][0]["label"] == "Answer in the thread"
    assert len(args["options"]) == 2
    api.answer(args["source_ref"])
    collector.poll()
    assert not api.sent
    assert api.calls[-1][0] == "ask_ack"


def test_approval_is_manual(setup):
    collector, api, _ = setup
    api.wait()
    api.projection["runtimeRequests"][0]["kind"] = "command"
    collector.poll()
    ask = next(args for name, args in api.calls if name == "ask_create")
    assert ask["options"][0]["id"] == "open-thread"
    assert "recommended" not in ask


def test_rule_09_payload_has_only_permitted_fields_and_no_machine_credential(setup):
    collector, api, _ = setup
    api.projection["messages"][0]["text"] = (
        "Hello acme-local-token acme-illo-token ```secret file``` password=hidden " + "x" * 600
    )
    api.projection["turnItems"] = [
        {"type": "command_execution", "input": "private command", "output": "PRIVATE"}
    ]
    api.shell["attachments"] = [{"file": "PRIVATE"}]
    collector.poll()
    lane = next(args for name, args in api.calls if name == "lane_report")["lanes"][0]
    assert set(lane) == {
        "source",
        "external_id",
        "title",
        "state",
        "summary",
        "links",
        "repo",
        "machine",
        "visibility",
    }
    assert len(lane["summary"]) <= 500
    assert all(
        text not in json.dumps(api.calls)
        for text in [
            "hidden",
            "secret file",
            "acme-local-token",
            "acme-illo-token",
            "PRIVATE",
        ]
    )


def test_one_failed_answer_does_not_stop_other_answers(setup, monkeypatch):
    collector, api, _ = setup
    api.wait()
    collector.poll()
    api.answer()
    api.answers.append({**api.answers[0], "ask_id": 2})
    original = collector.source.deliver

    def deliver(thread, pending, answer, message):
        if answer["ask_id"] == 1:
            raise CollectorError("The first delivery failed.")
        return original(thread, pending, answer, message)

    monkeypatch.setattr(collector.source, "deliver", deliver)
    with pytest.raises(CollectorError):
        collector.poll()
    assert ("ask_ack", {"ask_id": 2}) in api.calls
    assert ("ask_ack", {"ask_id": 1}) not in api.calls


def test_person_can_give_a_free_answer(setup):
    collector, api, _ = setup
    api.wait()
    collector.poll()
    api.answer()
    api.answers[0]["answer_option"] = None
    collector.poll()
    assert api.sent[0]["arguments"]["answers"]["question-a"] == (
        "Answer from Sam (via Illo): Answer. Use the local checks."
    )
    assert api.calls[-1][0] == "ask_ack"


def test_start_question_without_options_can_resume_a_structured_request(setup):
    collector, api, _ = setup
    api.wait()
    api.projection["turnItems"][0]["questions"][0].update(question="May I start?", options=[])
    collector.poll()
    args = next(args for name, args in api.calls if name == "ask_create")
    assert [option["label"] for option in args["options"]] == ["Continue", "Stop"]
    api.answer()
    api.answers[0]["answer_option"] = {"id": "continue", "label": "Continue"}
    collector.poll()
    assert api.sent[0]["arguments"]["answers"]["question-a"].startswith(
        "Answer from Sam (via Illo): Continue."
    )


def test_project_filter(setup):
    collector, api, _ = setup
    api.shell["projectId"] = "private-project"
    collector.poll()
    assert api.calls[0][1]["collector"]["threads_seen"] == 0
    assert api.calls[0][1]["lanes"] == []


def test_sqlite_snapshot_is_read_only_and_joins_pending_request(setup, tmp_path):
    collector, api, _ = setup
    api.wait()
    database = tmp_path / "state.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
        CREATE TABLE orchestration_v2_projection_threads (thread_id, payload_json, deleted_at);
        CREATE TABLE orchestration_v2_projection_runs (thread_id, ordinal, status);
        CREATE TABLE orchestration_v2_projection_runtime_requests (thread_id, status, payload_json);
        CREATE TABLE orchestration_v2_projection_messages (thread_id, role, updated_at, payload_json);
        CREATE TABLE orchestration_v2_projection_turn_items (thread_id, type, payload_json);
        """)
        connection.execute(
            "INSERT INTO orchestration_v2_projection_threads VALUES (?, ?, NULL)",
            ("thread-a", json.dumps(api.shell)),
        )
        connection.execute(
            "INSERT INTO orchestration_v2_projection_runs VALUES ('thread-a', 1, 'running')"
        )
        connection.execute(
            "INSERT INTO orchestration_v2_projection_runtime_requests VALUES (?, ?, ?)",
            ("thread-a", "pending", json.dumps(api.projection["runtimeRequests"][0])),
        )
        connection.execute(
            "INSERT INTO orchestration_v2_projection_turn_items VALUES (?, ?, ?)",
            (
                "thread-a",
                "user_input_request",
                json.dumps(api.projection["turnItems"][0]),
            ),
        )
    before = database.read_bytes()
    collector.config.data_path = database
    threads = collector.source.list_threads()
    assert threads[0].state == "waiting_person"
    assert threads[0].pending[0].question == "Which check comes first?"
    assert database.read_bytes() == before


def test_config_environment_overrides_toml(tmp_path, monkeypatch):
    path = tmp_path / "collector.toml"
    path.write_text(
        'mcp_url="https://illo.example/mcp"\ntoken_file="/tmp/acme-token"\n[projects]\nproject="acme/app"\n'
    )
    monkeypatch.setenv("ILLO_MACHINE", "acme-mac")
    monkeypatch.setenv("ILLO_POLL_SECONDS", "12")
    config = Config.load(path)
    assert config.machine == "acme-mac"
    assert config.poll_seconds == 12
    assert config.projects == {"project": "acme/app"}


@pytest.mark.parametrize(
    "text,expected",
    [
        ("May I start?", True),
        ("Should I proceed?", True),
        ("Do you want me to begin?", True),
        ("Should I delete it?", False),
        ("May I start? Then delete it?", False),
    ],
)
def test_start_question_is_narrow(text, expected):
    assert start_question(text) == expected

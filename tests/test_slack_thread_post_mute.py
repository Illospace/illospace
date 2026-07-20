"""Regression tests for Slack thread-scoped post mutes."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest


THREAD_TS = "1784484952.946099"
MUTE_TS = "1784485035.715559"


def _message(user: str, text: str, ts: str, **extra: Any) -> dict[str, Any]:
    return {"user": user, "text": text, "ts": ts, **extra}


def _trigger(thread_ts: str = THREAD_TS) -> dict[str, Any]:
    return {
        "channel_id": "C082SUKQKJL",
        "channel_type": "channel",
        "message_ts": "1784492958.128739",
        "thread_ts": thread_ts,
        "bot_user_id": "BILLO",
        "response_target": {
            "channel_id": "C082SUKQKJL",
            "thread_ts": thread_ts,
            "visibility": "public",
        },
    }


class _SlackClient:
    def __init__(self, threads: dict[str, list[dict[str, Any]]]):
        self.threads = threads
        self.posts: list[dict[str, Any]] = []
        self.reads: list[dict[str, Any]] = []

    async def conversation_replies(self, **kwargs: Any) -> dict[str, Any]:
        self.reads.append(kwargs)
        return {
            "ok": True,
            "messages": list(self.threads.get(kwargs["thread_ts"], [])),
            "response_metadata": {"next_cursor": ""},
        }

    async def post_message(self, **kwargs: Any) -> dict[str, Any]:
        self.posts.append(kwargs)
        return {"ok": True, "channel": kwargs["channel"], "ts": "1784493000.000001"}


async def _post(monkeypatch, client: _SlackClient, *, thread_ts: str = THREAD_TS) -> dict[str, Any]:
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.slack import _handle_post_slack_reply

    async def slack_client() -> _SlackClient:
        return client

    monkeypatch.setattr(
        "brain.systems.runs.tool_catalog.handlers.slack._slack_client_from_runtime",
        slack_client,
    )
    with bind_agent_context({"run_id": 392, "slack_trigger": _trigger(thread_ts)}):
        return json.loads(await _handle_post_slack_reply(body="I am re-entering the incident."))


def test_incident_stand_down_message_parses_as_thread_mute():
    from brain.systems.slack.thread_mute import find_thread_post_mute

    mute = find_thread_post_mute(
        [
            _message(
                "human",
                "@Illo not for you Illo, this is for the team",
                MUTE_TS,
            )
        ],
        illo_user_id="BILLO",
    )

    assert mute is not None
    assert mute.user == "human"
    assert mute.ts == MUTE_TS
    assert mute.ledger_line == f"thread muted by human at {MUTE_TS}"


@pytest.mark.asyncio
async def test_thread_stand_down_suppresses_post_and_records_ledger_line(monkeypatch):
    client = _SlackClient(
        {
            THREAD_TS: [
                _message("U_REDA", "Production incident", THREAD_TS),
                _message(
                    "human",
                    "@Illo not for you Illo, this is for the team",
                    MUTE_TS,
                ),
                _message("U_TEAM", "Still investigating", "1784492900.000001"),
            ]
        }
    )

    result = await _post(monkeypatch, client)

    assert result["ok"] is True
    assert result["suppressed"] is True
    assert result["ledger_line"] == f"thread muted by human at {MUTE_TS}"
    assert client.posts == []
    assert [read["thread_ts"] for read in client.reads] == [THREAD_TS]


@pytest.mark.asyncio
async def test_thread_mute_does_not_affect_another_thread(monkeypatch):
    other_thread_ts = "1784500000.000001"
    client = _SlackClient(
        {
            THREAD_TS: [
                _message("human", "@Illo leave this to us", MUTE_TS),
            ],
            other_thread_ts: [
                _message("U_OTHER", "New unrelated incident", other_thread_ts),
            ],
        }
    )

    result = await _post(monkeypatch, client, thread_ts=other_thread_ts)

    assert result["ok"] is True
    assert result.get("suppressed") is not True
    assert client.posts == [
        {
            "channel": "C082SUKQKJL",
            "text": "I am re-entering the incident.",
            "thread_ts": other_thread_ts,
        }
    ]
    assert [read["thread_ts"] for read in client.reads] == [other_thread_ts]


@pytest.mark.asyncio
async def test_later_explicit_reinvite_lifts_thread_mute(monkeypatch):
    client = _SlackClient(
        {
            THREAD_TS: [
                _message(
                    "human",
                    "<@BILLO> can you check the latest deploy now?",
                    "1784492900.000001",
                ),
                _message("human", "@Illo leave this to us", MUTE_TS),
            ]
        }
    )

    result = await _post(monkeypatch, client)

    assert result["ok"] is True
    assert result.get("suppressed") is not True
    assert len(client.posts) == 1


@pytest.mark.parametrize(
    ("text", "extra"),
    [
        ("@Illo the team is still checking the database metrics.", {}),
        ("Leave this to us while we check the database metrics.", {}),
        ("@Illo leave this to us", {"bot_id": "B_OTHER"}),
    ],
)
def test_thread_mute_requires_human_address_and_dismissal(text, extra):
    from brain.systems.slack.thread_mute import find_thread_post_mute

    mute = find_thread_post_mute(
        [_message("U_OTHER", text, MUTE_TS, **extra)],
        illo_user_id="BILLO",
    )

    assert mute is None


@pytest.mark.asyncio
async def test_ordinary_human_reply_does_not_mute_thread(monkeypatch):
    client = _SlackClient(
        {
            THREAD_TS: [
                _message("U_REDA", "Production incident", THREAD_TS),
                _message(
                    "human",
                    "The team is still checking the database metrics.",
                    MUTE_TS,
                ),
            ]
        }
    )

    result = await _post(monkeypatch, client)

    assert result["ok"] is True
    assert result.get("suppressed") is not True
    assert len(client.posts) == 1


def test_french_stand_down_directed_at_illo_mutes_thread():
    from brain.systems.slack.thread_mute import find_thread_post_mute

    mute = find_thread_post_mute(
        [_message("human", "@Illo merci, on gère.", MUTE_TS)],
        illo_user_id="BILLO",
    )

    assert mute is not None
    assert mute.ledger_line == f"thread muted by human at {MUTE_TS}"


@pytest.mark.asyncio
async def test_terminal_slack_settlement_also_suppresses_muted_thread(monkeypatch):
    from brain.systems.runs.cortex import runner

    client = _SlackClient(
        {THREAD_TS: [_message("human", "@Illo we've got this", MUTE_TS)]}
    )
    recorded: list[dict[str, Any]] = []
    run = SimpleNamespace(
        id=392,
        parent_run_id=None,
        root_run_id=392,
        thread_id=f"slack:T789:C082SUKQKJL:{THREAD_TS}",
        status="completed",
        org_id="org-1",
        user_id="user-1",
        target_ref={"kind": "slack_message", "slack_trigger": _trigger()},
        metadata_={"final_answer_target_surface": "slack"},
    )

    async def latest_final_answer(_session, *, run):
        return "This settlement must stay silent.", 101

    async def visible_action(_session, *, run):
        return False

    async def slack_client(run_arg):
        assert run_arg is run
        return client

    async def record_mute(_session, **kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(runner, "_latest_final_answer_artifact", latest_final_answer)
    monkeypatch.setattr(runner, "_slack_visible_action_already_recorded", visible_action)
    monkeypatch.setattr(runner, "_slack_client_for_run", slack_client)
    monkeypatch.setattr(runner, "_record_slack_thread_mute", record_mute)

    result = await runner._settle_slack_origin_run_async(object(), run)

    assert result["suppressed"] is True
    assert result["ledger_line"] == f"thread muted by human at {MUTE_TS}"
    assert client.posts == []
    assert recorded[0]["mute"].ledger_line == result["ledger_line"]


@pytest.mark.asyncio
async def test_muted_thread_remains_readable_for_tracker_state(monkeypatch):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.slack import _handle_read_slack_conversation

    messages = [
        _message("U_REDA", "Production incident", THREAD_TS),
        _message("human", "@Illo leave this to us", MUTE_TS),
        _message("U_TEAM", "Tracker: mitigation deployed", "1784492900.000001"),
    ]
    client = _SlackClient({THREAD_TS: messages})

    async def slack_client() -> _SlackClient:
        return client

    monkeypatch.setattr(
        "brain.systems.runs.tool_catalog.handlers.slack._slack_client_from_runtime",
        slack_client,
    )
    with bind_agent_context({"run_id": 392, "slack_trigger": _trigger()}):
        result = json.loads(await _handle_read_slack_conversation())

    assert result["ok"] is True
    assert result["messages"] == messages
    assert result["count"] == 3

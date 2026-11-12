"""The run carries effort and delivers a clear fallback from its own audit."""

import pytest

from brain.models import RunRequest as ServiceRequest
from brain.runtimes import openai_responses
from illo.brain.final_text import broken, guard
from illo.db import one
from illo.ledger import audit


@pytest.mark.parametrize("text", ["", "?!", "<@U123>", "Yes—<#| >}", "Yes <|bad>", "Yes < >"])
def test_broken_final_text(text):
    assert broken(text)


@pytest.mark.parametrize("text", ["The task is ready.", "<https://example.com|Proof> The checks pass.", "Yes"])
def test_clear_final_text(text):
    assert not broken(text)


async def test_guard_uses_only_run_audit(conn, run):
    await audit.write(conn, run_id=run["id"], principal="person:ada", tool="task_hand",
                      args={"id": 5, "text": "PRIVATE TEXT"}, decision="allow", reason="A change", target="task 5")
    await audit.write(conn, run_id=run["id"], principal="person:ada", tool="email_draft",
                      args={"body": "PRIVATE TEXT"}, decision="draft", reason="A draft", target="draft 8")
    text = await guard(conn, run["id"], "Yes—<#| >}")
    assert "could not write a clear answer" in text
    assert "task hand: task 5" in text.lower()
    assert "email draft: draft 8" in text
    assert "PRIVATE" not in text
    row = await one(conn, "SELECT detail FROM sensors WHERE key='brain:final-text'")
    assert str(run["id"]) in row["detail"] and "PRIVATE" not in row["detail"]


async def test_effort_reaches_person_dm_payload(app, replay, fake_brain, monkeypatch):
    ctx = app.state.ctx
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.config.brain.effort["person_dm"] = "high"
    await replay(text="Read my open tasks.")
    request = fake_brain.requests[-1]
    assert request.effort == "high"
    request = ServiceRequest.model_validate(request.wire())
    seen = []
    class Response:
        is_error = False
        def raise_for_status(self):
            pass
        async def aiter_lines(self):
            return
            yield
    class Stream:
        async def __aenter__(self):
            return Response()
        async def __aexit__(self, *args):
            return False
    class Http:
        def stream(self, method, url, **kwargs):
            seen.append(kwargs["json"])
            return Stream()
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    monkeypatch.delenv("ILLO_BRAIN_EFFORT", raising=False)
    openai_responses._without_effort.clear()
    async for _ in openai_responses.response_chunks(request, [], [], Http()):
        pass
    assert seen[-1]["reasoning"]["effort"] == "high"
    monkeypatch.setenv("ILLO_BRAIN_EFFORT", "low")
    async for _ in openai_responses.response_chunks(request, [], [], Http()):
        pass
    assert seen[-1]["reasoning"]["effort"] == "low"


async def test_final_answer_guard_runs_before_delivery(app, replay, fake_brain):
    fake_brain.final_text = "Yes—<#| >}"
    answer = await replay(text="Read my tasks.")
    assert answer["text"] == "Illo could not write a clear answer."


def test_guard_keeps_an_answer_that_is_one_labelled_link():
    from illo.brain.final_text import broken

    assert not broken("<https://example.com/tasks/12|Task 12>")
    assert broken("Yes—<#| >}")
    assert broken("<https://example.com/x>")

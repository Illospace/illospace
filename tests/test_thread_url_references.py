from __future__ import annotations

from brain.systems.cortex.thread_links import (
    canonicalize_thread_reference,
    extract_thread_reference_values,
    thread_id_from_reference,
    thread_link_payload,
    thread_route_for_id,
    thread_url_for_id,
)
from brain.systems.cortex.thread_read_model import (
    compact_handoff_context,
    preview_summary_from_handoff,
    unavailable_thread_reference,
)
from brain.systems.cortex.object_references import store_object_references_for_source
from brain.systems.launch_handoffs import (
    claude_prompt_for_handoff,
    codex_deep_link_for_handoff,
    extract_launch_handoff_reference_values,
    handoff_id_from_reference,
    launch_handoff_reference_payload,
    launch_handoff_route_for_id,
    launch_handoff_url_for_id,
)


THREAD_ID = "77777777-7777-4777-8777-777777777777"
HANDOFF_ID = "88888888-8888-4888-8888-888888888888"


def test_thread_link_payload_uses_canonical_thread_route(monkeypatch):
    monkeypatch.setenv("ILLO_PUBLIC_URL", "https://illo.example.com/app")

    assert thread_route_for_id(THREAD_ID) == f"/threads/{THREAD_ID}"
    assert thread_url_for_id(THREAD_ID) == f"https://illo.example.com/threads/{THREAD_ID}"
    assert thread_link_payload(THREAD_ID) == {
        "thread_id": THREAD_ID,
        "thread_route": f"/threads/{THREAD_ID}",
        "thread_url": f"https://illo.example.com/threads/{THREAD_ID}",
        "url": f"https://illo.example.com/threads/{THREAD_ID}",
    }


def test_thread_reference_parser_accepts_canonical_and_legacy_urls():
    assert thread_id_from_reference(f"https://illo.example.com/threads/{THREAD_ID}") == THREAD_ID
    assert thread_id_from_reference(f"/threads/{THREAD_ID}") == THREAD_ID
    assert thread_id_from_reference(f"https://illo.example.com/cortex?idea={THREAD_ID}") == THREAD_ID
    assert thread_id_from_reference(f"/cortex?idea={THREAD_ID}.") == THREAD_ID
    assert thread_id_from_reference(THREAD_ID) is None
    assert thread_id_from_reference(THREAD_ID, allow_raw_id=True) == THREAD_ID


def test_canonicalize_thread_reference_preserves_original_ref(monkeypatch):
    monkeypatch.setenv("ILLO_PUBLIC_URL", "https://illo.example.com")
    payload = canonicalize_thread_reference(f"/cortex?idea={THREAD_ID}")

    assert payload is not None
    assert payload["original_ref"] == f"/cortex?idea={THREAD_ID}"
    assert payload["thread_route"] == f"/threads/{THREAD_ID}"
    assert payload["thread_url"] == f"https://illo.example.com/threads/{THREAD_ID}"


def test_extract_thread_reference_values_finds_embedded_urls():
    text = (
        f"Compare https://illo.example.com/threads/{THREAD_ID} "
        f"with /cortex?idea={THREAD_ID}."
    )

    assert extract_thread_reference_values(text) == [
        f"https://illo.example.com/threads/{THREAD_ID}",
        f"/cortex?idea={THREAD_ID}",
    ]


def test_launch_handoff_links_use_codex_route_and_origin_url(monkeypatch):
    monkeypatch.setenv("ILLO_PUBLIC_URL", "https://illo.example.com/app")

    launch_route = f"/api/launch-handoffs/{HANDOFF_ID}/launch?target=codex"
    assert launch_handoff_route_for_id(HANDOFF_ID) == launch_route
    assert launch_handoff_url_for_id(HANDOFF_ID) == f"https://illo.example.com{launch_route}"
    assert handoff_id_from_reference(f"https://illo.example.com{launch_route}") == HANDOFF_ID
    assert handoff_id_from_reference(HANDOFF_ID) is None
    assert handoff_id_from_reference(HANDOFF_ID, allow_raw_id=True) == HANDOFF_ID
    assert extract_launch_handoff_reference_values(
        f"Open https://illo.example.com{launch_route}."
    ) == [f"https://illo.example.com{launch_route}"]


def test_launch_handoff_preview_and_codex_prompt_are_compact(monkeypatch):
    monkeypatch.setenv("ILLO_PUBLIC_URL", "https://illo.example.com")

    class Row:
        id = HANDOFF_ID
        org_id = "org-1"
        created_by_user_id = None
        source_surface = "slack"
        source_ref = {"channel_id": "C1"}
        target_tool = "codex"
        title = "Wire launch handoffs"
        summary = "Create a generic handoff link for Codex."
        instructions = "Fetch the context from Illo and implement the handoff."
        acceptance_criteria = []
        context_parts = []
        repo_origin_url = "git@github.com:uwear-ai/illospace-project.git"
        branch_hint = None
        status = "open"
        launch_count = 0
        last_launched_by_user_id = None
        last_launched_at = None
        expires_at = None
        idempotency_key = None
        metadata_ = {}
        created_at = None
        updated_at = None

    payload = launch_handoff_reference_payload(
        Row(),
        original_ref=f"/api/launch-handoffs/{HANDOFF_ID}/launch?target=codex",
    )
    deep_link = codex_deep_link_for_handoff(Row())

    assert payload["object_type"] == "launch_handoff"
    assert payload["launch_url"] == f"https://illo.example.com/api/launch-handoffs/{HANDOFF_ID}/launch?target=codex"
    assert payload["preview_summary"] == "Create a generic handoff link for Codex."
    assert deep_link == (
        "codex://threads/new?prompt=Pick+up+Illo+launch+handoff+"
        "88888888-8888-4888-8888-888888888888%3A+Wire+launch+handoffs%0A%0A"
        "Use+the+Illo+MCP+%60illo_read%60+tool+with+capability+%60handoff.get%60+"
        "and+arguments+%7B%22handoff_id%22%3A%2288888888-8888-4888-8888-"
        "888888888888%22%7D+to+fetch+the+full+context%2C+source+references%2C+"
        "instructions%2C+and+acceptance+criteria+before+changing+code.&originUrl="
        "git%40github.com%3Auwear-ai%2Fillospace-project.git"
    )
    claude_prompt = claude_prompt_for_handoff(Row())
    assert "Claude Code session" in claude_prompt
    assert "`illo_read`" in claude_prompt
    assert "`handoff.get`" in claude_prompt
    assert f'{{"handoff_id":"{HANDOFF_ID}"}}' in claude_prompt


def test_preview_summary_from_handoff_compacts_runtime_checkpoint():
    handoff = {
        "found": True,
        "run_id": 123,
        "updated_at": "2026-06-01T12:00:00+00:00",
        "message_count": 42,
        "summary": {
            "checkpoint": {
                "active_objective": "Ship canonical Thread URLs.",
                "recent_user_intent": "Make Thread references native everywhere.",
                "current_state": "Backend read model is wired; frontend previews are next.",
                "verification_status": "Focused checks still need to run.",
                "current_plan": ["Wire cards across chat surfaces."],
                "completed_work": ["Added canonical URL helpers."],
                "open_questions": [],
            }
        },
    }

    assert preview_summary_from_handoff(handoff) == (
        "Ship canonical Thread URLs. Make Thread references native everywhere. "
        "Backend read model is wired; frontend previews are next. Focused checks still need to run."
    )
    assert compact_handoff_context(handoff) == {
        "run_id": 123,
        "updated_at": "2026-06-01T12:00:00+00:00",
        "message_count": 42,
        "checkpoint": {
            "active_objective": "Ship canonical Thread URLs.",
            "recent_user_intent": "Make Thread references native everywhere.",
            "current_state": "Backend read model is wired; frontend previews are next.",
            "verification_status": "Focused checks still need to run.",
            "current_plan": ["Wire cards across chat surfaces."],
            "completed_work": ["Added canonical URL helpers."],
        },
    }


def test_unavailable_thread_reference_does_not_leak_title_or_summary():
    payload = unavailable_thread_reference(
        original_ref=f"https://illo.example.com/threads/{THREAD_ID}",
        thread_id=THREAD_ID,
    )

    assert payload["type"] == "thread_reference"
    assert payload["object_type"] == "thread"
    assert payload["object_id"] == THREAD_ID
    assert payload["thread_id"] == THREAD_ID
    assert payload["status"] == "unavailable"
    assert payload["title"] is None
    assert payload["preview_summary"] is None


async def test_storing_text_without_thread_links_does_not_touch_session():
    class SessionWithoutPersistence:
        pass

    references = await store_object_references_for_source(
        SessionWithoutPersistence(),
        source_type="chat_message",
        source_id="123",
        org_id="77777777-7777-4777-8777-777777777777",
        text="Plain message with no thread links.",
    )

    assert references == []

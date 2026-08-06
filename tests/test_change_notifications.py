"""Tests for brain/systems/change_notifications.py — pure notify decision + digest."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))

from brain.systems.change_notifications import (
    build_digest,
    classify_event,
    decide_notifications,
    format_line,
    is_urgent,
    render_outbound,
)


class TestClassify:
    def test_urgent_by_label_is_immediate(self):
        assert classify_event({"title": "x", "labels": ["p0", "bug"]}) == "immediate"
        assert classify_event({"title": "prod down!", "labels": []}) == "immediate"

    def test_security_is_urgent(self):
        assert is_urgent({"title": "security hole in auth"}) is True

    def test_noise_event_type_is_skipped(self):
        assert classify_event({"event_type": "schema.updated", "title": "x"}) == "skip"

    def test_explicit_not_noteworthy_is_skipped(self):
        assert classify_event({"title": "x", "noteworthy": False}) == "skip"

    def test_ordinary_change_is_digest(self):
        assert classify_event({"event_type": "record.updated", "title": "Ticket", "action": "closed"}) == "digest"

    def test_custom_urgent_terms(self):
        assert classify_event({"title": "escalated"}, urgent_terms=("escalated",)) == "immediate"


class TestDecide:
    def test_buckets_events(self):
        events = [
            {"title": "A", "labels": ["p0"]},                 # immediate
            {"title": "B", "action": "closed"},               # digest
            {"event_type": "record.read", "title": "C"},      # skip
        ]
        out = decide_notifications(events)
        assert [e["title"] for e in out["immediate"]] == ["A"]
        assert [e["title"] for e in out["digest"]] == ["B"]
        assert [e["title"] for e in out["skip"]] == ["C"]

    def test_empty(self):
        assert decide_notifications([]) == {"immediate": [], "digest": [], "skip": []}


class TestFormat:
    def test_line_owned(self):
        line = format_line({"title": "Ticket", "action": "closed", "owner_id": "u1", "owner_label": "Reda"})
        assert "Ticket" in line and "closed" in line and "Reda" in line

    def test_line_unclaimed(self):
        assert "unclaimed" in format_line({"title": "T", "action": "opened"})

    def test_line_includes_url(self):
        assert "http://x" in format_line({"title": "T", "url": "http://x"})


class TestBuildDigest:
    def test_none_when_empty(self):
        assert build_digest([], unclaimed_count=0) is None

    def test_unclaimed_only_still_posts(self):
        text = build_digest([], unclaimed_count=3)
        assert text is not None and "3 items waiting" in text

    def test_singular_unclaimed(self):
        assert "1 item waiting" in build_digest([], unclaimed_count=1)

    def test_digest_lists_changes(self):
        text = build_digest([{"title": "Ticket", "action": "closed", "owner_id": "u", "owner_label": "R"}])
        assert "Ticket" in text and "What changed" in text


class TestRenderOutbound:
    def test_splits_immediate_and_digest(self):
        events = [
            {"title": "Prod down", "labels": ["p0"]},          # immediate
            {"title": "Ticket closed", "action": "closed"},    # digest
            {"event_type": "record.read", "title": "noise"},   # skip
        ]
        out = render_outbound(events, unclaimed_count=2)
        assert len(out["immediate"]) == 1 and "Prod down" in out["immediate"][0]
        assert out["digest"] is not None
        assert "Ticket closed" in out["digest"]
        assert "2 items waiting" in out["digest"]

    def test_quiet_interval_is_noop(self):
        out = render_outbound([{"event_type": "record.read", "title": "x"}], unclaimed_count=0)
        assert out["immediate"] == []
        assert out["digest"] is None


class TestNotifyCycleOrchestration:
    async def test_posts_immediate_and_digest_to_channel(self, monkeypatch):
        import brain.systems.change_notifications_cycle as cyc

        async def fake_load(session, org_id, since, **kw):
            return [
                {"title": "Prod down", "labels": ["p0"]},        # immediate
                {"title": "Ticket", "action": "closed"},          # digest
            ]

        async def fake_count(session, org_id):
            return 1

        monkeypatch.setattr(cyc, "_load_change_events", fake_load)
        monkeypatch.setattr(cyc, "_count_unclaimed", fake_count)

        sent = []

        async def fake_post(channel, text):
            sent.append((channel, text))

        result = await cyc.run_notify_cycle(
            None, org_id="o", channel_id="C123", since=None, post=fake_post
        )
        assert result["immediate"] == 1
        assert result["digest_posted"] is True
        assert result["unclaimed"] == 1
        assert len(sent) == 2  # one urgent + one digest
        assert all(channel == "C123" for channel, _ in sent)
        assert any("Prod down" in text for _, text in sent)
        assert any("1 item waiting" in text for _, text in sent)

    async def test_resolution_harvest_is_inert_without_session(self, monkeypatch):
        import brain.systems.change_notifications_cycle as cyc

        monkeypatch.setattr(cyc, "_load_change_events", lambda *a, **k: _async_value([]))
        monkeypatch.setattr(cyc, "_count_unclaimed", lambda *a, **k: _async_value(0))

        result = await cyc.run_notify_cycle(
            None, org_id="o", channel_id="C123", post=lambda *a: _async_value(None),
        )
        assert result == {
            "events": 0,
            "immediate": 0,
            "digest_posted": False,
            "unclaimed": 0,
        }

    async def test_resolution_harvest_is_additive_and_safe(self, monkeypatch):
        import brain.systems.change_notifications_cycle as cyc

        monkeypatch.setattr(cyc, "_load_change_events", lambda *a, **k: _async_value([]))
        monkeypatch.setattr(cyc, "_count_unclaimed", lambda *a, **k: _async_value(0))
        monkeypatch.setattr(
            cyc,
            "_maybe_run_alert_resolution_harvest",
            lambda *a, **k: _async_value({"verified": 2}),
        )
        monkeypatch.setattr(
            cyc,
            "_deliver_pending_obligation_notices_safely",
            lambda *a, **k: _async_value(None),
        )

        result = await cyc.run_notify_cycle(
            object(), org_id="o", channel_id="C123", post=lambda *a: _async_value(None),
        )
        assert result["resolution_harvest"] == {"verified": 2}



async def _async_value(value):
    return value


class TestNormalizeEvent:
    def test_reads_user_fields_from_data_not_top_level(self):
        from brain.systems.change_notifications import is_urgent
        from brain.systems.change_notifications_cycle import _normalize_event

        class Row:
            event_type = "record.updated"
            record_id = 5
            # serialize_record shape: title/object_key top-level, fields under data
            after = {
                "object_key": "ticket",
                "title": "Login broken",
                "data": {"url": "http://x", "labels": ["p0"], "priority": "P0", "owner_id": "u1"},
            }

        ev = _normalize_event(Row())
        assert ev["title"] == "Login broken"
        assert ev["url"] == "http://x"
        assert ev["labels"] == ["p0"]
        assert ev["owner_id"] == "u1"
        # p0 label lives under data → urgency must now be detectable
        assert is_urgent(ev) is True


class TestReviewFixes:
    def test_routine_promotion_pr_skipped(self):
        assert classify_event({"title": "Promote staging to main", "action": "opened"}) == "skip"
        assert classify_event({"title": "staging -> main", "action": "opened"}) == "skip"

    def test_urgent_promotion_still_immediate(self):
        assert classify_event({"title": "Promote staging to main", "labels": ["p0"]}) == "immediate"

    def test_normal_pr_not_skipped(self):
        assert classify_event({"title": "Add pockets to garment card", "action": "opened"}) == "digest"

    def test_word_boundary_urgency_no_false_positive(self):
        assert is_urgent({"title": "production downtime resolved"}) is False
        assert is_urgent({"title": "coincidental cleanup"}) is False
        assert is_urgent({"title": "prod down now"}) is True

    def test_slack_control_chars_escaped(self):
        line = format_line({"title": "<!channel> ship it", "action": "opened"})
        assert "<!channel>" not in line
        assert "&lt;!channel&gt;" in line


class TestUnclaimedPool:
    async def test_count_zero_when_pool_unset(self, monkeypatch):
        import brain.systems.change_notifications_cycle as cyc

        monkeypatch.delenv("ILLO_UNCLAIMED_POOL_USER_ID", raising=False)
        # session is present but the pool is off -> 0 without touching the DB.
        assert await cyc._count_unclaimed(object(), "org-1") == 0


async def test_failed_sends_are_contained_and_counted(db_less_session=None):
    """A raising sender (token unavailable, Slack rejection) must not kill the
    tick; remaining messages still go out and the summary says how many
    failed (cross-family review finding, 2026-07-16)."""
    from unittest.mock import AsyncMock, patch

    from brain.systems.change_notifications_cycle import run_notify_cycle

    sent: list[str] = []

    async def flaky_sender(channel_id, text):
        if not sent:  # first send fails, later sends succeed
            sent.append("FAILED")
            raise RuntimeError("slack rejected the message")
        sent.append(text)

    events = [
        {"kind": "assigned", "title": f"item {i}", "urgent": True, "owner_id": None}
        for i in range(2)
    ]
    with (
        patch("brain.systems.change_notifications_cycle._maybe_run_alert_resolution_harvest",
              new=AsyncMock(return_value=None)),
        patch("brain.systems.change_notifications_cycle._load_change_events",
              new=AsyncMock(return_value=events)),
        patch("brain.systems.change_notifications_cycle._fill_owner_labels",
              new=AsyncMock(return_value=None)),
        patch("brain.systems.change_notifications_cycle._count_unclaimed",
              new=AsyncMock(return_value=0)),
        patch("brain.systems.change_notifications_cycle.render_outbound",
              return_value={"immediate": ["m1", "m2"], "digest": "d1"}),
    ):
        summary = await run_notify_cycle(
            None, org_id="o", channel_id="C1", post=flaky_sender,
        )

    assert summary["post_failures"] == 1
    assert summary["digest_posted"] is True  # later sends still attempted
    assert sent == ["FAILED", "m2", "d1"]

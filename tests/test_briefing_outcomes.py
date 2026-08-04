"""Slice 07 (illo-handoff-packets): packet outcome reporting (pure).

Contract under test: launched means launch_count>0 (never status), a
supersede chain counts as one job (mint time = first revision, launched if
any revision launched), ignored needs the 48h horizon, and the digest line
is one honest sentence or nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from brain.app.api.routers.agent_mcp_handoffs import read_packet_outcomes
from brain.systems.briefing import outcomes as packet_outcomes_module
from brain.systems.briefing.outcomes import (
    DEFAULT_OUTCOME_WINDOW_HOURS,
    MAX_OUTCOME_WINDOW_HOURS,
    MIN_OUTCOME_WINDOW_HOURS,
    OutcomeSummary,
    format_outcomes_line,
    load_packet_outcome_report,
    normalize_outcome_window_hours,
    packet_outcomes,
)

_NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def _row(row_id, *, created_hours_ago, launch_count=0, launched_minutes_after=None,
         owner="u-axel", supersedes=None, status="open"):
    created = _NOW - timedelta(hours=created_hours_ago)
    meta = {"owner_user_id": owner, "job_ref": f"idea:{row_id}"}
    if supersedes:
        meta["supersedes"] = supersedes
    return SimpleNamespace(
        id=row_id,
        created_at=created,
        launch_count=launch_count,
        last_launched_at=(created + timedelta(minutes=launched_minutes_after))
        if launched_minutes_after is not None
        else None,
        metadata_=meta,
        status=status,
    )


def test_launched_means_launch_count_not_status():
    rows = [
        _row("a", created_hours_ago=10, launch_count=1, launched_minutes_after=22, status="archived"),
        _row("b", created_hours_ago=10, launch_count=0, status="launched"),  # lying status
    ]
    summary = packet_outcomes(rows, now=_NOW)
    assert summary.minted == 2
    assert summary.launched == 1  # 'a' by count, never 'b' by status


def test_supersede_chain_counts_once_with_first_mint_time():
    rows = [
        _row("old", created_hours_ago=30, launch_count=0),
        _row("new", created_hours_ago=1, launch_count=1, launched_minutes_after=10,
             supersedes="old"),
    ]
    summary = packet_outcomes(rows, now=_NOW)
    assert summary.minted == 1  # one job, two revisions
    assert summary.launched == 1
    # mint time is the FIRST revision's; launch 29h later ≈ 1750 minutes
    assert summary.median_minutes_to_launch > 24 * 60


def test_ignored_needs_the_horizon_pending_before_it():
    rows = [
        _row("fresh", created_hours_ago=2),     # pending, not ignored
        _row("stale", created_hours_ago=72),    # ignored
    ]
    summary = packet_outcomes(rows, now=_NOW)
    assert summary.pending == 1
    assert summary.ignored == 1


def test_per_member_split_uses_newest_revision_owner():
    rows = [
        _row("old", created_hours_ago=30, owner="u-reda"),
        _row("new", created_hours_ago=1, owner="u-axel", supersedes="old",
             launch_count=2, launched_minutes_after=5),
        _row("solo", created_hours_ago=3, owner=None),
    ]
    summary = packet_outcomes(rows, now=_NOW)
    assert summary.per_member["u-axel"] == {"minted": 1, "launched": 1}
    assert summary.per_member["unclaimed"] == {"minted": 1, "launched": 0}
    assert "u-reda" not in summary.per_member  # reassigned chain follows the newest owner


def test_median_is_median():
    rows = [
        _row("a", created_hours_ago=5, launch_count=1, launched_minutes_after=10),
        _row("b", created_hours_ago=5, launch_count=1, launched_minutes_after=20),
        _row("c", created_hours_ago=5, launch_count=1, launched_minutes_after=90),
    ]
    assert packet_outcomes(rows, now=_NOW).median_minutes_to_launch == 20.0


def test_digest_line_shapes():
    assert format_outcomes_line(packet_outcomes([], now=_NOW)) is None
    line = format_outcomes_line(
        packet_outcomes(
            [
                _row("a", created_hours_ago=5, launch_count=1, launched_minutes_after=22),
                _row("b", created_hours_ago=72),
            ],
            now=_NOW,
        )
    )
    assert line == "Packets: 2 minted · 1 launched · 1 ignored >48h · median 22m to launch"
    hours = format_outcomes_line(
        packet_outcomes(
            [_row("a", created_hours_ago=10, launch_count=1, launched_minutes_after=180)],
            now=_NOW,
        )
    )
    assert "median 3.0h to launch" in hours


def test_empty_summary_is_json_safe():
    summary = packet_outcomes([], now=_NOW)
    assert summary == OutcomeSummary(
        minted=0, launched=0, ignored=0, pending=0,
        median_minutes_to_launch=None, per_member={},
    )
    assert summary.to_dict()["per_member"] == {}


def test_outcome_window_normalization_owns_defaults_and_bounds():
    assert normalize_outcome_window_hours(None) == DEFAULT_OUTCOME_WINDOW_HOURS
    assert normalize_outcome_window_hours("invalid") == DEFAULT_OUTCOME_WINDOW_HOURS
    assert normalize_outcome_window_hours(float("nan")) == DEFAULT_OUTCOME_WINDOW_HOURS
    assert normalize_outcome_window_hours(0) == MIN_OUTCOME_WINDOW_HOURS
    assert normalize_outcome_window_hours(24 * 365) == MAX_OUTCOME_WINDOW_HOURS


async def test_report_loader_formats_the_canonical_window_line_without_history_query(
    monkeypatch,
):
    rows = [
        _row("ignored", created_hours_ago=72),
        _row(
            "launched",
            created_hours_ago=4,
            launch_count=1,
            launched_minutes_after=60,
        ),
    ]

    async def fake_load_packet_handoffs(session, *, org_id, since):
        assert isinstance(session, NoHistoryQuerySession)
        assert org_id == "org-1"
        assert since == _NOW - timedelta(hours=DEFAULT_OUTCOME_WINDOW_HOURS)
        return rows

    class NoHistoryQuerySession:
        async def scalar(self, _statement):
            raise AssertionError("window reports must not query launch history")

    monkeypatch.setattr(
        packet_outcomes_module,
        "load_packet_handoffs",
        fake_load_packet_handoffs,
    )

    report = await load_packet_outcome_report(
        NoHistoryQuerySession(),
        org_id="org-1",
        now=_NOW,
    )

    assert report.digest_line == (
        "Packets: 2 minted · 1 launched · 1 ignored >48h · median 60m to launch"
    )


async def test_mcp_packet_outcomes_returns_only_the_window_report():
    class EmptyScalarResult:
        def scalars(self):
            return self

        def all(self):
            return []

    class WindowReadSession:
        def __init__(self):
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            return EmptyScalarResult()

        async def scalar(self, _statement):
            raise AssertionError("MCP packet reads must not query launch history")

    session = WindowReadSession()
    result = await read_packet_outcomes(
        session,
        SimpleNamespace(org_id="org-1"),
        {"since_hours": "invalid"},
    )

    assert result == {
        "since_hours": DEFAULT_OUTCOME_WINDOW_HOURS,
        "outcomes": {
            "minted": 0,
            "launched": 0,
            "ignored": 0,
            "pending": 0,
            "median_minutes_to_launch": None,
            "per_member": {},
        },
        "digest_line": None,
    }
    assert len(session.statements) == 1

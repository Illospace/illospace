"""Slice 01 (illo-handoff-packets): dossier assembly core.

The contract under test: deterministic, budgeted, deduplicated assembly
where every cut is visible — markers on truncated excerpts, counts on
dropped items, human-readable omission lines. See
specs/illo-handoff-packets/slices/01-dossier-core.md.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brain.systems.briefing import (
    SOURCE_PRIORITY,
    DossierBudget,
    SourcePiece,
    assemble_dossier,
)
from brain.systems.briefing.__main__ import main as briefing_cli

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "briefing"
_T0 = datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc)


def _piece(source: str, ref: str, *, title: str = "t", body: str = "one two three",
           ts: datetime | None = None, weight: int = 0) -> SourcePiece:
    return SourcePiece(source=source, ref=ref, title=title, body=body, ts=ts, weight=weight)


def _budget(**overrides) -> DossierBudget:
    return DossierBudget(**overrides)


def test_zero_pieces_yields_empty_but_valid_dossier():
    dossier = assemble_dossier([], job_ref="idea:1", budget=_budget())
    assert dossier.sections == ()
    assert dossier.omissions == ()
    assert dossier.headline == "idea:1"
    assert "job: idea:1" in dossier.render_text()
    assert dossier.total_chars == len(dossier.render_text())


def test_requires_job_ref():
    with pytest.raises(ValueError):
        assemble_dossier([], job_ref="  ", budget=_budget())


def test_budget_validation_rejects_non_positive():
    with pytest.raises(ValueError):
        DossierBudget(total_chars=0)
    with pytest.raises(ValueError):
        DossierBudget(source_overrides={"slack_thread": -5})


def test_source_priority_order_and_unknown_sources_last():
    pieces = [
        _piece("posthog", "p1"),
        _piece("evidence", "e1"),
        _piece("slack_thread", "s1"),
        _piece("record", "r1"),
        _piece("aardvark", "a1"),
    ]
    dossier = assemble_dossier(pieces, job_ref="idea:1", budget=_budget())
    got = [section.source for section in dossier.sections]
    assert got == ["record", "slack_thread", "evidence", "aardvark", "posthog"]
    assert set(got[:3]).issubset(set(SOURCE_PRIORITY))


def test_within_source_ordering_weight_then_recency_then_ref():
    pieces = [
        _piece("slack_thread", "s-old", ts=_T0, weight=0),
        _piece("slack_thread", "s-new", ts=_T0 + timedelta(hours=2), weight=0),
        _piece("slack_thread", "s-heavy", ts=_T0 - timedelta(days=1), weight=9),
        _piece("slack_thread", "s-a", weight=0),  # no ts sorts oldest
        _piece("slack_thread", "s-b", weight=0),  # ties break by ref asc
    ]
    dossier = assemble_dossier(pieces, job_ref="idea:1", budget=_budget())
    refs = [item.ref for item in dossier.sections[0].items]
    assert refs == ["s-heavy", "s-new", "s-old", "s-a", "s-b"]


def test_deterministic_regardless_of_input_order():
    pieces = [
        _piece("slack_thread", f"s{i}", ts=_T0 + timedelta(minutes=i), body=f"msg {i} " * 30)
        for i in range(8)
    ] + [_piece("record", "r1", weight=5), _piece("github_issue", "g1")]
    baseline = assemble_dossier(pieces, job_ref="idea:1", budget=_budget(max_items_per_source=4))
    for seed in range(3):
        shuffled = list(pieces)
        random.Random(seed).shuffle(shuffled)
        again = assemble_dossier(shuffled, job_ref="idea:1", budget=_budget(max_items_per_source=4))
        assert again == baseline


def test_dedupe_by_source_and_ref_is_not_an_omission():
    pieces = [
        _piece("slack_thread", "s1", title="first", ts=_T0 + timedelta(minutes=1)),
        _piece("slack_thread", "s1", title="dupe", ts=_T0),
        _piece("github_issue", "s1", title="same ref other source"),
    ]
    dossier = assemble_dossier(pieces, job_ref="idea:1", budget=_budget())
    slack = dossier.sections[0]  # slack_thread outranks github_issue
    assert [item.title for item in slack.items] == ["first"]
    assert slack.omitted_count == 0
    assert dossier.omissions == ()
    assert len(dossier.sections[1].items) == 1  # github_issue keeps its own


def test_empty_refs_are_never_deduped():
    pieces = [_piece("evidence", "", title="a"), _piece("evidence", "", title="b")]
    dossier = assemble_dossier(pieces, job_ref="idea:1", budget=_budget())
    assert len(dossier.sections[0].items) == 2


def test_max_items_per_source_counts_and_surfaces_omissions():
    pieces = [
        _piece("slack_thread", f"s{i}", ts=_T0 + timedelta(minutes=i)) for i in range(8)
    ]
    dossier = assemble_dossier(pieces, job_ref="idea:1", budget=_budget(max_items_per_source=6))
    section = dossier.sections[0]
    assert len(section.items) == 6
    assert section.omitted_count == 2
    assert "slack_thread: 2 items omitted (budget)" in dossier.omissions
    assert "2 items omitted" in dossier.render_text()


def test_excerpt_cut_at_word_boundary_with_visible_marker():
    body = "alpha bravo charlie delta " * 40  # ~1040 chars
    dossier = assemble_dossier(
        [_piece("record", "r1", body=body)], job_ref="idea:1", budget=_budget(excerpt_chars=200)
    )
    item = dossier.sections[0].items[0]
    assert item.truncated
    assert item.omitted_chars > 0
    assert len(item.excerpt) <= 200
    assert f"(+{item.omitted_chars} chars)" in item.excerpt
    head = item.excerpt.split(" … ")[0]
    # No mid-word cut: the kept head must be a prefix of the body ending at a boundary.
    assert body.startswith(head)
    assert body[len(head)] == " "


def test_single_giant_token_hard_cuts_but_still_marks():
    body = "x" * 500
    dossier = assemble_dossier(
        [_piece("record", "r1", body=body)], job_ref="idea:1", budget=_budget(excerpt_chars=120)
    )
    item = dossier.sections[0].items[0]
    assert item.truncated
    assert len(item.excerpt) <= 120
    assert "chars)" in item.excerpt


def test_source_budget_sheds_tail_items_but_keeps_one():
    pieces = [
        _piece("slack_thread", f"s{i}", ts=_T0 + timedelta(minutes=i), body="word " * 60)
        for i in range(5)
    ]
    dossier = assemble_dossier(
        pieces,
        job_ref="idea:1",
        budget=_budget(source_overrides={"slack_thread": 400}, excerpt_chars=300),
    )
    section = dossier.sections[0]
    assert len(section.items) >= 1
    assert len(section.items) + section.omitted_count == 5
    assert section.omitted_count > 0
    assert any(entry.startswith("slack_thread:") for entry in dossier.omissions)


def test_lone_oversized_item_is_recut_to_fit_source_budget():
    dossier = assemble_dossier(
        [_piece("record", "r1", body="word " * 200)],
        job_ref="idea:1",
        budget=_budget(source_overrides={"record": 260}, excerpt_chars=900),
    )
    section = dossier.sections[0]
    assert len(section.items) == 1
    assert section.items[0].truncated
    assert len(section.items[0].excerpt) < 900


def test_total_budget_sheds_lowest_priority_first_and_keeps_top_item():
    pieces = [
        _piece("record", "r1", body="word " * 50, weight=5),
        _piece("slack_thread", "s1", body="word " * 50),
        _piece("evidence", "e1", body="word " * 50),
        _piece("evidence", "e2", body="word " * 50),
    ]
    tight = assemble_dossier(pieces, job_ref="idea:1", budget=_budget(total_chars=700, excerpt_chars=250))
    sources = [section.source for section in tight.sections]
    assert "record" in sources  # top section survives
    dropped = [entry for entry in tight.omissions if entry.startswith("evidence")]
    assert dropped, f"expected evidence to shed first, got omissions={tight.omissions}"
    # The whole render honors what it promised: content within budget means
    # the dossier never exceeds total by more than the exempt footer.
    assert tight.total_chars <= 700 + len("Omitted: " + "; ".join(tight.omissions)) + 2


def test_headline_defaults_to_top_item_title():
    pieces = [_piece("record", "r1", title="Melted hands regression")]
    dossier = assemble_dossier(pieces, job_ref="idea:1", budget=_budget())
    assert dossier.headline == "Melted hands regression"
    explicit = assemble_dossier(pieces, job_ref="idea:1", budget=_budget(), headline="Custom")
    assert explicit.headline == "Custom"


def test_total_chars_is_render_length():
    pieces = [_piece("record", "r1"), _piece("slack_thread", "s1")]
    dossier = assemble_dossier(pieces, job_ref="idea:1", budget=_budget())
    assert dossier.total_chars == len(dossier.render_text())


def test_golden_fixture_snapshot():
    fixture = FIXTURE_DIR / "uwear_bug.json"
    expected = json.loads((FIXTURE_DIR / "uwear_bug.expected.json").read_text())
    data = json.loads(fixture.read_text())
    from brain.systems.briefing.__main__ import _piece as piece_from_dict

    dossier = assemble_dossier(
        [piece_from_dict(item) for item in data["pieces"]],
        job_ref=data["job_ref"],
        budget=DossierBudget(**data["budget"]),
        headline=data.get("headline"),
    )
    assert dossier.to_dict() == expected


def test_cli_probe_prints_text_and_json(capsys):
    exit_code = briefing_cli(["--fixture", str(FIXTURE_DIR / "uwear_bug.json")])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "# Maison L. batch 8841 melted hands" in out
    assert '"job_ref": "domain_record:1238"' in out

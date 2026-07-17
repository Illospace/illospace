"""Slice 02 (illo-handoff-packets): dual-audience packet composer.

Contract under test: deterministic dual render (human brief + handoff
input), the review-hardened idempotency model — the revision hashes EVERY
persisted, launch-affecting field — and cross-family review regressions:
cumulative truncation markers, strict brief cap, structured trimming
totals, launch-line fill safety.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brain.systems.briefing import DossierBudget, SourcePiece, assemble_dossier
from brain.systems.briefing.compose import (
    BRIEF_CHAR_CAP,
    LAUNCH_URL_PLACEHOLDER,
    UNCLAIMED_LABEL,
    compose_packet,
    fill_launch_url,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "briefing"
_T0 = datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc)


def _fixture_dossier():
    data = json.loads((FIXTURE_DIR / "uwear_bug.json").read_text())
    pieces = [
        SourcePiece(
            source=item["source"],
            ref=item["ref"],
            title=item["title"],
            body=item["body"],
            ts=datetime.fromisoformat(item["ts"]) if item.get("ts") else None,
            weight=int(item.get("weight", 0)),
        )
        for item in data["pieces"]
    ]
    return assemble_dossier(
        pieces, job_ref=data["job_ref"], budget=DossierBudget(**data["budget"])
    )


def _compose(dossier=None, **overrides):
    kwargs = dict(
        org_id="org-1",
        ask="fix the melted-hands batch and rerun the 41 generations",
        owner_user_id="8b6f3f7e-0000-0000-0000-000000000001",
        owner_label="Axel",
        target_tool="codex",
        repo_origin_url="https://github.com/uwear/uwear-backend.git",
        branch_hint="fix/default-model-backfill",
    )
    kwargs.update(overrides)
    return compose_packet(dossier or _fixture_dossier(), **kwargs)


def test_brief_shape_and_placeholder():
    packet = _compose()
    lines = packet.human_brief.splitlines()
    assert lines[0].startswith("*Maison L. batch 8841 melted hands* → Axel")
    assert lines[1].startswith("*What happened:* ")
    assert lines[2].startswith("*Evidence:* ")
    assert "uwear-backend#346" in lines[2] and "uwear-backend#347" in lines[2]
    assert lines[3].startswith("*Prior decisions:* Reda: rerun the 41")
    assert lines[4].startswith("*Ask:* fix the melted-hands batch")
    # Fixture drops 2 slack items AND shortens 2 excerpts — both must show.
    assert "2 items omitted" in lines[4]
    assert "2 excerpts shortened" in lines[4]
    assert lines[-1] == f"Launch: {LAUNCH_URL_PLACEHOLDER}"


def test_unclaimed_owner_label_default():
    packet = _compose(owner_label=None, owner_user_id=None)
    assert f"→ {UNCLAIMED_LABEL}" in packet.human_brief.splitlines()[0]


def test_handoff_input_carries_dossier_and_provenance():
    packet = _compose(
        acceptance_criteria=["reply lands in origin thread"],
        source_ref={"channel": "C0PROD", "thread_ts": "1751964840"},
    )
    handoff = packet.handoff_input
    assert handoff.org_id == "org-1"
    assert handoff.title == "Maison L. batch 8841 melted hands"
    assert "handoff.get" in handoff.instructions
    assert handoff.acceptance_criteria == ["reply lands in origin thread"]
    assert handoff.source_ref == {"channel": "C0PROD", "thread_ts": "1751964840"}
    assert handoff.metadata["owner_user_id"].endswith("0001")
    assert handoff.metadata["revision"] == packet.revision
    sources = {part["source"] for part in handoff.context_parts}
    assert {"record", "slack_thread", "github_issue"}.issubset(sources)
    omission_parts = [part for part in handoff.context_parts if part["source"] == "omissions"]
    assert omission_parts and omission_parts[0]["notes"]
    # Agent audience sees the marker inline plus the structured fields.
    truncated_parts = [part for part in handoff.context_parts if part.get("truncated")]
    assert truncated_parts
    assert all("chars)" in part["excerpt"] for part in truncated_parts)


def test_chantier_context_flows_to_both_audiences_within_brief_cap():
    dossier = assemble_dossier(
        [
            SourcePiece(
                source="record",
                ref="domain_record:1238",
                title="Handoff dossiers inherit chantier context",
                body="external_id: github:Illospace/illospace:issue:330; status: In Progress",
                weight=10,
            ),
            SourcePiece(
                source="chantier",
                ref="domain_record:1400",
                title="Agent runtime chantier layer",
                body=(
                    "goal: Done means no work arrives cold; state: building; kind: feature; "
                    "owner: Reda; next_step: ship context; siblings: contract (state: Done); "
                    "artifacts: PRD (doc: specs/chantier.md)"
                ),
                weight=10,
            ),
        ],
        job_ref="domain_record:1238",
        budget=DossierBudget(),
    )

    packet = _compose(dossier)

    chantier_line = next(
        line for line in packet.human_brief.splitlines() if line.startswith("*Chantier:*")
    )
    assert "goal: Done means no work arrives cold" in chantier_line
    assert "state: building" in chantier_line
    assert "siblings: contract (state: Done)" in chantier_line
    assert "artifacts: PRD (doc: specs/chantier.md)" in chantier_line
    assert len(packet.human_brief) <= BRIEF_CHAR_CAP
    chantier_parts = [
        part for part in packet.handoff_input.context_parts if part["source"] == "chantier"
    ]
    assert len(chantier_parts) == 1
    assert "siblings: contract (state: Done)" in chantier_parts[0]["excerpt"]


def test_oversized_chantier_is_honestly_trimmed_for_both_audiences():
    dossier = assemble_dossier(
        [
            SourcePiece(source="record", ref="r1", title="Item", body="item context", weight=10),
            SourcePiece(
                source="chantier",
                ref="domain_record:1400",
                title="Large chantier",
                body="goal: Done means " + "sibling state and artifact context " * 200,
                weight=10,
            ),
        ],
        job_ref="domain_record:1238",
        budget=DossierBudget(excerpt_chars=600),
    )
    packet = _compose(dossier, ask="Implement with all acceptance criteria " * 20)

    chantier_line = next(
        line for line in packet.human_brief.splitlines() if line.startswith("*Chantier:*")
    )
    assert "chars)" in chantier_line
    assert len(packet.human_brief) <= BRIEF_CHAR_CAP
    chantier_part = next(
        part for part in packet.handoff_input.context_parts if part["source"] == "chantier"
    )
    assert chantier_part["truncated"] is True
    assert chantier_part["omitted_chars"] > 0
    assert "chars)" in chantier_part["excerpt"]


def test_revision_stable_for_identical_inputs():
    assert _compose().idempotency_key == _compose().idempotency_key


@pytest.mark.parametrize(
    "change",
    [
        {"ask": "different ask entirely"},
        {"owner_user_id": "8b6f3f7e-0000-0000-0000-000000000002"},
        {"target_tool": "claude"},
        {"acceptance_criteria": ["new criterion"]},
        # Cross-family review finding 1: persisted launch-affecting fields
        # MUST rotate the key — a stale row would launch the wrong repo/branch.
        {"repo_origin_url": "https://github.com/uwear/other-repo.git"},
        {"branch_hint": "hotfix/other-branch"},
        {"source_surface": "slack"},
        {"source_ref": {"channel": "C0OTHER"}},
    ],
)
def test_revision_changes_when_persisted_content_changes(change):
    assert _compose().idempotency_key != _compose(**change).idempotency_key


def test_revision_ignores_display_only_and_audit_only_fields():
    # owner_label is display-only (derived from owner_user_id at post time);
    # created_by_user_id is audit-only — identical content re-minted by a
    # different actor SHOULD reuse the row.
    base = _compose()
    assert base.idempotency_key == _compose(owner_label="Renamed Axel").idempotency_key
    assert base.idempotency_key == _compose(created_by_user_id="illo-2").idempotency_key


def test_revision_changes_when_dossier_truth_changes():
    base = _fixture_dossier()
    changed = assemble_dossier(
        [
            SourcePiece(
                source="record", ref="domain_record:1238",
                title="Maison L. batch 8841 melted hands",
                body="tracker updated: rerun verified clean", ts=_T0, weight=10,
            )
        ],
        job_ref=base.job_ref,
        budget=DossierBudget(),
    )
    assert _compose(base).idempotency_key != _compose(changed).idempotency_key


def test_idempotency_key_fits_column_for_pathological_job_ref():
    long_ref = "record:" + "x" * 400
    dossier = assemble_dossier(
        [SourcePiece(source="record", ref=long_ref, title="t", body="b")],
        job_ref=long_ref,
        budget=DossierBudget(),
    )
    packet = _compose(dossier)
    assert len(packet.idempotency_key) <= 120
    assert packet.idempotency_key.endswith(packet.revision)


def test_brief_narrative_marker_is_cumulative_never_understating():
    # A dossier-level cut followed by a brief-level cut must report the
    # TOTAL distance from the raw source (cross-family review finding 2).
    body = "lorem ipsum dolor sit amet " * 200  # ~5400 chars
    normalized = " ".join(body.split())
    dossier = assemble_dossier(
        [SourcePiece(source="record", ref="r1", title="big", body=body, weight=5)],
        job_ref="idea:9",
        budget=DossierBudget(excerpt_chars=600),
    )
    packet = _compose(dossier)
    narrative_line = packet.human_brief.splitlines()[1]
    head = narrative_line.removeprefix("*What happened:* ").split(" … (+")[0]
    reported = int(narrative_line.rsplit("(+", 1)[1].split(" chars")[0])
    assert normalized.startswith(head)
    assert reported == len(normalized) - len(head)  # exact, cumulative
    assert reported > dossier.sections[0].items[0].omitted_chars  # brief cut added to it


def test_fully_shed_sections_count_in_trimming_note():
    pieces = [SourcePiece(source="record", ref="r1", title="t", body="word " * 30, weight=5)] + [
        SourcePiece(source="evidence", ref=f"e{i}", title="e", body="word " * 40)
        for i in range(10)
    ]
    dossier = assemble_dossier(
        pieces, job_ref="idea:9",
        budget=DossierBudget(total_chars=250, excerpt_chars=120, max_items_per_source=10),
    )
    packet = _compose(dossier)
    ask_line = [line for line in packet.human_brief.splitlines() if line.startswith("*Ask:*")][0]
    assert "10 items omitted" in ask_line  # not "1" — structured totals, not string counts


def test_brief_cap_is_strict_under_adversarial_inputs():
    noisy = assemble_dossier(
        [
            SourcePiece(source="record", ref="r1", title="T " * 60, body="word " * 400, weight=5),
            SourcePiece(source="decision", ref="d1", title="d", body="decide " * 200),
            SourcePiece(source="github_issue", ref="uwear-backend#" + "9" * 60, title="i", body="x"),
        ]
        + [
            SourcePiece(source="slack_thread", ref=f"s{i}", title="m", body="chat " * 80)
            for i in range(9)
        ],
        job_ref="idea:9",
        budget=DossierBudget(max_items_per_source=2, excerpt_chars=580),
    )
    packet = _compose(noisy, ask="a very long ask " * 40, owner_label="Some Very Long Owner Label Here")
    assert len(packet.human_brief) <= BRIEF_CHAR_CAP  # exact contract, no slack
    assert packet.human_brief.splitlines()[-1] == f"Launch: {LAUNCH_URL_PLACEHOLDER}"
    assert "context trimmed" in packet.human_brief


def test_fill_launch_url_replaces_only_the_final_line():
    # Even when interpolated fields contain the placeholder text, only the
    # final launch line is filled (cross-family review finding 8).
    packet = _compose(ask=f"echo {LAUNCH_URL_PLACEHOLDER} into the thread", owner_label=LAUNCH_URL_PLACEHOLDER)
    filled = fill_launch_url(packet.human_brief, "https://illo.example/api/launch-handoffs/h1/launch?target=codex")
    lines = filled.splitlines()
    assert lines[-1] == "Launch: https://illo.example/api/launch-handoffs/h1/launch?target=codex"
    assert LAUNCH_URL_PLACEHOLDER in lines[0]  # owner's literal text untouched
    assert LAUNCH_URL_PLACEHOLDER in lines[4]  # ask's literal text untouched


def test_fill_launch_url_rejects_tampered_brief():
    with pytest.raises(ValueError):
        fill_launch_url("no launch line here", "https://example.com")


def test_requires_ask():
    with pytest.raises(ValueError):
        _compose(ask="   ")


def test_composer_is_deterministic():
    first = _compose()
    second = _compose()
    assert first.human_brief == second.human_brief
    assert first.handoff_input == second.handoff_input

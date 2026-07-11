"""Slice 02 (illo-handoff-packets): dual-audience packet composer.

Contract under test: deterministic dual render (human brief + handoff
input), and the review-hardened idempotency model — the revision hashes the
COMPOSE OUTPUT (dossier + ask + criteria + owner + target), so any input
that changes the packet changes the key; unchanged truth reuses it.
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
    assert "context trimmed" in lines[4]  # fixture omits 2 slack items
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


def test_revision_stable_for_identical_inputs():
    assert _compose().idempotency_key == _compose().idempotency_key


@pytest.mark.parametrize(
    "change",
    [
        {"ask": "different ask entirely"},
        {"owner_user_id": "8b6f3f7e-0000-0000-0000-000000000002"},
        {"target_tool": "claude"},
        {"acceptance_criteria": ["new criterion"]},
    ],
)
def test_revision_changes_when_packet_content_changes(change):
    assert _compose().idempotency_key != _compose(**change).idempotency_key


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


def test_brief_cap_tightens_narrative_never_launch_or_omissions():
    noisy = assemble_dossier(
        [
            SourcePiece(source="record", ref="r1", title="T " * 40, body="word " * 400, weight=5),
            SourcePiece(source="decision", ref="d1", title="d", body="decide " * 120),
        ]
        + [
            SourcePiece(source="slack_thread", ref=f"s{i}", title="m", body="chat " * 50)
            for i in range(9)
        ],
        job_ref="idea:9",
        budget=DossierBudget(max_items_per_source=2, excerpt_chars=580),
    )
    packet = _compose(noisy, ask="a very long ask " * 20)
    assert len(packet.human_brief) <= BRIEF_CHAR_CAP + 200  # structural overhead is bounded
    assert packet.human_brief.splitlines()[-1] == f"Launch: {LAUNCH_URL_PLACEHOLDER}"
    assert "context trimmed" in packet.human_brief


def test_requires_ask():
    with pytest.raises(ValueError):
        _compose(ask="   ")


def test_composer_is_deterministic():
    first = _compose()
    second = _compose()
    assert first.human_brief == second.human_brief
    assert first.handoff_input == second.handoff_input

"""Retrieval cues name source subjects instead of introductory filler."""

import pytest

from brain.systems.reconstructive_memory.cues import extract_memory_cues


def test_support_lesson_cues_include_identifiers_and_failure_subjects():
    content = (
        "Principle: vague low-quality reports, especially generic ones. "
        "Cedar; requester briver; company 424242. "
        "Ask for the background, art direction, product description, "
        "reference photo and bug report."
    )

    cues = extract_memory_cues(content)

    assert set(cues) == {
        "cedar", "briver", "424242", "background", "art direction",
        "product description", "reference photo", "bug report",
    }
    assert len(cues) == 8


def test_explicit_identifiers_and_error_phrase_precede_ordinary_prose():
    cues = extract_memory_cues(
        "The launch needs review. Requester @BRiver uses briver@example.test "
        "for acme/photos#123 and company 42. The error is `HTTP 500`."
    )

    assert cues[:5] == (
        "@briver", "briver@example.test", "acme/photos#123", "42", "http 500",
    )
    assert "launch" in cues


def test_quotes_and_unicode_subjects_remain_readable_and_casefolded():
    cues = extract_memory_cues(
        'Étoile has "Art   Direction" problems. Repeat art direction.', limit=12,
    )

    assert "étoile" in cues
    assert "art direction" in cues
    assert cues.count("art direction") == 1


def test_subject_pairs_survive_generic_modifiers_and_longer_word_runs():
    cues = extract_memory_cues(
        "Require detailed art direction guidance and clear product description feedback.",
    )
    assert {"art direction", "product description"} <= set(cues)
    assert not {"detailed", "clear", "require"} & set(cues)


@pytest.mark.parametrize("limit", [0, -1])
def test_empty_budget_returns_no_cues(limit):
    assert extract_memory_cues("Company 42 has a launch plan.", limit=limit) == ()


def test_generic_words_do_not_become_single_word_cues():
    assert extract_memory_cues(
        "Vague low-quality reports, especially generic notes and lessons. "
        "Usually truly different information."
    ) == ()


def test_cues_are_deduplicated_and_bounded_in_stable_order():
    content = "Company 42; company 42; requester briver; background; art direction."
    assert extract_memory_cues(content, limit=3) == ("42", "briver", "background")
    assert extract_memory_cues(content, limit=3) == extract_memory_cues(content, limit=3)


def test_oversized_quoted_text_does_not_fill_the_cue_budget():
    assert "background" in extract_memory_cues('"' + "x" * 101 + '"; background.')


@pytest.mark.parametrize("content", ["Company ID:42", "Account=123", "Profile#7", "Requester:briver"])
def test_label_delimiters_do_not_require_extra_whitespace(content):
    expected = {"Company ID:42": "42", "Account=123": "123", "Profile#7": "7", "Requester:briver": "briver"}
    assert expected[content] in extract_memory_cues(content)


def test_generic_single_words_remain_useful_in_explicit_subject_phrases():
    cues = extract_memory_cues('Weekly update; "monthly report"; bug report.')
    assert {"weekly update", "monthly report", "bug report"} <= set(cues)
    assert not {"weekly", "update", "monthly", "report"} & set(cues)


def test_many_overlapping_identifier_spans_keep_later_subjects():
    content = " ".join(f"requester @user{i}" for i in range(2000)) + "; background."
    cues = extract_memory_cues(content, limit=2001)
    assert len(cues) == 2001
    assert cues[-1] == "background"


@pytest.mark.timeout(1)
def test_long_dot_separated_prose_does_not_restart_the_identifier_scan():
    assert extract_memory_cues("a." * 32000 + "; background.") == ("background",)


@pytest.mark.timeout(1)
def test_unmatched_smart_quote_openers_do_not_restart_the_quote_scan():
    assert extract_memory_cues("“a " * 32000 + "; background.") == ("background",)

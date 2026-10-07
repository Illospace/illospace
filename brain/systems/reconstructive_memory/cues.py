"""Bounded, deterministic retrieval terms from source text.

Identifiers and explicit terms outrank ordinary prose. Short runs of meaningful
words stay together, so a cue can name a subject such as ``art direction``.
This extractor does not change content identity or interpret the source's claims.
"""

from __future__ import annotations

import re

_WORDS = re.compile(r"[^\W_]+(?:[-_][^\W_]+)*", re.UNICODE)
_IDENTIFIERS = re.compile(
    r"(?<![A-Za-z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    r"|(?<![\w@])@[A-Za-z][A-Za-z0-9_]*(?:[.-][A-Za-z0-9_]+)*"
    r"|(?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:#\d+)?\b"
    r"|(?<!\w)#\d+\b"
    r"|\b\d{5,}\b"
)
_LABELED_IDENTIFIER = re.compile(
    r"\b(?:company|profile|account|customer|requester|user)"
    r"(?:\s+(?:id|number))?(?:\s*[:=#]\s*|\s+)(@?\w+(?:[.-]\w+)*)",
    re.IGNORECASE,
)
_QUOTED_TERMS = re.compile(r"`([^`\n]+)`|\"([^\"\n]+)\"|“([^“”\n]+)”")

# Function words and generic modifiers break phrases; generic standalone nouns
# below remain useful inside phrases ("bug report", "weekly update").
_PHRASE_BREAKS = frozenset("""
    a about after again all also always an and any are as at be because been before
    being both but by can cannot could did do does each either especially every for
    from had has have how i if in into is it its just may might more most must never
    no not of on only or other our out please principle really should so some such
    than that the their them then there these they this those through to too until
    us very was we were what when where which who why will with without would you
    your me my his hers yours one ones many few several vague low-quality generic
    wrong incorrect different new old useful good clearly simply usually likely
    mostly often strongly truly concrete specific explicit complete incomplete
    clear correct exact full detailed missing enough better bad basic simple
    important request requester company profile account customer user id number
    ask asks asked need needs needed require requires required prefer prefers reject rejects
""".split())
_GENERIC_SINGLE_WORDS = frozenset("""
    report reports note notes lesson lessons update updates weekly daily monthly
    information context content example examples result results thing things
""".split())


def extract_memory_cues(content: str, *, limit: int = 8) -> tuple[str, ...]:
    """Prefer source identifiers and subjects, with a stable bounded result."""
    if limit <= 0:
        return ()

    candidates: dict[str, tuple[int, int]] = {}
    identifier_spans: list[tuple[int, int]] = []

    def add(value: str, priority: int, position: int) -> None:
        cue = " ".join(value.casefold().split()).strip()
        words = _WORDS.findall(cue)
        if not cue or len(cue) > 100 or not words or len(words) > 6:
            return
        if all(word in _PHRASE_BREAKS for word in words):
            return
        if len(words) == 1 and words[0] in _GENERIC_SINGLE_WORDS:
            return
        ordering = (priority, position)
        candidates[cue] = min(candidates.get(cue, ordering), ordering)

    for match in _IDENTIFIERS.finditer(content):
        add(match.group(), 0, match.start())
        identifier_spans.append(match.span())
    for match in _LABELED_IDENTIFIER.finditer(content):
        add(match.group(1), 0, match.start(1))
        identifier_spans.append(match.span(1))
    for match in _QUOTED_TERMS.finditer(content):
        add(next(value for value in match.groups() if value is not None), 1, match.start())

    run: list[re.Match[str]] = []

    def add_run() -> None:
        if not run:
            return
        if len(run) == 1:
            add(" ".join(match.group() for match in run), 2, run[0].start())
        else:
            for left, right in zip(run, run[1:]):
                add(f"{left.group()} {right.group()}", 2 if len(run) <= 3 else 3, left.start())
        for match in run:
            word = match.group().casefold()
            if word not in _GENERIC_SINGLE_WORDS:
                add(word, 4, match.start())
        run.clear()

    identifier_spans.sort()
    span_index = 0
    for match in _WORDS.finditer(content):
        word = match.group()
        while span_index < len(identifier_spans) and identifier_spans[span_index][1] <= match.start():
            span_index += 1
        in_identifier = (
            span_index < len(identifier_spans)
            and identifier_spans[span_index][0] <= match.start()
        )
        if (
            word.casefold() in _PHRASE_BREAKS
            or in_identifier
            or word.isdecimal()
        ):
            add_run()
            continue
        if run and content[run[-1].end():match.start()].strip():
            add_run()
        if word[0].isupper() and word.casefold() not in _GENERIC_SINGLE_WORDS:
            add(word, 1, match.start())
        run.append(match)
    add_run()

    return tuple(sorted(candidates, key=lambda cue: (*candidates[cue], cue))[:limit])

"""Decide which domain changes are worth telling the team (pure).

The notify-loop (a cycle) reads ``domain_events`` since its last run and asks, per
change: urgent, worth a digest, or noise? This module is the pure decision +
formatting core; the cycle wiring (``change_notifications_cycle``) supplies the
events (normalized from ``domain_events``) and posts the result to the team's
Slack channel.

Separation of concerns (the failure mode this whole feature fixes): the decision
is explicit, testable logic here — not a vague prose instruction to the model.

Expected normalized event shape (the wiring maps raw ``domain_events`` rows to
this)::

    {
        "event_type": "record.updated",   # domain event type
        "title": "Login is broken",       # human title of the changed record
        "object_key": "github_ticket",    # record type
        "owner_id": "u-123" | None,        # None => in the unclaimed pool
        "owner_label": "Reda" | None,
        "action": "closed",               # what changed, for the line
        "url": "https://…" | None,
        "labels": ["p0", "bug"],          # any labels/tags on the record
        "priority": "P1" | None,
        "noteworthy": True,               # optional explicit override
    }
"""

from __future__ import annotations

# Terms that make a change worth an immediate ping instead of the next digest.
DEFAULT_URGENT_TERMS = (
    "p0", "sev0", "sev1", "urgent", "critical", "security",
    "outage", "prod down", "production down", "incident",
)

# Domain event types that are never worth notifying on.
_NOISE_EVENT_TYPES = frozenset({
    "schema.created", "schema.updated", "record.read", "record.viewed",
})


def _signals(event: dict) -> str:
    parts = [str(event.get("title", "")), str(event.get("priority", "")), str(event.get("action", ""))]
    parts.extend(str(x) for x in (event.get("labels") or []))
    return " ".join(parts).lower()


def is_urgent(event: dict, *, urgent_terms=DEFAULT_URGENT_TERMS) -> bool:
    signals = _signals(event)
    return any(term in signals for term in urgent_terms)


def classify_event(event: dict, *, urgent_terms=DEFAULT_URGENT_TERMS) -> str:
    """Return ``"immediate"``, ``"digest"``, or ``"skip"`` for one change."""
    if is_urgent(event, urgent_terms=urgent_terms):
        return "immediate"
    if str(event.get("event_type", "")).lower() in _NOISE_EVENT_TYPES:
        return "skip"
    if event.get("noteworthy") is False:
        return "skip"
    return "digest"


def decide_notifications(events, *, urgent_terms=DEFAULT_URGENT_TERMS) -> dict:
    """Bucket events into ``{"immediate": [...], "digest": [...], "skip": [...]}``."""
    out: dict = {"immediate": [], "digest": [], "skip": []}
    for event in events or []:
        out[classify_event(event, urgent_terms=urgent_terms)].append(event)
    return out


def format_line(event: dict) -> str:
    title = event.get("title") or event.get("object_key") or "(untitled)"
    action = event.get("action") or event.get("event_type") or "updated"
    if event.get("owner_id"):
        owner = event.get("owner_label") or event["owner_id"]
    else:
        owner = "unclaimed"
    line = f"• {title} — {action} ({owner})"
    url = event.get("url")
    if url:
        line += f" {url}"
    return line


def format_urgent(event: dict) -> str:
    """Text for an immediate urgent ping."""
    return "⚠️ " + format_line(event).lstrip("• ")


def build_digest(digest_events, *, unclaimed_count: int = 0) -> "str | None":
    """Periodic batch text. Returns ``None`` when there is nothing to say, so a
    quiet interval is a genuine no-op rather than a noisy heartbeat.

    ``digest_events`` are the non-urgent changes (urgent ones post immediately, so
    they are excluded here). ``unclaimed_count`` surfaces Slice 3's pool.
    """
    parts: list = []
    for event in digest_events or []:
        parts.append(format_line(event))
    if unclaimed_count:
        noun = "item" if unclaimed_count == 1 else "items"
        parts.append(
            f"🫳 {unclaimed_count} {noun} waiting for an owner — pick one up when you have capacity."
        )
    if not parts:
        return None
    header = "*What changed*"
    return header + "\n" + "\n".join(parts)


def render_outbound(events, *, unclaimed_count: int = 0, urgent_terms=DEFAULT_URGENT_TERMS) -> dict:
    """Pure orchestration for one notify-cycle tick.

    Turns the changes-since-last-run (plus the current unclaimed count) into the
    messages to send: urgent items each go out immediately, everything else
    batches into one digest (or ``None`` = stay quiet). The cycle wrapper only has
    to read events, count the unclaimed pool, and post these strings.
    """
    decisions = decide_notifications(events, urgent_terms=urgent_terms)
    return {
        "immediate": [format_urgent(e) for e in decisions["immediate"]],
        "digest": build_digest(decisions["digest"], unclaimed_count=unclaimed_count),
    }

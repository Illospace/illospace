"""Narrative lifecycle — topic tagging, linking, and arc synthesis.

Extracts topics from harvest items, creates/updates ProjectNarrative records,
and synthesises arc summaries via the configured default model with deterministic fallback.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from typing import TYPE_CHECKING

from sqlalchemy import select

from brain.platform.db.models.narrative import NarrativeSession, ProjectNarrative
from brain.platform.db.repositories.unit_of_work import UnitOfWork

if TYPE_CHECKING:
    from brain.systems.sessions.harvest_extraction import HarvestItem

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Topic extraction
# ---------------------------------------------------------------------------


def extract_topic_tags(harvest_items: list[HarvestItem], *, max_tags: int = 5) -> list[str]:
    """Return the most common topic tags across all harvest items.

    Tags are lowercased and deduplicated by frequency.  Returns at most
    *max_tags* tags ordered by descending frequency.
    """
    counter: Counter[str] = Counter()
    for item in harvest_items:
        for tag in item.topic_tags:
            counter[tag.lower()] += 1
    return [tag for tag, _ in counter.most_common(max_tags)]


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------

_NON_ALNUM_DASH = re.compile(r"[^a-z0-9\-]")
_MULTI_DASH = re.compile(r"-{2,}")


def slugify_topic(topic: str) -> str:
    """Convert a topic string to a URL-safe slug.

    Lowercase, replace spaces with hyphens, strip non-alphanumeric characters
    (except hyphens), collapse consecutive hyphens.
    """
    slug = topic.lower().replace(" ", "-")
    slug = _NON_ALNUM_DASH.sub("", slug)
    slug = _MULTI_DASH.sub("-", slug)
    return slug.strip("-")


# ---------------------------------------------------------------------------
# Creation threshold
# ---------------------------------------------------------------------------


def should_create_narrative(session_count: int) -> bool:
    """Return True if enough sessions exist to justify a narrative (>= 2)."""
    return session_count >= 2


# ---------------------------------------------------------------------------
# Arc synthesis (configured default model with fallback)
# ---------------------------------------------------------------------------


def _synthesize_arc(title: str, session_summaries: list[str], *, user_id: str | None = None) -> str:
    """Synthesise a narrative arc summary from session entries.

    Calls the configured default model for a concise synthesis. On any failure, falls back
    to concatenating the last 3 entries with `` | `` separator.
    """
    fallback = " | ".join(session_summaries[-3:])
    if not session_summaries:
        return title

    try:
        from brain.platform.integrations.completions import simple_text_completion
        from brain.platform.providers.model_policy import get_default_model

        text = simple_text_completion(
            (
                f"Synthesise a 1-3 sentence narrative arc summary for "
                f'the topic "{title}" from these session summaries:\n\n'
                + "\n".join(f"- {s}" for s in session_summaries)
            ),
            model=get_default_model(include_provider_prefix=True, user_id=user_id),
            max_tokens=300,
            user_id=user_id,
        )
        if text:
            return text
    except Exception:
        logger.warning("Arc synthesis via configured default model failed, using fallback", exc_info=True)

    return fallback


# ---------------------------------------------------------------------------
# Link session to narratives
# ---------------------------------------------------------------------------


async def link_session_to_narratives(
    session_id: str,
    session_date,
    session_summary: str,
    topic_tags: list[str],
    *,
    org_id: str | None = None,
    user_id: str | None = None,
    visibility: str = "private",
) -> list[int]:
    """Create or update narratives for each topic tag and link the session.

    Returns list of narrative IDs that were created or updated.
    """
    narrative_ids: list[int] = []

    async with UnitOfWork() as uow:
        for tag in topic_tags:
            slug = slugify_topic(tag)
            stmt = (
                select(ProjectNarrative)
                .where(
                    ProjectNarrative.topic_slug == slug,
                    ProjectNarrative.user_id == user_id,
                    ProjectNarrative.visibility == visibility,
                    ProjectNarrative.stale_at.is_(None),
                )
            )
            if org_id is not None:
                stmt = stmt.where(ProjectNarrative.org_id == org_id)
            else:
                stmt = stmt.where(ProjectNarrative.org_id.is_(None))

            narrative = (await uow.session.scalars(stmt)).first()

            if narrative is not None:
                # Append session entry
                uow.session.add(NarrativeSession(
                    narrative_id=narrative.id,
                    session_id=session_id,
                    session_date=session_date,
                    summary=session_summary,
                ))
                await uow.session.flush()

                # Regenerate arc summary from all entries
                entries = (
                    await uow.session.scalars(
                        select(NarrativeSession)
                        .where(NarrativeSession.narrative_id == narrative.id)
                        .order_by(NarrativeSession.session_date.asc())
                    )
                ).all()
                summaries = [e.summary for e in entries]
                narrative.arc_summary = _synthesize_arc(narrative.title, summaries, user_id=user_id)
                narrative_ids.append(narrative.id)
            else:
                # Create new narrative
                new_narrative = ProjectNarrative(
                    topic_slug=slug,
                    title=tag.title(),
                    arc_summary=session_summary,
                    org_id=org_id,
                    user_id=user_id,
                    visibility=visibility,
                )
                uow.session.add(new_narrative)
                await uow.session.flush()

                uow.session.add(NarrativeSession(
                    narrative_id=new_narrative.id,
                    session_id=session_id,
                    session_date=session_date,
                    summary=session_summary,
                ))
                narrative_ids.append(new_narrative.id)

    return narrative_ids

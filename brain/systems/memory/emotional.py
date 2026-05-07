"""Emotion-weighted memory retrieval.

Re-ranks vector similarity results by boosting memories whose emotional
signatures match the current emotional context.
"""

from __future__ import annotations

import json
import os
import sys

from sqlalchemy import text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.memory.embeddings import embed_query, vec_to_pg

# Mapping: current emotion → which memory emotion labels get boosted
EMOTION_BOOST_MAP: dict[str, dict[str, float]] = {
    "frustrated": {
        "frustrated": 1.3, "angry": 1.2, "disappointed": 1.2,
        "satisfied": 1.1,  # past solutions to frustration
    },
    "angry": {
        "angry": 1.3, "frustrated": 1.2, "urgent": 1.1,
    },
    "disappointed": {
        "disappointed": 1.3, "frustrated": 1.2, "satisfied": 1.1,
    },
    "curious": {
        "curious": 1.3, "excited": 1.2, "teaching": 1.2,
        "impressed": 1.1,
    },
    "happy": {
        "happy": 1.3, "excited": 1.2, "proud": 1.2, "satisfied": 1.1,
    },
    "excited": {
        "excited": 1.3, "happy": 1.2, "curious": 1.2, "proud": 1.1,
    },
    "urgent": {
        "urgent": 1.3, "frustrated": 1.2, "angry": 1.1,
    },
    "teaching": {
        "teaching": 1.3, "curious": 1.2, "satisfied": 1.1,
    },
    "satisfied": {
        "satisfied": 1.2, "happy": 1.1, "proud": 1.1,
    },
    "confused": {
        "confused": 1.3, "curious": 1.2, "teaching": 1.2,
    },
    "anxious": {
        "anxious": 1.3, "urgent": 1.2, "relieved": 1.2,
    },
    "proud": {
        "proud": 1.3, "happy": 1.2, "excited": 1.1,
    },
}

# Behavioral hints for each detected emotion
BEHAVIORAL_HINTS: dict[str, dict[str, str]] = {
    "frustrated": {
        "pace": "slower",
        "verification": "thorough",
        "tone": "empathetic, acknowledge the frustration",
        "strategy": "show work explicitly, verify before claiming done",
    },
    "angry": {
        "pace": "slower",
        "verification": "thorough",
        "tone": "calm and professional, don't mirror the anger",
        "strategy": "acknowledge the issue, focus on resolution, avoid excuses",
    },
    "disappointed": {
        "pace": "measured",
        "verification": "careful",
        "tone": "understanding, take responsibility where appropriate",
        "strategy": "acknowledge gap between expectation and reality, propose concrete fix",
    },
    "urgent": {
        "pace": "fast but accurate",
        "verification": "essential checks only",
        "tone": "focused and direct",
        "strategy": "prioritize the fix, minimize chatter, confirm before deploying",
    },
    "curious": {
        "pace": "exploratory",
        "verification": "light",
        "tone": "enthusiastic, collaborative",
        "strategy": "explore options together, share interesting tangents",
    },
    "happy": {
        "pace": "natural",
        "verification": "standard",
        "tone": "warm, match the energy",
        "strategy": "build on the momentum, suggest next steps",
    },
    "excited": {
        "pace": "energetic",
        "verification": "standard",
        "tone": "enthusiastic, share the excitement",
        "strategy": "channel the energy productively, validate the vision",
    },
    "teaching": {
        "pace": "attentive",
        "verification": "confirm understanding",
        "tone": "receptive, ask clarifying questions",
        "strategy": "absorb the lesson, connect to existing knowledge",
    },
    "satisfied": {
        "pace": "natural",
        "verification": "standard",
        "tone": "positive, reinforce what worked",
        "strategy": "maintain quality, suggest improvements if appropriate",
    },
    "confused": {
        "pace": "slower",
        "verification": "step by step",
        "tone": "patient, clarifying",
        "strategy": "break down complexity, use examples, confirm understanding",
    },
    "anxious": {
        "pace": "steady and reassuring",
        "verification": "thorough",
        "tone": "calm, provide certainty where possible",
        "strategy": "address concerns directly, provide evidence, outline safety nets",
    },
    "neutral": {
        "pace": "natural",
        "verification": "standard",
        "tone": "professional and friendly",
        "strategy": "standard approach, be helpful",
    },
}

# Valence values for trajectory tracking
EMOTION_VALENCE: dict[str, float] = {
    "frustrated": -0.7,
    "angry": -0.9,
    "disappointed": -0.5,
    "confused": -0.3,
    "anxious": -0.5,
    "neutral": 0.0,
    "curious": 0.3,
    "satisfied": 0.5,
    "happy": 0.8,
    "excited": 0.8,
    "proud": 0.8,
    "urgent": -0.2,
    "relieved": 0.5,
    "teaching": 0.4,
}


def query_with_emotion(
    text: str,
    emotion_context: dict | None = None,
    limit: int = 10,
) -> list[dict]:
    """Query memories with emotion-weighted re-ranking.

    Args:
        text: Query text for semantic search.
        emotion_context: Dict with at least 'emotion' key from detect_emotion.
        limit: Max results to return.

    Returns:
        List of memory dicts with similarity scores, re-ranked by emotional relevance.
    """
    if len(text) <= 10:
        return []

    qemb = embed_query(text)
    emb_str = vec_to_pg(qemb)

    # Fetch more than needed so re-ranking has room to work
    fetch_limit = min(limit * 3, 50)

    with UnitOfWork() as uow:
        result = uow.session.execute(text("""
            SELECT id, content, memory_type, salience, emotion_label,
                   1 - (semantic_embedding <=> CAST(:emb AS vector)) as sim
            FROM memories WHERE NOT archived AND superseded_by IS NULL
            ORDER BY semantic_embedding <=> CAST(:emb AS vector) LIMIT :lim
        """), {"emb": emb_str, "lim": fetch_limit})

        results = []
        for row in result.mappings().all():
            if row["sim"] <= 0.25:
                continue
            results.append({
                "id": row["id"],
                "content": row["content"][:200],
                "type": row["memory_type"],
                "salience": row["salience"],
                "emotion": row["emotion_label"],
                "similarity": round(float(row["sim"]), 3),
            })

    # Apply emotion boost if we have context
    current_emotion = (emotion_context or {}).get("emotion", "neutral")
    boosts = EMOTION_BOOST_MAP.get(current_emotion, {})

    for r in results:
        mem_emotion = r.get("emotion") or "neutral"
        boost = boosts.get(mem_emotion, 1.0)
        r["emotion_boost"] = boost
        r["adjusted_score"] = round(r["similarity"] * boost, 3)

    # Sort by adjusted score
    results.sort(key=lambda r: r["adjusted_score"], reverse=True)

    return results[:limit]


def get_behavioral_hints(emotion: str) -> dict[str, str]:
    """Get behavioral adaptation hints for a detected emotion."""
    return BEHAVIORAL_HINTS.get(emotion, BEHAVIORAL_HINTS["neutral"])


# Session-level emotion trajectory tracking
_session_trajectory: list[dict] = []


def reset_trajectory() -> None:
    """Reset the session trajectory (for testing)."""
    global _session_trajectory
    _session_trajectory = []


def track_emotion(emotion: str, message: str = "") -> dict:
    """Track an emotion in the session trajectory and detect escalation.

    Returns dict with trajectory info and optional escalation_warning.
    """
    valence = EMOTION_VALENCE.get(emotion, 0.0)
    _session_trajectory.append({
        "emotion": emotion,
        "valence": valence,
        "message_preview": message[:50] if message else "",
    })

    result: dict = {
        "trajectory_length": len(_session_trajectory),
        "current_valence": valence,
    }

    # Check for negative escalation over last 3+ entries
    if len(_session_trajectory) >= 3:
        recent = _session_trajectory[-3:]
        valences = [e["valence"] for e in recent]

        # Trending negative: each step same or worse
        trending_negative = all(
            valences[i] <= valences[i - 1] and valences[-1] < -0.2
            for i in range(1, len(valences))
        )

        # Trending positive: each step same or better
        trending_positive = all(
            valences[i] >= valences[i - 1] and valences[-1] > 0.2
            for i in range(1, len(valences))
        )

        if trending_negative:
            result["escalation_warning"] = {
                "trend": "negative",
                "recent_emotions": [e["emotion"] for e in recent],
                "advice": "Emotional trend is worsening. Slow down, change approach, acknowledge the difficulty.",
            }
            result["trend"] = "declining"
        elif trending_positive:
            result["trend"] = "improving"
        else:
            result["trend"] = "stable"
    else:
        result["trend"] = "insufficient_data"

    return result

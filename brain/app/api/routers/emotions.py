"""Emotions router — emotional snapshots with analytics."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db, rate_limit
from brain.platform.db.repositories.emotions import EmotionRepository

router = APIRouter(
    prefix="/api/emotions",
    tags=["emotions"],
    dependencies=[Depends(rate_limit)],
)


def _serialize_snapshot(s: Any) -> dict:
    return {
        "id": s.id,
        "label": s.label,
        "valence": s.valence,
        "arousal": s.arousal,
        "trigger_summary": s.trigger_summary,
        "attributed_to": getattr(s, "attributed_to", None),
        "timestamp": s.timestamp.isoformat() if s.timestamp else None,
    }


@router.get("/")
def list_emotions(
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    repo = EmotionRepository(db)
    snapshots = repo.list_recent(limit=500)

    # Build daily aggregation
    daily: dict[str, dict] = defaultdict(
        lambda: {"count": 0, "valence_sum": 0.0, "arousal_sum": 0.0}
    )
    for s in snapshots:
        day = s.timestamp.strftime("%Y-%m-%d") if s.timestamp else "unknown"
        daily[day]["count"] += 1
        daily[day]["valence_sum"] += s.valence or 0
        daily[day]["arousal_sum"] += s.arousal or 0

    daily_list = [
        {
            "date": k,
            "count": v["count"],
            "avg_valence": round(v["valence_sum"] / v["count"], 3)
            if v["count"]
            else 0,
            "avg_arousal": round(v["arousal_sum"] / v["count"], 3)
            if v["count"]
            else 0,
        }
        for k, v in sorted(daily.items())
    ]

    # Build distribution
    label_counts = Counter(s.label for s in snapshots if s.label)
    distribution = [
        {"label": k, "count": v} for k, v in label_counts.most_common()
    ]

    return {
        "snapshots": [_serialize_snapshot(s) for s in snapshots],
        "daily": daily_list,
        "distribution": distribution,
    }

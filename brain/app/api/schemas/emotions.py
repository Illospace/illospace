from __future__ import annotations
from datetime import datetime
from pydantic import BaseModel

class EmotionalSnapshotRead(BaseModel):
    id: int
    session_date: str | None = None
    timestamp: datetime | None = None
    valence: float
    arousal: float
    label: str | None = None
    trigger_summary: str | None = None
    model_config = {"from_attributes": True}

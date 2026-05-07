from __future__ import annotations
from datetime import date, datetime
from pydantic import BaseModel

class DailyMetricsRead(BaseModel):
    id: int
    metric_date: date
    avg_valence: float | None = None
    avg_arousal: float | None = None
    total_exchanges: int | None = None
    retrieval_attempts: int | None = None
    retrieval_hits: int | None = None
    model_config = {"from_attributes": True}

class ConsolidationRunRead(BaseModel):
    id: int
    run_date: date | None = None
    phase: str | None = None
    status: str | None = None
    memories_created: int | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    model_config = {"from_attributes": True}

class OperatingParamRead(BaseModel):
    key: str
    value: str
    description: str | None = None
    model_config = {"from_attributes": True}

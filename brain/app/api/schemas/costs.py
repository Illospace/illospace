from __future__ import annotations
from datetime import datetime
from pydantic import BaseModel

class RunCostRead(BaseModel):
    id: int
    idea_id: str | None = None
    skill_used: str | None = None
    model_used: str | None = None
    tokens_input: int | None = None
    tokens_output: int | None = None
    tokens_total: int | None = None
    estimated_cost: float | None = None
    created_at: datetime
    model_config = {"from_attributes": True}

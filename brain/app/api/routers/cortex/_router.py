"""Shared router instance for all cortex submodules."""
from fastapi import APIRouter, Depends

from brain.app.api.deps import rate_limit

router = APIRouter(
    prefix="/api/cortex",
    tags=["cortex"],
    dependencies=[Depends(rate_limit)],
)

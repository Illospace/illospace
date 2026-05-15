from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.org import User

from .auth import async_get_openai_connection
from .memory import async_get_runtime_memory
from .models import async_get_runtime_models
from .schemas import RuntimePermissionsRead, RuntimeSettingsRead


def can_manage_runtime_settings(user: User) -> bool:
    return getattr(user, "role", None) in {"owner", "admin"}


async def async_get_runtime_settings(session: AsyncSession, user: User) -> RuntimeSettingsRead:
    return RuntimeSettingsRead(
        connection=await async_get_openai_connection(session, user),
        models=await async_get_runtime_models(session, user),
        memory=await async_get_runtime_memory(session, user),
        permissions=RuntimePermissionsRead(can_manage_settings=can_manage_runtime_settings(user)),
    )

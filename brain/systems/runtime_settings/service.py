from __future__ import annotations

from brain.platform.db.models.org import User

from .auth import get_openai_connection
from .memory import get_runtime_memory
from .models import get_runtime_models
from .schemas import RuntimePermissionsRead, RuntimeSettingsRead


def can_manage_runtime_settings(user: User) -> bool:
    return getattr(user, "role", None) in {"owner", "admin"}


def get_runtime_settings(user: User) -> RuntimeSettingsRead:
    return RuntimeSettingsRead(
        connection=get_openai_connection(user),
        models=get_runtime_models(user),
        memory=get_runtime_memory(user),
        permissions=RuntimePermissionsRead(can_manage_settings=can_manage_runtime_settings(user)),
    )

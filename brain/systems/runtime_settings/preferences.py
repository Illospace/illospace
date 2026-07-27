from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.authorization import PrincipalIdentity, human_identity
from brain.platform.db.models.agent_run import AgentRunEventRow
from brain.platform.db.models.org import User

from .display import (
    DISPLAY_TIMEZONE_SETTING_KEY,
    RUNTIME_DISPLAY_SETTINGS_KEY,
    RUNTIME_PREFERENCE_RECEIPT_KIND,
    RuntimePreferenceWriteReceipt,
    async_get_runtime_display,
    async_get_runtime_display_config,
    async_update_runtime_display,
    normalize_display_timezone,
)
from .schemas import RuntimeDisplayUpdate


class RuntimePreferenceAccessError(PermissionError):
    pass


async def authenticate_runtime_preference_principal(
    session: AsyncSession,
    *,
    user_id: object,
    org_id: object,
) -> PrincipalIdentity:
    """Resolve trusted AgentRun identity into an authenticated owner/admin principal."""

    normalized_user_id = str(user_id or "").strip()
    normalized_org_id = str(org_id or "").strip()
    if not normalized_user_id or not normalized_org_id:
        raise RuntimePreferenceAccessError(
            "authenticated user and workspace context are required"
        )
    user = await session.get(User, normalized_user_id)
    if user is None or str(getattr(user, "org_id", "") or "") != normalized_org_id:
        raise RuntimePreferenceAccessError(
            "authenticated user does not belong to the active workspace"
        )
    role = str(getattr(user, "role", "") or "").strip().lower()
    if role not in {"owner", "admin"}:
        raise RuntimePreferenceAccessError("owner or admin authority is required")
    return human_identity(
        {
            "id": str(user.id),
            "name": str(getattr(user, "name", "") or ""),
            "email": str(getattr(user, "email", "") or ""),
            "role": role,
            "org_id": normalized_org_id,
            "org_name": "",
        }
    )


def _denied_result(detail: str) -> dict[str, Any]:
    return {
        "action": "set",
        "status": "denied",
        "saved": False,
        "setting": DISPLAY_TIMEZONE_SETTING_KEY,
        "detail": f"The display preference was not saved: {detail}.",
    }


async def async_manage_runtime_preferences(
    session: AsyncSession,
    *,
    principal: PrincipalIdentity,
    run_id: int | None,
    action: str = "get",
    setting: str | None = None,
    value: str | None = None,
) -> dict[str, Any]:
    """Inspect or persist preferences for an already-authenticated principal."""

    if (
        principal.principal_type != "human"
        or principal.role not in {"owner", "admin"}
        or not principal.id
        or not principal.org_id
    ):
        return _denied_result("owner or admin authority is required")

    normalized_action = str(action or "get").strip().lower()
    normalized_setting = str(setting or "").strip().lower()
    if normalized_action not in {"get", "set"}:
        raise ValueError("manage_runtime_preferences action must be 'get' or 'set'.")

    if normalized_action == "set":
        if normalized_setting != DISPLAY_TIMEZONE_SETTING_KEY:
            return {
                "action": "set",
                "status": "unsupported",
                "saved": False,
                "setting": normalized_setting or None,
                "detail": (
                    "I can do that for this message, but I have no way to make it stick "
                    "— file it?"
                ),
                "supported_settings": [DISPLAY_TIMEZONE_SETTING_KEY],
            }
        normalized_value = normalize_display_timezone(value)
        receipt = RuntimePreferenceWriteReceipt(
            receipt_id=secrets.token_urlsafe(24),
            run_id=run_id,
            org_id=str(principal.org_id or ""),
            actor_user_id=principal.id,
            setting=DISPLAY_TIMEZONE_SETTING_KEY,
            value=normalized_value,
            recorded_at=datetime.now(timezone.utc).isoformat(),
        )
        display = await async_update_runtime_display(
            session,
            principal,
            RuntimeDisplayUpdate(display_timezone=normalized_value),
            write_receipt=receipt,
        )
        return {
            "action": "set",
            "status": "saved",
            "saved": True,
            "setting": DISPLAY_TIMEZONE_SETTING_KEY,
            "value": display.display_timezone,
            "storage": {
                "table": "vault_config",
                "key": RUNTIME_DISPLAY_SETTINGS_KEY,
                "scope": display.scope,
            },
            "write_receipt": receipt.to_payload(),
            "confirmation": (
                f"Saved: alerts will render {display.display_timezone} alongside UTC "
                f"(setting: {DISPLAY_TIMEZONE_SETTING_KEY}; storage: "
                f"vault_config/{RUNTIME_DISPLAY_SETTINGS_KEY})."
            ),
        }

    if normalized_setting:
        if normalized_setting != DISPLAY_TIMEZONE_SETTING_KEY:
            return {
                "action": "get",
                "status": "unsupported",
                "setting": normalized_setting,
                "supported_settings": [DISPLAY_TIMEZONE_SETTING_KEY],
            }
        display = await async_get_runtime_display(session)
        return {
            "action": "get",
            "status": "ok",
            "setting": DISPLAY_TIMEZONE_SETTING_KEY,
            "value": display.display_timezone,
            "storage": {
                "table": "vault_config",
                "key": RUNTIME_DISPLAY_SETTINGS_KEY,
                "scope": display.scope,
            },
        }

    return {
        "action": "get",
        "status": "ok",
        "supported_settings": [DISPLAY_TIMEZONE_SETTING_KEY],
    }


def denied_runtime_preference_result(exc: RuntimePreferenceAccessError) -> dict[str, Any]:
    return _denied_result(str(exc))


def _successful_receipt_payload(
    result: object,
    *,
    run_id: int,
    org_id: str,
) -> bool:
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            return False
    if not isinstance(result, Mapping):
        return False
    if result.get("status") != "saved" or result.get("saved") is not True:
        return False
    receipt = result.get("write_receipt")
    if not isinstance(receipt, Mapping):
        return False
    try:
        receipt_run_id = int(receipt.get("run_id") or 0)
    except (TypeError, ValueError):
        return False
    return (
        receipt.get("kind") == RUNTIME_PREFERENCE_RECEIPT_KIND
        and receipt_run_id == run_id
        and str(receipt.get("org_id") or "") == org_id
    )


async def async_has_runtime_preference_write_evidence(
    session: AsyncSession,
    *,
    run_id: object,
    org_id: object,
) -> bool:
    """Verify an atomically stored receipt or a successful same-run tool event."""

    try:
        normalized_run_id = int(run_id or 0)
    except (TypeError, ValueError):
        return False
    normalized_org_id = str(org_id or "").strip()
    if normalized_run_id <= 0 or not normalized_org_id:
        return False

    config = await async_get_runtime_display_config(session)
    if any(
        receipt.run_id == normalized_run_id
        and receipt.org_id == normalized_org_id
        and receipt.setting == DISPLAY_TIMEZONE_SETTING_KEY
        for receipt in config.write_receipts
    ):
        return True

    events = list(
        (
            await session.scalars(
                select(AgentRunEventRow)
                .where(
                    AgentRunEventRow.run_id == normalized_run_id,
                    AgentRunEventRow.event_type == "run.tool_completed",
                )
                .order_by(
                    AgentRunEventRow.sequence_no.desc(),
                    AgentRunEventRow.id.desc(),
                )
                .limit(100)
            )
        ).all()
    )
    for event in events:
        payload = getattr(event, "payload", None)
        if not isinstance(payload, Mapping):
            continue
        if str(payload.get("tool_name") or payload.get("tool") or "") != (
            "manage_runtime_preferences"
        ):
            continue
        if _successful_receipt_payload(
            payload.get("result"),
            run_id=normalized_run_id,
            org_id=normalized_org_id,
        ):
            return True
    return False


__all__ = [
    "RuntimePreferenceAccessError",
    "async_has_runtime_preference_write_evidence",
    "async_manage_runtime_preferences",
    "authenticate_runtime_preference_principal",
    "denied_runtime_preference_result",
]

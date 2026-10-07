"""Durable credential expiry and one alert per personal Codex connection."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.common.time import assume_utc
from brain.platform.db.models.org import UserCodexConnection
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.platform.integrations.openai_codex_auth import CodexCredentialExpiredError, parse_codex_auth_payload
from brain.systems.cortex.thread_links import public_app_base_url
from brain.systems.failure_guard.slack_delivery import (
    FailureAlertPresentation,
    FailureAlertSubject,
    SlackFailureAlertPolicy,
    async_deliver_failure_alert,
)
from brain.systems.slack.client import slack_web_client_from_runtime

logger = logging.getLogger(__name__)
_WRITE_TRANSACTION_KEY = "codex_credential_write_transaction"


def note_codex_credential_write(session: AsyncSession, user_id: str) -> None:
    """Track the transaction that already owns a connection's write lock."""
    transaction = session.get_transaction()
    assert transaction is not None
    recorded = session.info.get(_WRITE_TRANSACTION_KEY)
    users = recorded[1] if recorded and recorded[0] is transaction else set()
    users.add(user_id)
    session.info[_WRITE_TRANSACTION_KEY] = (transaction, users)


def owns_codex_credential_write(session: AsyncSession, user_id: str) -> bool:
    transaction = session.get_transaction()
    recorded = session.info.get(_WRITE_TRANSACTION_KEY)
    return bool(
        transaction is not None and recorded
        and recorded[0] is transaction and user_id in recorded[1]
    )


def replace_codex_credential(connection: UserCodexConnection, encrypted: bytes) -> None:
    """Reset the circuit only when a different credential replaces the old one."""
    from brain.systems.vault import _decrypt

    replacement = _credential_identity(_decrypt(encrypted))
    try:
        previous = _credential_identity(_decrypt(bytes(connection.encrypted_credential)))
    except InvalidToken:
        # Recovery may replace ciphertext that an old vault key can no longer
        # read. The replacement must still decrypt successfully with this key.
        previous = None
    if previous != replacement:
        connection.credential_error_code = None
        connection.credential_error_at = None
        connection.credential_alerted_at = None
    connection.encrypted_credential = encrypted
    connection.is_active = True


def _credential_identity(payload: str) -> tuple[str | None, ...]:
    """Ignore profile and freshness metadata when comparing authentication."""
    if payload.startswith("sk-"):
        return ("api_key", payload)
    try:
        credential = parse_codex_auth_payload(payload)
    except (ValueError, TypeError):
        return (payload,)
    return (
        credential.auth_mode, credential.access_token,
        credential.refresh_token, credential.account_id,
    )


async def _mark_expired(session: AsyncSession, user_id: str, credential_payload: str) -> int | None:
    from brain.systems.vault import _decrypt

    connection = (await session.scalars(
        select(UserCodexConnection)
        .where(UserCodexConnection.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).first()
    if connection is None or not connection.is_active:
        return None
    if _credential_identity(_decrypt(bytes(connection.encrypted_credential))) != _credential_identity(credential_payload):
        return None
    if connection.credential_error_code != CodexCredentialExpiredError.error_code:
        connection.credential_error_code = CodexCredentialExpiredError.error_code
        connection.credential_error_at = datetime.now(timezone.utc)
        connection.credential_alerted_at = None
    await session.flush()
    return int(connection.id)


async def mark_codex_credential_expired(
    *, user_id: str, credential_payload: str, session: AsyncSession | None = None,
) -> bool:
    """Keep the circuit closed even when the caller's failed run rolls back.

    Compare the failed credential under the connection lock: a stale refresh
    failure must never disable a replacement supplied by re-authentication.
    """
    if session is not None and owns_codex_credential_write(session, user_id):
        # A newly supplied credential is still tentative. Its health must commit
        # or roll back with that same credential, rather than deadlock on itself.
        # The next committed read delivers its pending alert.
        return await _mark_expired(session, user_id, credential_payload) is not None
    async with UnitOfWork() as uow:
        connection_id = await _mark_expired(uow.session, user_id, credential_payload)
    if connection_id is not None:
        await retry_codex_credential_alert(connection_id=connection_id)
        return True
    return False


async def retry_codex_credential_alert(*, connection_id: int) -> None:
    """Serialize alert delivery on the same durable connection state.

    The expiry has already committed. Delivery failures leave the alert pending;
    repeats retry delivery, but never retry the revoked OAuth credential.
    """
    try:
        async with UnitOfWork() as uow:
            connection = (await uow.session.scalars(
                select(UserCodexConnection)
                .where(UserCodexConnection.id == connection_id)
                .with_for_update()
            )).first()
            if (
                connection is None
                or connection.credential_error_code != CodexCredentialExpiredError.error_code
                or connection.credential_alerted_at is not None
            ):
                return
            await asyncio.wait_for(
                async_deliver_failure_alert(
                    policy=SlackFailureAlertPolicy(
                        provide_client=slack_web_client_from_runtime,
                        requested_by="provider_credential_failure",
                        reason="Report a personal Codex credential that needs re-authentication.",
                        channel="#alerts",
                        unknown_error_text="credential_expired",
                        client_msg_id=str(uuid5(
                            NAMESPACE_URL,
                            f"codex-credential-expiry:{connection.id}:{assume_utc(connection.credential_error_at).isoformat()}",
                        )),
                    ),
                    subject=FailureAlertSubject(
                        identity_label="Connection",
                        identity=f"OpenAI Codex / ChatGPT #{connection.id}",
                        url_label="Settings",
                        url=f"{public_app_base_url()}/system",
                        link_label="sign in again",
                    ),
                    presentation=FailureAlertPresentation(
                        title="Provider credential expired",
                        summary="This user's Codex lane is blocked until they sign in again.",
                    ),
                    error_text=str(CodexCredentialExpiredError()),
                ),
                timeout=10.0,
            )
            connection.credential_alerted_at = datetime.now(timezone.utc)
    except Exception as exc:
        # Credential and Slack errors can contain secrets. Keep this log bounded
        # to the safe exception class while the durable alert remains pending.
        logger.warning(
            "codex_credential_alert_pending connection_id=%s exception_class=%s",
            connection_id, type(exc).__name__,
        )


async def retry_pending_codex_credential_alerts(session: AsyncSession, *, limit: int = 10) -> None:
    """Retry bounded pending connection episodes without resolving OAuth."""
    connection_ids = (await session.scalars(
        select(UserCodexConnection.id).where(
            UserCodexConnection.is_active.is_(True),
            UserCodexConnection.credential_error_code == CodexCredentialExpiredError.error_code,
            UserCodexConnection.credential_alerted_at.is_(None),
        ).order_by(UserCodexConnection.credential_error_at, UserCodexConnection.id).limit(limit)
    )).all()
    for connection_id in connection_ids:
        # Delivery, row locking, the expiry identity and Slack idempotency remain
        # owned by the same transition. This scan never retries a revoked token.
        await retry_codex_credential_alert(connection_id=int(connection_id))

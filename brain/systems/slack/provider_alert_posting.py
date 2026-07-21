"""Prepare, gate, post, and finalize provider alerts sent to Slack."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import json
import logging
from typing import Any

from brain.platform.provider_alerts import ProviderAlertDecision
from brain.systems.slack.provider_alert_gate import (
    gate_provider_alert_post,
    record_provider_alert_posted,
)


logger = logging.getLogger(__name__)


def _provider_alert_result(decision: ProviderAlertDecision) -> dict[str, Any]:
    return {
        "provider_alert": True,
        "alert_signature": decision.signature,
        "alert_classification": decision.classification,
        "alert_severity": decision.severity,
        "alert_rule_id": decision.rule_id,
        "alert_policy_source": decision.policy_source,
        "alert_escalation_reason": decision.escalation_reason,
    }


def _posted_slack_message_ts(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    direct = str(response.get("ts") or "").strip()
    if direct:
        return direct
    message = response.get("message")
    if isinstance(message, dict):
        nested = str(message.get("ts") or "").strip()
        return nested or None
    return None


async def post_provider_alert(
    client: Any,
    *,
    org_id: str,
    channel_id: str,
    thread_ts: str | None,
    visibility: str,
    illo_user_id: str | None,
    submitted_body: str,
    decision: ProviderAlertDecision,
    uploaded_image: bool,
    resolve_channel: Callable[[str], Awaitable[str]],
    post: Callable[[str], Awaitable[tuple[Any, str | None]]],
    clear_processing_status: Callable[[], Awaitable[None]],
) -> str:
    """Gate one classified alert, post it if allowed, then update its ledger."""

    channel_id = await resolve_channel(channel_id)
    if not org_id:
        return json.dumps(
            {
                "ok": False,
                "posted": False,
                "error": "provider_alert_org_context_required",
                "submitted_chars": len(submitted_body),
                "posted_chars": 0,
                **_provider_alert_result(decision),
            }
        )
    try:
        alert_gate = await gate_provider_alert_post(
            client,
            org_id=org_id,
            channel_id=channel_id,
            illo_user_id=illo_user_id,
            decision=decision,
        )
    except Exception as exc:
        logger.exception("provider alert ledger gate failed")
        return json.dumps(
            {
                "ok": False,
                "posted": False,
                "error": "provider_alert_ledger_unavailable",
                "detail": str(exc),
                "submitted_chars": len(submitted_body),
                "posted_chars": 0,
                **_provider_alert_result(decision),
            }
        )
    if alert_gate.suppress:
        await clear_processing_status()
        return json.dumps(
            {
                "ok": True,
                "posted": False,
                "suppressed": True,
                "reason": alert_gate.reason,
                "delta_line": alert_gate.delta_line,
                "acknowledged_by": alert_gate.acknowledged_by,
                "acknowledged_at": alert_gate.acknowledged_at,
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "visibility": visibility,
                "counts_as_visible_response": True,
                "submitted_chars": len(submitted_body),
                "posted_chars": 0,
                "submitted_bytes": len(submitted_body.encode("utf-8")),
                "posted_bytes": 0,
                "chunk_count": 0,
                "truncated": False,
                **_provider_alert_result(decision),
            }
        )

    channel_id = await resolve_channel(channel_id)
    response, early_result = await post(channel_id)
    if early_result is not None:
        return early_result
    alert_ledger_record_error: str | None = None
    try:
        await record_provider_alert_posted(
            org_id=org_id,
            channel_id=channel_id,
            message_ts=_posted_slack_message_ts(response),
            thread_ts=thread_ts,
            decision=decision,
        )
    except Exception as exc:
        # Slack has already accepted the post. Report the ledger fault without
        # lying about delivery and inviting an immediate retry.
        alert_ledger_record_error = str(exc)
        logger.exception("provider alert post succeeded but ledger finalization failed")
    await clear_processing_status()

    body = decision.body
    submitted_chars = int(response.get("submitted_chars", len(body)))
    posted_chars = int(response.get("posted_chars", submitted_chars))
    return json.dumps(
        {
            "ok": True,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "visibility": visibility,
            "uploaded_image": uploaded_image,
            "submitted_chars": submitted_chars,
            "posted_chars": posted_chars,
            "submitted_bytes": int(response.get("submitted_bytes", len(body.encode("utf-8")))),
            "posted_bytes": int(response.get("posted_bytes", len(body.encode("utf-8")))),
            "chunk_count": int(response.get("chunk_count", 1)),
            "truncated": bool(response.get("truncated", False)),
            "slack": response,
            "posted": True,
            "alert_ledger_record_error": alert_ledger_record_error,
            **_provider_alert_result(decision),
        },
        default=str,
    )


__all__ = ["post_provider_alert"]

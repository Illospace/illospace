"""Prepare, gate, post, and finalize Cycle exception pings sent to Slack."""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from brain.systems.cycles.exception_ping import (
    gate_exception_ping,
    record_exception_ping_posted,
    release_exception_ping_claim,
)


logger = logging.getLogger(__name__)


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _message_ts(response: Any) -> str | None:
    if not isinstance(response, Mapping):
        return None
    direct = _text(response.get("ts"))
    if direct:
        return direct
    message = response.get("message")
    if isinstance(message, Mapping):
        return _text(message.get("ts")) or None
    return None


async def post_exception_ping(
    *,
    cycle_run_id: int,
    run_kind: str,
    payload: Mapping[str, Any],
    channel_id: str,
    thread_ts: str | None,
    visibility: str,
    submitted_body: str,
    body: str,
    uploaded_image: bool,
    resolve_channel,
    post,
    clear_processing_status,
) -> str:
    """Gate one Cycle ping, post it if allowed, then update both ledgers."""

    try:
        channel_id = await resolve_channel(channel_id)
        gate = await gate_exception_ping(
            cycle_run_id=cycle_run_id,
            run_kind=run_kind,
            payload=payload,
        )
    except (TypeError, ValueError) as exc:
        return json.dumps(
            {
                "ok": False,
                "posted": False,
                "error": "invalid_exception_ping",
                "detail": str(exc),
                "submitted_chars": len(submitted_body),
                "posted_chars": 0,
            }
        )
    except Exception as exc:
        logger.exception("exception ping ledger gate failed")
        return json.dumps(
            {
                "ok": False,
                "posted": False,
                "error": "exception_ping_ledger_unavailable",
                "detail": str(exc),
                "submitted_chars": len(submitted_body),
                "posted_chars": 0,
            }
        )

    gate_result = {
        "exception_ping": True,
        "target_teammate_id": gate.request.target_teammate_id,
        "item_ref": gate.request.item_ref,
        "material_change": gate.material.material,
        "matched_change_types": list(gate.material.matched_change_types),
        "ledger_line": gate.ledger_line,
    }
    if gate.suppress:
        await clear_processing_status()
        return json.dumps(
            {
                "ok": True,
                "posted": False,
                "suppressed": True,
                "reason": gate.reason,
                "folded_into_cycle_run_id": gate.previous_cycle_run_id,
                "folded_into_item_ref": gate.previous_item_ref,
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
                **gate_result,
            }
        )

    try:
        response, early_result = await post(channel_id)
        if early_result is not None:
            await release_exception_ping_claim(
                cycle_run_id=cycle_run_id,
                gate=gate,
                reason="slack_post_rejected",
            )
            return early_result
    except Exception:
        await release_exception_ping_claim(
            cycle_run_id=cycle_run_id,
            gate=gate,
            reason="slack_delivery_failed",
        )
        raise

    ledger_record_error = None
    try:
        await record_exception_ping_posted(
            cycle_run_id=cycle_run_id,
            run_kind=run_kind,
            gate=gate,
            channel_id=channel_id,
            message_ts=_message_ts(response),
            thread_ts=thread_ts,
        )
    except Exception as exc:
        ledger_record_error = str(exc)
        logger.exception("exception ping posted but ledger finalization failed")
    await clear_processing_status()

    submitted_chars = int(response.get("submitted_chars", len(body)))
    posted_chars = int(response.get("posted_chars", submitted_chars))
    return json.dumps(
        {
            "ok": True,
            "posted": True,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "visibility": visibility,
            "uploaded_image": uploaded_image,
            "submitted_chars": submitted_chars,
            "posted_chars": posted_chars,
            "submitted_bytes": int(
                response.get("submitted_bytes", len(body.encode("utf-8")))
            ),
            "posted_bytes": int(
                response.get("posted_bytes", len(body.encode("utf-8")))
            ),
            "chunk_count": int(response.get("chunk_count", 1)),
            "truncated": bool(response.get("truncated", False)),
            "slack": response,
            "exception_ping_ledger_record_error": ledger_record_error,
            **gate_result,
        },
        default=str,
    )


__all__ = ["post_exception_ping"]

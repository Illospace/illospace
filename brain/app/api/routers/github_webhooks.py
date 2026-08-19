"""GitHub webhook ingress → the shared inbound pipeline.

GitHub cannot send the bridge-token / ``WebhookEnvelopeCreate`` shape the generic
``/webhooks`` endpoint expects, so this router verifies GitHub's
``X-Hub-Signature-256`` and maps the event into the SAME
``submit_inbound_envelope`` pipeline every other lane uses. The pure translation
(signature verify + event→envelope) lives in
``brain/systems/inbound/github_webhook.py`` and is unit-tested; this file is the
thin HTTP + connection wiring.

Config (env):
  ``ILLO_GITHUB_WEBHOOK_SECRET`` — the App/webhook secret for signature verify.
  ``ILLO_GITHUB_CONNECTION_ID``  — the ``external_agent_connections`` row GitHub
                                   events are attributed to. Its owner is the
                                   ingestion authority; Slice 3 assignment then
                                   re-routes ownership (business/product → Reda).

The router is mounted unconditionally in ``brain/app/api/main.py`` and
self-gates instead of gating registration: a missing secret answers 503
outright; with a secret set, requests are still verified and parsed first
(401 bad signature, 400 malformed JSON, 202 unsupported event) and a missing
connection id answers 503 before any envelope is submitted. An unconfigured
deployment therefore never ingests a GitHub event.
"""

from __future__ import annotations

import json
import os
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.deps import get_db, rate_limit
from brain.app.api.routers.external_agent_errors import raise_external_agent_http_error
from brain.platform.db.models.external_agent import ExternalAgentConnectionRow
from brain.systems.inbound import service as inbound
from brain.systems.inbound.github_webhook import (
    github_event_to_envelope,
    verify_signature,
)

router = APIRouter(tags=["webhooks", "github"], dependencies=[Depends(rate_limit)])


def _webhook_secret() -> str:
    return os.environ.get("ILLO_GITHUB_WEBHOOK_SECRET", "").strip()


def _connection_id() -> str:
    return os.environ.get("ILLO_GITHUB_CONNECTION_ID", "").strip()


async def _load_github_connection(db: AsyncSession) -> ExternalAgentConnectionRow:
    conn_id = _connection_id()
    if not conn_id:
        raise HTTPException(status_code=503, detail="ILLO_GITHUB_CONNECTION_ID is not configured")
    row = (
        await db.execute(
            select(ExternalAgentConnectionRow).where(ExternalAgentConnectionRow.id == conn_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=503, detail="Configured GitHub connection not found")
    return row


@router.post("/webhooks/github", status_code=202)
async def receive_github_webhook(
    request: Request,
    x_github_event: str | None = Header(default=None, alias="X-GitHub-Event"),
    x_github_delivery: str | None = Header(default=None, alias="X-GitHub-Delivery"),
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    secret = _webhook_secret()
    if not secret:
        raise HTTPException(status_code=503, detail="ILLO_GITHUB_WEBHOOK_SECRET is not configured")

    raw = await request.body()
    if not verify_signature(secret, raw, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid GitHub signature")

    try:
        payload = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Malformed JSON payload")

    envelope = github_event_to_envelope(
        str(x_github_event or ""), payload, delivery_id=x_github_delivery
    )
    if envelope is None:
        # Unsupported event — acknowledge without creating work.
        return {"status": "ignored", "event": x_github_event}

    try:
        connection = await _load_github_connection(db)
        return await inbound.submit_inbound_envelope(
            db,
            connection=connection,
            envelope=envelope,
            ingress_context={
                "surface": "github_webhook",
                "path": str(request.url.path),
                "method": request.method,
                "event": x_github_event,
                "delivery": x_github_delivery,
            },
        )
    except HTTPException:
        raise
    except Exception as exc:  # normalize to the shared external-agent error shape
        raise_external_agent_http_error(exc)

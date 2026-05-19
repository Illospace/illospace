"""Public webhook ingress for inbound source signals."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.deps import get_db, rate_limit
from brain.app.api.routers.agent_bridge import _bearer_token
from brain.app.api.routers.external_agent_errors import raise_external_agent_http_error
from brain.app.api.schemas.webhooks import WebhookEnvelopeCreate
from brain.systems.external_agents import service as external_agents
from brain.systems.inbound import service as inbound


router = APIRouter(tags=["webhooks"], dependencies=[Depends(rate_limit)])


@router.post("/webhooks", status_code=202)
async def receive_webhook(
    body: WebhookEnvelopeCreate,
    request: Request,
    x_illo_bridge_token: str | None = Header(default=None, alias="X-Illo-Bridge-Token"),
    x_illo_idempotency_key: str | None = Header(default=None, alias="X-Illo-Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    token = _bearer_token(request, x_illo_bridge_token)
    try:
        principal = await external_agents.authenticate_bridge_token(
            db,
            token,
            required_scope=external_agents.SCOPE_SIGNAL_SUBMIT,
        )
        envelope = body.model_dump()
        header_idempotency_key = _clean_optional(x_illo_idempotency_key)
        if header_idempotency_key and len(header_idempotency_key) > 160:
            raise HTTPException(
                status_code=422,
                detail="X-Illo-Idempotency-Key must be 160 characters or fewer",
            )
        envelope["idempotency_key"] = envelope.get("idempotency_key") or header_idempotency_key
        return await inbound.submit_inbound_envelope(
            db,
            connection=principal,
            envelope=envelope,
            ingress_context={
                "surface": "webhook",
                "path": str(request.url.path),
                "method": request.method,
                "client_host": request.client.host if request.client else None,
            },
        )
    except Exception as exc:
        raise_external_agent_http_error(exc)


def _clean_optional(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None

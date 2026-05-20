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

PROVIDER_DELIVERY_ID_HEADERS = (
    "x-atlassian-webhook-identifier",
    "x-atlassian-webhook-id",
    "x-github-delivery",
    "x-linear-delivery",
    "x-webhook-delivery",
    "x-webhook-id",
)


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
        provider_delivery = _provider_delivery_id(request)
        if header_idempotency_key:
            _validate_idempotency_key(
                header_idempotency_key,
                field_name="X-Illo-Idempotency-Key",
            )
        provider_idempotency_key = _provider_idempotency_key(provider_delivery)
        envelope["idempotency_key"] = envelope.get("idempotency_key") or header_idempotency_key or provider_idempotency_key
        return await inbound.submit_inbound_envelope(
            db,
            connection=principal,
            envelope=envelope,
            ingress_context={
                "surface": "webhook",
                "path": str(request.url.path),
                "method": request.method,
                "client_host": request.client.host if request.client else None,
                "provider_delivery": provider_delivery,
            },
        )
    except Exception as exc:
        raise_external_agent_http_error(exc)


def _clean_optional(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _provider_delivery_id(request: Request) -> dict[str, str] | None:
    for header in PROVIDER_DELIVERY_ID_HEADERS:
        value = _clean_optional(request.headers.get(header))
        if value:
            return {"header": header, "value": value}
    return None


def _provider_idempotency_key(provider_delivery: dict[str, str] | None) -> str | None:
    if not provider_delivery:
        return None
    key = f"{provider_delivery['header']}:{provider_delivery['value']}"
    _validate_idempotency_key(key, field_name=provider_delivery["header"])
    return key


def _validate_idempotency_key(value: str, *, field_name: str) -> None:
    if len(value) > 160:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must produce an idempotency key of 160 characters or fewer",
        )

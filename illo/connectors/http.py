"""Bound all outside responses. Retry only rate limits and temporary failures."""

import asyncio
import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar

import httpx

_private_request = ContextVar("private_http_request", default=False)


class _PrivateRequestLog(logging.Filter):
    def filter(self, record):
        if _private_request.get():
            # A request address can contain a secret in its query.
            record.msg = "The private HTTP request completed."
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True


for _name in (
    "httpx",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.connection",
    "slack_sdk.web.async_base_client",
    "slack_sdk.web.base_client",
):
    logging.getLogger(_name).addFilter(_PrivateRequestLog())
logging.getLogger("mcp.client.streamable_http").addFilter(_PrivateRequestLog())


@contextmanager
def private_http_logs():
    token = _private_request.set(True)
    try:
        yield
    finally:
        _private_request.reset(token)


class HTTPFailure(ValueError):
    def __init__(self, error=None, *, status=None, headers=None):
        super().__init__("The connector request failed")
        self.error = error
        self.status = status
        self.headers = headers or {}


class BoundedHTTP:
    def __init__(self, transport=None, *, public=False):
        self.transport = transport
        self.public = public

    async def request(self, method, url, *, max_bytes=262144, **kwargs):
        from illo.connectors.addresses import PublicTransport, public_address

        if self.public:
            public_address(url)
        transport = PublicTransport(self.transport) if self.public else self.transport
        with private_http_logs():
            async with asyncio.timeout(20):
                return await self._request(method, url, transport, max_bytes=max_bytes, **kwargs)

    async def _request(self, method, url, transport, *, max_bytes, **kwargs):
        async with httpx.AsyncClient(
            transport=transport, timeout=20, trust_env=False, follow_redirects=False
        ) as client:
            for attempt in range(3):
                async with client.stream(method, url, **kwargs) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt < 2:
                            await asyncio.sleep(0.2 * 2**attempt)
                            continue
                    if 300 <= response.status_code < 400:
                        raise ValueError("The connector redirect is not allowed")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > max_bytes:
                            raise ValueError("The connector response is too large")
                    if response.status_code >= 400:
                        # Keep only a known error name. Never expose the provider's text.
                        try:
                            error = json.loads(data).get("error")
                        except (ValueError, AttributeError):
                            error = None
                        raise HTTPFailure(
                            error
                            if isinstance(error, str)
                            and error in {"invalid_client", "invalid_grant"}
                            else None,
                            status=response.status_code,
                            headers=dict(response.headers),
                        )
                    if not data:
                        return {}
                    if "json" in response.headers.get("content-type", ""):
                        return json.loads(data)
                    return data.decode("utf-8", errors="replace")
        raise ValueError("The connector request failed")


def marked(source, data):
    # The structured boundary cannot be closed by text inside the remote result.
    return {"type": "outside_data", "source": source, "trusted": False, "data": data}

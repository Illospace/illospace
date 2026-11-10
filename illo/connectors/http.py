"""Bound all outside responses. Retry only rate limits and temporary failures."""

import asyncio
import json

import httpx


class BoundedHTTP:
    def __init__(self, transport=None):
        self.transport = transport

    async def request(self, method, url, *, max_bytes=262144, **kwargs):
        async with httpx.AsyncClient(
            transport=self.transport, timeout=20, trust_env=False, follow_redirects=False
        ) as client:
            for attempt in range(3):
                async with client.stream(method, url, **kwargs) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt < 2:
                            await asyncio.sleep(0.2 * 2**attempt)
                            continue
                    if response.status_code >= 400:
                        raise ValueError("The connector request failed")
                    if response.status_code >= 300:
                        raise ValueError("The connector redirect is not allowed")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > max_bytes:
                            raise ValueError("The connector response is too large")
                    if not data:
                        return {}
                    if "json" in response.headers.get("content-type", ""):
                        return json.loads(data)
                    return data.decode("utf-8", errors="replace")
        raise ValueError("The connector request failed")


def marked(source, data):
    # The structured boundary cannot be closed by text inside the remote result.
    return {"type": "outside_data", "source": source, "trusted": False, "data": data}

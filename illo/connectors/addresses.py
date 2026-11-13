"""Check and pin public connector addresses before each request."""

import ipaddress

import httpx

from illo.connectors.config import validate_url
from illo.tools.web import normalize_url, resolve_public


def public_address(value):
    validate_url(value)
    url = normalize_url(value)
    if url.host.lower().rstrip(".") == "localhost":
        raise ValueError("The host is not public")
    try:
        ip = ipaddress.ip_address(url.host)
    except ValueError:
        return url
    if (
        not ip.is_global
        or ip.is_multicast
        or (isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped)
    ):
        raise ValueError("The host is not public")
    return url


class PublicTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner=None):
        self.inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        url = public_address(str(request.url))
        addresses = await resolve_public(url.host, url.port or 443)
        headers = request.headers.copy()
        headers["Host"] = url.netloc.decode()
        pinned = httpx.Request(
            request.method,
            url.copy_with(host=addresses[0]),
            headers=headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": url.host},
        )
        response = await self.inner.handle_async_request(pinned)
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise ValueError("The connector redirect is not allowed")
        return response

    async def aclose(self):
        await self.inner.aclose()

"""Fetch public text through checked addresses. Never use a proxy or cookies."""

import asyncio
from urllib.parse import urljoin

import httpx

from illo.tools._format import data_text
from illo.tools.web import MAX_BYTES, USER_AGENT, PageText, normalize_url, resolve_public

MAX_TEXT = 20_000
SERVICE_NAMES = frozenset(
    {
        "gateway",
        "illo",
        "brain",
        "sandbox",
        "db",
        "postgres",
        "redis",
        "host.docker.internal",
        "metadata.google.internal",
    }
)
TEXT_TYPES = {
    "application/json",
    "application/xml",
    "application/rss+xml",
    "application/atom+xml",
    "application/xhtml+xml",
}


class LinkedText(PageText):
    def __init__(self, base):
        super().__init__()
        self.base = base
        self.links = []

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if tag == "a" and not self.hidden:
            self.links.append(dict(attrs).get("href", ""))

    def handle_endtag(self, tag):
        if tag == "a" and self.links and not self.hidden:
            link = urljoin(self.base, self.links.pop())
            if link.startswith(("https://", "http://")):
                self.parts.append(f" ({link})")
        super().handle_endtag(tag)


MAX_URL = 400


def public_url(value, *, blocked=()):
    # A deep run reads private repositories and then the web. A long address can carry
    # that text to an outside server, so the address stays short.
    if len(str(value)) > MAX_URL:
        raise ValueError("The address is too long")
    url = normalize_url(value, allow_http=True)
    host = url.host.lower().rstrip(".")
    if host in SERVICE_NAMES | set(blocked) or "." not in host and ":" not in host:
        raise ValueError("The host is not public")
    return url


async def fetch(value, *, blocked=(), transport=None):
    url = public_url(value, blocked=blocked)
    async with (
        asyncio.timeout(15),
        httpx.AsyncClient(
            timeout=15, trust_env=False, follow_redirects=False, transport=transport
        ) as client,
    ):
        for redirect in range(4):
            addresses = await resolve_public(
                url.host, url.port or (443 if url.scheme == "https" else 80)
            )
            # Each redirect has a fresh host check and an empty cookie jar.
            client.cookies.clear()
            async with client.stream(
                "GET",
                url.copy_with(host=addresses[0]),
                headers={
                    "Host": url.netloc.decode(),
                    "User-Agent": USER_AGENT,
                    "Accept": "text/*, application/json, application/xml, application/rss+xml, application/atom+xml",
                    "Accept-Encoding": "identity",
                },
                extensions={"sni_hostname": url.host},
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if redirect == 3 or not response.headers.get("location"):
                        raise ValueError("The page has too many redirects")
                    url = public_url(
                        urljoin(str(url), response.headers["location"]), blocked=blocked
                    )
                    continue
                response.raise_for_status()
                kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if not (
                    kind.startswith("text/")
                    or kind in TEXT_TYPES
                    or kind.endswith(("+json", "+xml"))
                ):
                    raise ValueError("The page is not text")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("The page uses an unsupported encoding")
                if int(response.headers.get("content-length", "0")) > MAX_BYTES:
                    raise ValueError("The page is too large")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_BYTES:
                        raise ValueError("The page is too large")
                    body.extend(chunk)
                text = body.decode(response.encoding or "utf-8", errors="replace")
                if kind in {"text/html", "application/xhtml+xml"}:
                    parser = LinkedText(str(url))
                    parser.feed(text)
                    text = parser.text()
                if len(text) > MAX_TEXT:
                    text = text[:MAX_TEXT] + "\n[Page text cut at 20,000 characters.]"
                return data_text("web", text)
    raise ValueError("The page could not be read")

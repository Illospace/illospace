"""Read one public page without a route to private hosts or private data."""

import asyncio
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import Field

from illo.db import one
from illo.tools._format import data_text
from illo.tools.registry import Arguments, Refusal, Tool

MAX_BYTES = 2 * 1024 * 1024
MAX_TEXT = 15000
USER_AGENT = "Illo/0.1 (public page reader)"
TRANSLATED_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("64:ff9b::/96", "64:ff9b:1::/48", "::/96")
)


class PageText(HTMLParser):
    """Keep text. Ignore scripts, styles, and navigation."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.title = []
        self.hidden = 0
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "nav", "svg", "template"}:
            self.hidden += 1
        if tag == "title":
            self.in_title = True
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "article", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "nav", "svg", "template"}:
            self.hidden = max(0, self.hidden - 1)
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if not self.hidden:
            (self.title if self.in_title else self.parts).append(data)

    def text(self):
        body = "\n".join(" ".join(line.split()) for line in "".join(self.parts).splitlines())
        return (" ".join(self.title) + "\n" + body).strip()



class PageArguments(Arguments):
    url: str = Field(min_length=1, max_length=4096)


def normalize_url(value: str, allow_http: bool = False) -> httpx.URL:
    """Reject ambiguous authority syntax before DNS or HTTP."""
    if any(ord(c) < 33 or c == "\\" for c in value):
        raise ValueError("The URL contains an invalid character")
    url = httpx.URL(value)
    if url.scheme not in ({"https", "http"} if allow_http else {"https"}):
        raise ValueError("The URL scheme is not allowed")
    if not url.host or url.username or url.password or "@" in urlsplit(value).netloc:
        raise ValueError("The URL must have a host and no credentials")
    return url.copy_with(fragment=None)


def inbound_urls(text: str, allow_http: bool) -> list[httpx.URL]:
    urls = []
    for value in re.findall(r'https?://[^\s<>"\']+', text):
        try:
            urls.append(normalize_url(value.rstrip(".,;!?)]"), allow_http))
        except (ValueError, httpx.InvalidURL):
            continue
    return urls


def allowed_url(url: httpx.URL, text: str, allow_http: bool) -> bool:
    """Only an inbound URL or the root of its host can leave this run."""
    for source in inbound_urls(text, allow_http):
        if url == source:
            return True
        if (
            url.scheme == source.scheme
            and url.host == source.host
            and url.port == source.port
            and url.raw_path == b"/"
        ):
            return True
    return False


async def resolve_public(host: str, port: int) -> list[str]:
    """Check every DNS answer. A mixed public/private answer fails closed."""
    records = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(record[4][0] for record in records))
    if not addresses:
        raise ValueError("The host has no address")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global or ip.is_multicast or (
            isinstance(ip, ipaddress.IPv6Address)
            and (ip.ipv4_mapped or any(ip in network for network in TRANSLATED_NETWORKS))
        ):
            raise ValueError("The host is not public")
    return addresses


async def fetch_page(url: httpx.URL, *, allow_http: bool = False) -> str:
    """Pin the checked IP. Keep the original host for TLS and HTTP."""
    async with asyncio.timeout(10):
        async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
            for redirect in range(4):
                addresses = await resolve_public(
                    url.host, url.port or (443 if url.scheme == "https" else 80)
                )
                target = url.copy_with(host=addresses[0])
                async with client.stream(
                    "GET",
                    target,
                    headers={
                        "Host": url.netloc.decode(),
                        "User-Agent": USER_AGENT,
                        "Accept": "text/html, text/plain",
                        "Accept-Encoding": "identity",
                    },
                    extensions={"sni_hostname": url.host},
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if redirect == 3 or not response.headers.get("location"):
                            raise ValueError("The page has too many redirects")
                        url = normalize_url(
                            urljoin(str(url), response.headers["location"]), allow_http
                        )
                        continue
                    response.raise_for_status()
                    kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    if kind not in {"text/html", "text/plain"}:
                        raise ValueError("The page is not text")
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise ValueError("The page uses an unsupported encoding")
                    if int(response.headers.get("content-length", "0")) > MAX_BYTES:
                        raise ValueError("The page is too large")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            raise ValueError("The page is too large")
                    text = bytes(body).decode(response.encoding or "utf-8", errors="replace")
                    if kind == "text/html":
                        parser = PageText()
                        parser.feed(text)
                        text = parser.text()
                    return data_text("web", text[:MAX_TEXT])
    raise ValueError("The page could not be read")


async def read_page(ctx, args):
    if ctx.run.get("customer_read"):
        raise Refusal("This run read customer data. The web is closed for this run.")
    if ctx.run["personal_read"]:
        raise ValueError("A run with personal data cannot read a public page")
    allow_http = ctx.app.config.web_allow_http
    url = normalize_url(args["url"], allow_http)
    event = await one(ctx.conn, "SELECT payload FROM events WHERE id=%s", (ctx.run["event_id"],))
    if not event or not allowed_url(url, event["payload"].get("text", ""), allow_http):
        raise ValueError("The URL must come from the inbound event")
    if ctx.run["tainted"]:
        sources = inbound_urls(event["payload"].get("text", ""), allow_http)
        if not sources or url != sources[0]:
            raise ValueError("A tainted run can read only the first inbound URL")
        previous = await one(
            ctx.conn,
            "SELECT id FROM audit WHERE run_id=%s AND tool='web_read_public_page' LIMIT 1",
            (ctx.run["id"],),
        )
        if previous:
            raise ValueError("A tainted run can read a public page only once")
    return {"text": await fetch_page(url, allow_http=allow_http)}


def register(registry) -> None:
    registry.add(
        Tool(
            name="web_read_public_page",
            family="web",
            risk="read",
            destination=lambda _: "ledger",
            personal=False,
            handler=read_page,
            arguments=PageArguments,
            description='Read one public web page whose URL is in the run message, or the root page of that host. In a run from outside text, only the first URL of the message, once. Refused after this run read personal data.',
            # Host checks and the inbound URL bound are safe under hostile text.
            profiles=frozenset(
                {"person_dm", "team_channel", "tainted", "routine", "agent_request", "outsider"}
            ),
        )
    )

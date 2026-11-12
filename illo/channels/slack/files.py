"""Download bounded files from Slack without redirects."""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

_private_io = ContextVar("slack_private_io", default=False)


class _PrivateIOFilter(logging.Filter):
    def filter(self, record):
        return not _private_io.get()


# The clients log private URLs and Slack response bodies at INFO or DEBUG.
# A task-local filter keeps concurrent, unrelated requests visible.
for _name in (
    "httpx",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
    "slack_sdk.web.async_base_client",
):
    logging.getLogger(_name).addFilter(_PrivateIOFilter())


@contextmanager
def private_io():
    marker = _private_io.set(True)
    try:
        yield
    finally:
        _private_io.reset(marker)


async def slack_call(method, **kwargs):
    with private_io():
        return await method(**kwargs)


async def download(client, url: str, token, max_bytes: int) -> bytes | None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "files.slack.com"
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
        or not token
    ):
        return None
    with private_io():
        async with client.stream(
            "GET",
            url,
            headers={
                "Authorization": f"Bearer {token.get_secret_value()}",
                "Accept-Encoding": "identity",
            },
        ) as response:
            if response.status_code != 200:
                return None
            if int(response.headers.get("content-length", 0)) > max_bytes:
                return None
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                body.extend(chunk)
                if len(body) > max_bytes:
                    return None
        return bytes(body) or None

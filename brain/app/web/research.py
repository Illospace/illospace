"""Lightweight web research runtime for Illo."""
from __future__ import annotations

import html
import inspect
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from brain.platform.async_io import async_http_client, run_blocking

logger = logging.getLogger(__name__)

_SEARCH_TTL_SEC = int(os.environ.get("ILLO_WEB_SEARCH_CACHE_TTL_SEC", "600"))
_FETCH_TTL_SEC = int(os.environ.get("ILLO_WEB_FETCH_CACHE_TTL_SEC", "900"))
_MAX_FETCH_BYTES = int(os.environ.get("ILLO_WEB_FETCH_MAX_BYTES", "1000000"))
_DEFAULT_TIMEOUT = float(os.environ.get("ILLO_WEB_TIMEOUT_SEC", "15"))


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


_cache_lock = threading.Lock()
_search_cache: dict[str, _CacheEntry] = {}
_fetch_cache: dict[str, _CacheEntry] = {}


class WebResearchError(RuntimeError):
    """Raised when web research fails safely."""


async def _search_api_key(key_name: str, runtime_secret_context: Any = None) -> str:
    from brain.systems.vault.runtime_secrets import (
        RuntimeSecretContext,
        RuntimeSecretUnavailable,
        read_runtime_secret,
    )

    try:
        return await read_runtime_secret(
            key_name,
            context=runtime_secret_context or RuntimeSecretContext(actor_user_id=None, org_id=None),
            reason="Run Illo's web search tool through a configured search provider.",
            requested_by="web_search",
            access="service",
            allow_env_fallback=True,
        )
    except RuntimeSecretUnavailable as exc:
        raise WebResearchError(f"{key_name} is not configured") from exc


def _cache_get(cache: dict[str, _CacheEntry], key: str):
    now = time.time()
    with _cache_lock:
        entry = cache.get(key)
        if not entry:
            return None
        if entry.expires_at <= now:
            cache.pop(key, None)
            return None
        return entry.value


def _cache_put(cache: dict[str, _CacheEntry], key: str, value: Any, ttl_sec: int) -> None:
    with _cache_lock:
        cache[key] = _CacheEntry(value=value, expires_at=time.time() + ttl_sec)


def _normalize_url(url: str) -> str:
    value = (url or "").strip()
    if not value:
        raise WebResearchError("URL is required")
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise WebResearchError(f"Unsupported URL scheme: {parsed.scheme}")
    if not parsed.hostname:
        raise WebResearchError("URL must include a hostname")
    return value


def _is_private_host(hostname: str) -> bool:
    lowered = hostname.lower()
    if lowered in {"localhost", "localhost.localdomain"}:
        return True
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        raise WebResearchError(f"Could not resolve host: {hostname}")
    for info in infos:
        ip = info[4][0]
        addr = ipaddress.ip_address(ip)
        if any([
            addr.is_private,
            addr.is_loopback,
            addr.is_link_local,
            addr.is_multicast,
            addr.is_reserved,
            addr.is_unspecified,
        ]):
            return True
    return False


def _assert_safe_url(url: str) -> str:
    normalized = _normalize_url(url)
    parsed = urlparse(normalized)
    if _is_private_host(parsed.hostname or ""):
        raise WebResearchError(f"Blocked private or local host: {parsed.hostname}")
    return normalized


def _http_client() -> httpx.AsyncClient:
    return async_http_client(
        timeout=httpx.Timeout(_DEFAULT_TIMEOUT, connect=_DEFAULT_TIMEOUT),
        headers={
            "User-Agent": "illo-brain/1.0",
            "Accept-Language": "en-US,en;q=0.8",
        },
        follow_redirects=True,
    )


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _accepts_runtime_secret_context(fn) -> bool:
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    positional = 0
    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
            return True
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            return True
        if parameter.name == "runtime_secret_context":
            return True
        if parameter.kind in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }:
            positional += 1
    return positional >= 3


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</(p|div|section|article|h\d|li|tr)>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_readable_text(page_html: str) -> tuple[str, str | None]:
    title = None
    title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", page_html)
    if title_match:
        title = _strip_html(title_match.group(1))

    try:
        from readability import Document

        doc = Document(page_html)
        summary_html = doc.summary(html_partial=True)
        title = _strip_html(doc.short_title() or title or "")
        text = _strip_html(summary_html)
        if text:
            return text, title or None
    except Exception as e:
        logger.debug("readability extraction failed: %s", e)

    body_match = re.search(r"(?is)<body[^>]*>(.*?)</body>", page_html)
    body_html = body_match.group(1) if body_match else page_html
    return _strip_html(body_html), title


def _markdown_from_text(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n\n".join(lines)


async def _brave_search(query: str, limit: int, runtime_secret_context: Any = None) -> dict[str, Any]:
    api_key = await _search_api_key("BRAVE_SEARCH_API_KEY", runtime_secret_context)
    async with _http_client() as client:
        response = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": min(limit, 20)},
            headers={"X-Subscription-Token": api_key},
        )
        response.raise_for_status()
        payload = response.json()
    results = []
    for item in (payload.get("web") or {}).get("results", [])[:limit]:
        results.append({
            "title": item.get("title"),
            "url": item.get("url"),
            "snippet": item.get("description") or item.get("snippet"),
            "source": "brave",
        })
    return {"provider": "brave", "results": results}


async def _tavily_search(query: str, limit: int, runtime_secret_context: Any = None) -> dict[str, Any]:
    api_key = await _search_api_key("TAVILY_API_KEY", runtime_secret_context)
    async with _http_client() as client:
        response = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "max_results": min(limit, 10),
                "include_answer": False,
                "search_depth": "basic",
            },
        )
        response.raise_for_status()
        payload = response.json()
    results = []
    for item in payload.get("results", [])[:limit]:
        results.append({
            "title": item.get("title"),
            "url": item.get("url"),
            "snippet": item.get("content"),
            "source": "tavily",
        })
    return {"provider": "tavily", "results": results}


async def _duckduckgo_lite_search(query: str, limit: int) -> dict[str, Any]:
    async with _http_client() as client:
        response = await client.get(
            "https://lite.duckduckgo.com/lite/",
            params={"q": query},
        )
        response.raise_for_status()
        content = response.text

    anchor_pattern = re.compile(
        r'<a[^>]+href="(?P<url>[^"]+)"[^>]*class="[^"]*result-link[^"]*"[^>]*>(?P<title>.*?)</a>',
        re.I | re.S,
    )
    matches = [(match.group("url"), match.group("title")) for match in anchor_pattern.finditer(content)]
    results = []
    if not matches:
        # Fallback: lite markup is simple but not stable. Use generic anchors as a last resort.
        generic = re.compile(r'<a rel="nofollow" href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>', re.I | re.S)
        matches = [(match.group("url"), match.group("title")) for match in generic.finditer(content)]
    if not matches:
        flexible = re.compile(r"<a(?P<attrs>[^>]*)>(?P<title>.*?)</a>", re.I | re.S)
        for match in flexible.finditer(content):
            attrs = match.group("attrs")
            if "result-link" not in attrs and 'rel="nofollow"' not in attrs:
                continue
            href = re.search(r'href=["\'](?P<url>[^"\']+)["\']', attrs, re.I)
            if href:
                matches.append((href.group("url"), match.group("title")))
    for raw_url, raw_title in matches:
        url = _normalize_duckduckgo_result_url(raw_url)
        title = _strip_html(raw_title)
        if not url:
            continue
        results.append({
            "title": title,
            "url": url,
            "snippet": None,
            "source": "duckduckgo-lite",
        })
        if len(results) >= limit:
            break
    return {"provider": "duckduckgo-lite", "results": results}


def _normalize_duckduckgo_result_url(value: str) -> str | None:
    url = html.unescape(value or "").strip()
    if not url:
        return None
    if url.startswith("//"):
        url = f"https:{url}"
    elif url.startswith("/"):
        url = f"https://duckduckgo.com{url}"

    parsed = urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        uddg = parse_qs(parsed.query).get("uddg", [])
        if uddg:
            url = unquote(uddg[0])

    return url if url.startswith(("http://", "https://")) else None


async def web_search(
    query: str,
    *,
    provider: str | None = None,
    limit: int = 5,
    runtime_secret_context: Any = None,
) -> dict[str, Any]:
    normalized_query = (query or "").strip()
    if not normalized_query:
        raise WebResearchError("Query is required")
    limit = max(1, min(limit, 10))
    provider_name = (provider or os.environ.get("ILLO_WEB_SEARCH_PROVIDER", "")).strip().lower() or "auto"
    cache_key = json.dumps({"q": normalized_query, "provider": provider_name, "limit": limit}, sort_keys=True)
    cached = _cache_get(_search_cache, cache_key)
    if cached is not None:
        return {**cached, "cached": True}

    providers = {
        "brave": _brave_search,
        "tavily": _tavily_search,
        "duckduckgo": _duckduckgo_lite_search,
        "duckduckgo-lite": _duckduckgo_lite_search,
    }
    order = ["brave", "tavily", "duckduckgo-lite"] if provider_name == "auto" else [provider_name]

    auto_mode = provider_name == "auto"
    provider_errors: list[dict[str, str]] = []
    for name in order:
        fn = providers.get(name)
        if not fn:
            provider_errors.append({"provider": name, "error": f"Unsupported search provider: {name}"})
            continue
        try:
            if _accepts_runtime_secret_context(fn):
                result = await _maybe_await(fn(normalized_query, limit, runtime_secret_context))
            else:
                result = await _maybe_await(fn(normalized_query, limit))
            results = list(result.get("results") or [])
            if auto_mode and not results:
                provider_errors.append({"provider": result.get("provider", name), "error": "No results returned"})
                continue
            payload = {
                "query": normalized_query,
                "provider": result["provider"],
                "results": results,
                "count": len(results),
                "cached": False,
            }
            if provider_errors:
                payload["provider_errors"] = provider_errors
            if results:
                _cache_put(_search_cache, cache_key, payload, _SEARCH_TTL_SEC)
            return payload
        except Exception as e:
            logger.debug("web search provider failed provider=%s: %s", name, e)
            provider_errors.append({"provider": name, "error": str(e)})
            continue
    detail = "; ".join(f"{item['provider']}: {item['error']}" for item in provider_errors if item.get("error"))
    raise WebResearchError(detail or "No search provider returned results")


async def web_fetch(url: str, *, max_chars: int = 12000, extract_mode: str = "markdown") -> dict[str, Any]:
    safe_url = await run_blocking(_assert_safe_url, url)
    extract_mode = (extract_mode or "markdown").strip().lower()
    if extract_mode not in {"markdown", "text", "html"}:
        raise WebResearchError(f"Unsupported extract_mode: {extract_mode}")
    max_chars = max(500, min(max_chars, 50000))
    cache_key = json.dumps({"url": safe_url, "max_chars": max_chars, "mode": extract_mode}, sort_keys=True)
    cached = _cache_get(_fetch_cache, cache_key)
    if cached is not None:
        return {**cached, "cached": True}

    async with _http_client() as client:
        response = await client.get(safe_url)
        response.raise_for_status()
        raw = response.content[:_MAX_FETCH_BYTES]
        content_type = response.headers.get("content-type", "")
        final_url = str(response.url)

    if "html" in content_type or raw.lstrip().startswith(b"<"):
        decoded = raw.decode("utf-8", errors="replace")
        readable_text, title = _extract_readable_text(decoded)
        if extract_mode == "html":
            content = decoded[:max_chars]
        elif extract_mode == "text":
            content = readable_text[:max_chars]
        else:
            content = _markdown_from_text(readable_text)[:max_chars]
    else:
        decoded = raw.decode("utf-8", errors="replace")
        title = None
        content = decoded[:max_chars]

    payload = {
        "url": safe_url,
        "final_url": final_url,
        "title": title,
        "content": content,
        "content_type": content_type or None,
        "extract_mode": extract_mode,
        "truncated": len(content) >= max_chars,
        "cached": False,
    }
    _cache_put(_fetch_cache, cache_key, payload, _FETCH_TTL_SEC)
    return payload

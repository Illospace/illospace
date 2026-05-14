from __future__ import annotations

from types import SimpleNamespace

import pytest


class _FakeResponse:
    def __init__(self, *, text="", content=b"", headers=None, url="https://example.com", json_data=None):
        self.text = text
        self.content = content or text.encode("utf-8")
        self.headers = headers or {"content-type": "text/html; charset=utf-8"}
        self.url = url
        self._json_data = json_data

    def raise_for_status(self):
        return None

    def json(self):
        return self._json_data


class _FakeClient:
    def __init__(self, response: _FakeResponse):
        self.response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return self.response

    async def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return self.response


@pytest.mark.asyncio
async def test_web_fetch_blocks_private_hosts(monkeypatch):
    from brain.app.web.research import WebResearchError, web_fetch

    monkeypatch.setattr("brain.app.web.research.socket.getaddrinfo", lambda host, port: [(None, None, None, None, ("127.0.0.1", 0))])

    with pytest.raises(WebResearchError, match="Blocked private or local host"):
        await web_fetch("http://localhost:8000")


@pytest.mark.asyncio
async def test_web_fetch_extracts_readable_text(monkeypatch):
    from brain.app.web.research import web_fetch

    html = """
    <html>
      <head><title>Test Article</title></head>
      <body>
        <article>
          <h1>Headline</h1>
          <p>First paragraph.</p>
          <p>Second paragraph.</p>
        </article>
      </body>
    </html>
    """
    monkeypatch.setattr("brain.app.web.research._assert_safe_url", lambda url: url)
    monkeypatch.setattr("brain.app.web.research._http_client", lambda: _FakeClient(_FakeResponse(text=html)))

    result = await web_fetch("https://example.com/post", extract_mode="text", max_chars=5000)

    assert result["title"] in {"Test Article", "Headline", "Test Article Headline"}
    assert "First paragraph." in result["content"]
    assert result["final_url"] == "https://example.com"


@pytest.mark.asyncio
async def test_web_search_uses_provider_and_caches(monkeypatch):
    from brain.app.web.research import _search_cache, web_search

    _search_cache.clear()

    payload = {
        "web": {
            "results": [
                {"title": "Result A", "url": "https://a.test", "description": "Snippet A"},
                {"title": "Result B", "url": "https://b.test", "description": "Snippet B"},
            ]
        }
    }
    client = _FakeClient(_FakeResponse(json_data=payload, headers={"content-type": "application/json"}))
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key")
    monkeypatch.setattr("brain.app.web.research._http_client", lambda: client)

    first = await web_search("illo brain", provider="brave", limit=2)
    second = await web_search("illo brain", provider="brave", limit=2)

    assert first["provider"] == "brave"
    assert first["results"][0]["title"] == "Result A"
    assert first["cached"] is False
    assert second["cached"] is True
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_web_search_auto_skips_empty_provider_without_caching(monkeypatch):
    from brain.app.web.research import WebResearchError, _search_cache, web_search

    _search_cache.clear()
    monkeypatch.setattr("brain.app.web.research._brave_search", lambda query, limit: (_ for _ in ()).throw(WebResearchError("no brave")))
    monkeypatch.setattr("brain.app.web.research._tavily_search", lambda query, limit: {"provider": "tavily", "results": []})
    monkeypatch.setattr("brain.app.web.research._duckduckgo_lite_search", lambda query, limit: {"provider": "duckduckgo-lite", "results": []})

    with pytest.raises(WebResearchError, match="No results returned"):
        await web_search("illo unstable", provider="auto", limit=1)

    monkeypatch.setattr(
        "brain.app.web.research._duckduckgo_lite_search",
        lambda query, limit: {"provider": "duckduckgo-lite", "results": [{"title": "Now", "url": "https://now.test", "snippet": None}]},
    )
    result = await web_search("illo unstable", provider="auto", limit=1)

    assert result["count"] == 1
    assert result["provider"] == "duckduckgo-lite"
    assert result["cached"] is False
    assert any(item["provider"] == "tavily" for item in result["provider_errors"])


@pytest.mark.asyncio
async def test_duckduckgo_lite_normalizes_redirect_urls(monkeypatch):
    from brain.app.web.research import _duckduckgo_lite_search

    html = """
    <html><body>
      <a class="result-link" href="/l/?uddg=https%3A%2F%2Ftarget.test%2Fpage">Target title</a>
    </body></html>
    """
    monkeypatch.setattr("brain.app.web.research._http_client", lambda: _FakeClient(_FakeResponse(text=html)))

    result = await _duckduckgo_lite_search("target", 1)

    assert result["results"] == [{
        "title": "Target title",
        "url": "https://target.test/page",
        "snippet": None,
        "source": "duckduckgo-lite",
    }]


def test_tool_handlers_include_web_research():
    from brain.systems.runs.direct_agent import _get_tool_handlers

    handlers = _get_tool_handlers()
    assert "web_search" in handlers
    assert "web_fetch" in handlers

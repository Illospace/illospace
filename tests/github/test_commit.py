"""Commit reads pin checks to a SHA and refuse missing access without remote error text."""

import httpx
import pytest

from illo.connectors.github_client import GitHubClient
from tests.github.test_tools import call

REPO = "acme-example/engine"
SHA = "a" * 40
COMMIT = {"sha": SHA, "html_url": f"https://github.com/{REPO}/commit/{SHA}",
          "commit": {"message": "Repair the export\n\nThe proof.",
                     "author": {"date": "2026-11-01T12:00:00Z"}}}
CHECK = {"name": "Release production", "status": "completed", "conclusion": "success",
         "completed_at": "2026-11-01T12:10:00Z", "html_url": "https://example.com/check/1"}


@pytest.mark.parametrize("total", [1, 101])
async def test_commit_success_and_cap(rpc, rpc_token, app, total):
    requests = []
    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/check-runs"):
            assert request.url.path == f"/repos/{REPO}/commits/{SHA}/check-runs"
            assert request.url.params["per_page"] == "100"
            return httpx.Response(200, json={"total_count": total, "check_runs": [CHECK] * min(total, 100)})
        assert request.url.path.endswith("/commits/release/production")
        return httpx.Response(200, json=COMMIT)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        app.state.ctx.github_client = GitHubClient(token="example", http=http)
        result = await call(rpc, rpc_token[1], "github_get_commit", {"repo": REPO, "ref": "release/production"})
    assert result["decision"] == "allow"
    text = result["result"]
    assert '<outside_data source="github">' in text and SHA in text
    assert "Repair the export" in text and "The proof." not in text
    assert "2026-11-01T12:00:00Z" in text and "Release production" in text
    # Read the full client result too. The tool registry may cut large output.
    if total == 1:
        assert "More check runs" not in text
    else:
        assert "first 100" in text
    assert len(requests) == 2


async def test_commit_client_returns_at_most_100_with_note():
    def handle(request):
        return httpx.Response(200, json={"total_count": 120, "check_runs": [CHECK] * 100}
                              if request.url.path.endswith("/check-runs") else COMMIT)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await GitHubClient(token="example", http=http).get_commit(REPO, "main")
    assert len(result["checks"]) == 100 and "first 100" in result["note"]
    assert result["checks"][0] == CHECK


@pytest.mark.parametrize("status,checks,phrase", [
    (404, False, "ref was not found"),
    (403, False, "refused access"),
    (403, True, "Checks read permissions"),
])
async def test_commit_refusals(rpc, rpc_token, app, status, checks, phrase):
    def handle(request):
        if checks and not request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json=COMMIT)
        return httpx.Response(status, json={"message": "private remote error"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        app.state.ctx.github_client = GitHubClient(token="example", http=http)
        result = await call(rpc, rpc_token[1], "github_get_commit", {"repo": REPO, "ref": "main"})
    assert result["decision"] == "deny" and phrase in result["reason"]
    assert "private remote error" not in str(result)


@pytest.mark.parametrize("repo,ref", [("other/repo", "main"), (REPO, "main?secret"), (REPO, "../main"), (REPO, "main%2fother")])
async def test_commit_scope_and_ref_characters(rpc, rpc_token, github_client, repo, ref):
    result = await call(rpc, rpc_token[1], "github_get_commit", {"repo": repo, "ref": ref})
    assert result["decision"] == "deny" and not github_client.calls

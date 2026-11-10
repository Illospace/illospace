"""The HTTP client uses fake responses for authentication and REST calls."""

import asyncio
import base64
import json
from datetime import UTC, datetime

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from illo.connectors.github_client import GitHubClient, GitHubError

REPO = "acme-example/engine"


async def test_app_token_cache_refresh_and_jwt_signature():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    now = [1_800_000_000.0]
    mints = []
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/access_tokens"):
            mints.append(request)
            jwt = request.headers["authorization"].split()[1]
            header, payload, signature = jwt.split(".")
            key.public_key().verify(
                base64.urlsafe_b64decode(signature + "=="),
                f"{header}.{payload}".encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
            assert claims["iss"] == "123" and claims["exp"] - claims["iat"] <= 600
            assert claims["iat"] <= now[0] < claims["exp"]
            return httpx.Response(
                201,
                json={
                    "token": f"installation-{len(mints)}",
                    "expires_at": datetime.fromtimestamp(now[0] + 3600, UTC).isoformat(),
                },
            )
        return httpx.Response(200, json={"private": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = GitHubClient(
            app_id="123", private_key=pem, installation_id="456", http=http, clock=lambda: now[0]
        )
        await asyncio.gather(*(client.get_repository(REPO) for _ in range(5)))
        assert len(mints) == 1
        now[0] += 3550
        await client.get_repository(REPO)
        assert len(mints) == 2
        assert requests[-1].headers["authorization"] == "Bearer installation-2"


@pytest.mark.parametrize(
    "headers",
    [
        {"retry-after": "65"},
        {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1065"},
    ],
)
async def test_rate_limit_waits_and_reports_sensor(headers):
    attempts = []
    sleeps = []
    reports = []

    def handle(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(403, headers=headers, json={"message": "Rate limit"})
        return httpx.Response(200, json={"private": True})

    async def sleep(seconds):
        sleeps.append(seconds)

    async def report(ok, detail):
        reports.append(ok)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = GitHubClient(
            token="example-test-token", http=http, sleep=sleep, report=report, clock=lambda: 1000
        )
        assert (await client.get_repository(REPO))["private"]
    assert sleeps == [30, 30, 5]
    assert reports == [False, True]


async def test_rate_limit_exhaustion_is_safe():
    async def no_wait(seconds):
        pass

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                429, headers={"retry-after": "1"}, json={"token": "must-not-appear"}
            )
        )
    ) as http:
        client = GitHubClient(token="example-test-token", http=http, sleep=no_wait)
        with pytest.raises(GitHubError, match="status 429") as error:
            await client.get_repository(REPO)
        assert "must-not-appear" not in str(error.value)


async def test_pagination_issue_comments_and_pull_checks():
    requests = []

    def handle(request):
        requests.append(request)
        path = request.url.path
        page = request.url.params.get("page", "1")
        if path == "/search/issues":
            return httpx.Response(
                200,
                json={
                    "items": [{"number": n} for n in range(100)]
                    if page == "1"
                    else [{"number": 101}]
                },
            )
        if path.endswith("/comments"):
            return httpx.Response(200, json=[{"body": "A comment"}])
        if path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": [{"status": "completed"}]})
        if path.endswith("/reviews"):
            return httpx.Response(200, json=[{"state": "APPROVED"}])
        if path.endswith("/status"):
            return httpx.Response(200, json={"state": "success"})
        return httpx.Response(200, json={"number": 1, "head": {"sha": "abc"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = GitHubClient(token="example", http=http)
        assert len(await client.search_issues("failure", repo=REPO, state="all")) == 101
        assert "repo:acme-example/engine failure" == requests[0].url.params["q"]
        assert (await client.get_issue(REPO, 1))["comments_data"][0]["body"] == "A comment"
        pull = await client.get_pull(REPO, 1)
        assert pull["checks"][0]["status"] == "completed" and pull["status"]["state"] == "success"
        assert pull["reviews"][0]["state"] == "APPROVED"


async def test_duplicate_search_fails_closed_if_incomplete():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"incomplete_results": True, "items": []})
        )
    ) as http:
        with pytest.raises(GitHubError):
            await GitHubClient(token="example", http=http).search_issues(
                '"stable failure"', repo=REPO
            )


async def test_list_changed_first_open_then_all_since():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = GitHubClient(token="example", http=http)
        await client.list_changed(REPO)
        await client.list_changed(REPO, since="2026-01-01T00:00:00Z")
    assert requests[0].url.params["state"] == "open" and "since" not in requests[0].url.params
    assert requests[1].url.params["state"] == "all"
    assert requests[1].url.params["since"] == "2026-01-01T00:00:00Z"


async def test_file_write_retries_conflicting_sha():
    writes = []
    reads = []

    def handle(request):
        if request.method == "GET":
            reads.append(request)
            return httpx.Response(200, json={"sha": f"sha-{len(reads)}", "content": "e30="})
        writes.append(json.loads(request.content))
        return httpx.Response(409 if len(writes) == 1 else 200, json={"content": {"sha": "new"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = GitHubClient(token="example", http=http)
        await client.write_file(REPO, "ops/heartbeat", "heartbeat.json", '{"ts":"example"}\n')
    assert writes[0]["sha"] == "sha-1" and writes[1]["sha"] == "sha-2"
    assert base64.b64decode(writes[1]["content"]) == b'{"ts":"example"}\n'
    assert writes[1]["branch"] == "ops/heartbeat"


async def test_mutation_routes_and_bearer():
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer example"
        return httpx.Response(201, json={"id": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = GitHubClient(token="example", http=http)
        await client.create_issue(REPO, "Title", "Body", labels=["bug"])
        await client.assign_issue(REPO, 1, ["acme-ada-example"])
        await client.add_labels(REPO, 1, ["agent:working"])
        await client.comment_issue(REPO, 1, "A comment")
    assert [r.url.path.rsplit("/", 1)[-1] for r in requests] == [
        "issues",
        "assignees",
        "labels",
        "comments",
    ]
    assert json.loads(requests[0].content)["labels"] == ["bug"]


async def test_file_write_initializes_missing_branch_without_copying_code():
    requests = []

    def handle(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(404, json={})
        return httpx.Response(201, json={"sha": "example-sha"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        await GitHubClient(token="example", http=http).write_file(
            REPO, "ops/heartbeat", "heartbeat.json", "{}\n"
        )
    commits = [r for r in requests if r.url.path.endswith("/commits")]
    assert json.loads(commits[0].content)["parents"] == []
    assert json.loads(requests[-1].content)["ref"] == "refs/heads/ops/heartbeat"
    assert not any(r.method == "DELETE" for r in requests)


async def test_app_sign_in_survives_a_wrong_host_clock():
    """GitHub refuses a token signed with a clock that is minutes ahead. Learn its time."""
    from email.utils import format_datetime

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    local, real = 1_800_000_270.0, 1_800_000_000.0  # The host clock is 270 seconds ahead.
    date = format_datetime(datetime.fromtimestamp(real, UTC), usegmt=True)
    mints = []

    def handle(request):
        if request.url.path.endswith("/access_tokens"):
            payload = request.headers["authorization"].split()[1].split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
            mints.append(claims)
            if claims["iat"] > real or claims["exp"] > real + 600:
                return httpx.Response(
                    401, json={"message": "Bad credentials"}, headers={"date": date}
                )
            return httpx.Response(
                201,
                json={
                    "token": "installation-1",
                    "expires_at": datetime.fromtimestamp(real + 3600, UTC).isoformat(),
                },
                headers={"date": date},
            )
        return httpx.Response(200, json={"private": True}, headers={"date": date})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = GitHubClient(
            app_id="123", private_key=pem, installation_id="456", http=http, clock=lambda: local
        )
        await client.get_repository(REPO)
    assert len(mints) == 2
    assert mints[0]["iat"] > real and mints[1]["iat"] <= real < mints[1]["exp"] <= real + 600


async def test_pull_read_survives_a_missing_status_permission():
    """An app without the Commit statuses permission still reads a pull request."""

    def handle(request):
        path = request.url.path
        if path.endswith("/status"):
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        if path.endswith("/check-runs"):
            return httpx.Response(
                200, json={"check_runs": [{"name": "test", "conclusion": "success"}]}
            )
        if path.endswith("/reviews") or path.endswith("/comments"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"number": 7, "head": {"sha": "abc"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = GitHubClient(token="example", http=http)
        pull = await client.get_pull(REPO, 7)
    assert pull["status"] == {} and pull["checks"][0]["conclusion"] == "success"

"""Small GitHub REST client. Errors never include credentials or response bodies."""

import asyncio
import base64
import json
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


class GitHubError(RuntimeError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"GitHub returned status {status}")


class GitHubClient:
    def __init__(
        self,
        *,
        token=None,
        app_id=None,
        private_key=None,
        installation_id=None,
        http=None,
        report=None,
        sleep=asyncio.sleep,
        clock=time.time,
    ):
        self.token = token
        self.app_id = app_id
        self.private_key = private_key
        self.installation_id = installation_id
        self.http = http
        self.report = report
        self.sleep = sleep
        self.clock = clock
        self._installation_token = None
        self._expires = 0
        # Seconds that the local clock is ahead of GitHub. A wrong host clock makes an
        # app sign-in fail, because GitHub checks the times inside the signed token.
        self._skew = 0.0
        self._auth_lock = asyncio.Lock()

    def _jwt(self) -> str:
        def encode(value):
            return base64.urlsafe_b64encode(value).rstrip(b"=")

        now = int(self.clock() - self._skew)
        header = encode(b'{"alg":"RS256","typ":"JWT"}')
        body = encode(
            json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(self.app_id)}).encode()
        )
        message = header + b"." + body
        key = serialization.load_pem_private_key(self.private_key.encode(), password=None)
        signature = key.sign(message, padding.PKCS1v15(), hashes.SHA256())
        return (message + b"." + encode(signature)).decode()

    def _note_server_time(self, value: str | None) -> None:
        """Learn the clock difference from the Date header of each GitHub answer."""
        if not value:
            return
        try:
            server = parsedate_to_datetime(value).timestamp()
        except (TypeError, ValueError):
            return
        skew = self.clock() - server
        self._skew = skew if abs(skew) > 30 else 0.0

    async def _auth(self) -> str:
        if self.token:
            return self.token
        async with self._auth_lock:
            if self._installation_token and self._expires > self.clock() + 60:
                return self._installation_token
            path = f"/app/installations/{self.installation_id}/access_tokens"
            try:
                result = await self._request("POST", path, bearer=self._jwt())
            except GitHubError as exc:
                # The refused answer carried the time of GitHub. Sign again with that time.
                if exc.status != 401 or not self._skew:
                    raise
                result = await self._request("POST", path, bearer=self._jwt())
            self._installation_token = result["token"]
            self._expires = datetime.fromisoformat(
                result["expires_at"].replace("Z", "+00:00")
            ).timestamp()
            return self._installation_token

    async def _request(self, method: str, path: str, *, bearer: str | None = None, **kwargs) -> Any:
        for attempt in range(4):
            auth = bearer or await self._auth()
            headers = {
                "Authorization": f"Bearer {auth}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }

            async def send(client):
                return await client.request(
                    method, "https://api.github.com" + path, headers=headers, **kwargs
                )

            if self.http is not None:
                response = await send(self.http)
            else:
                async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                    response = await send(client)
            self._note_server_time(response.headers.get("date"))
            limited = response.status_code == 429 or (
                response.status_code == 403
                and (
                    response.headers.get("x-ratelimit-remaining") == "0"
                    or "retry-after" in response.headers
                    or "secondary rate limit" in response.text.lower()
                )
            )
            if limited:
                if self.report:
                    await self.report(False, "GitHub rate limit reached")
                if attempt == 3:
                    raise GitHubError(response.status_code)
                delay = float(response.headers.get("retry-after", "0"))
                if not delay:
                    delay = max(
                        1, float(response.headers.get("x-ratelimit-reset", "0")) - self.clock()
                    )
                # Short sleeps let shutdown cancel a long rate-limit wait.
                while delay > 0:
                    step = min(delay, 30)
                    await self.sleep(step)
                    delay -= step
                continue
            if response.status_code >= 400:
                if response.status_code == 401 and not bearer and not self.token and attempt == 0:
                    self._expires = 0
                    continue
                if self.report:
                    await self.report(False, f"GitHub returned status {response.status_code}")
                raise GitHubError(response.status_code)
            if self.report:
                await self.report(True, "")
            return response.json() if response.content else {}
        raise GitHubError(503)

    async def _optional(self, method: str, path: str) -> dict:
        """Read a detail that an app may have no permission for. A refusal gives no data.

        A GitHub App with the Checks permission but without the Commit statuses permission
        gets 403 here. The check runs already carry the state that Illo needs.
        """
        try:
            return await self._request(method, path)
        except GitHubError as exc:
            if exc.status not in (403, 404):
                raise
            if self.report:
                await self.report(True, "")
            return {}

    async def _pages(
        self, path: str, *, params: dict | None = None, key: str | None = None
    ) -> list[dict]:
        rows = []
        for page in range(1, 10001):
            result = await self._request(
                "GET", path, params={**(params or {}), "per_page": 100, "page": page}
            )
            if key and result.get("incomplete_results"):
                raise GitHubError(503)
            batch = result[key] if key else result
            rows.extend(batch)
            if len(batch) < 100:
                return rows
            if key == "items" and len(rows) >= 1000:
                raise GitHubError(422)
        raise GitHubError(422)

    async def get_repository(self, repo: str) -> dict:
        return await self._request("GET", f"/repos/{repo}")

    async def search_issues(
        self, query: str, *, repo: str | None = None, state: str | None = None
    ) -> list[dict]:
        q = f"repo:{repo} {query}" if repo else query
        if state and state != "all":
            q += f" state:{state}"
        return await self._pages("/search/issues", params={"q": q}, key="items")

    async def get_issue(self, repo: str, number: int) -> dict:
        item = await self._request("GET", f"/repos/{repo}/issues/{number}")
        item["comments_data"] = await self._pages(f"/repos/{repo}/issues/{number}/comments")
        return item

    async def create_issue(
        self, repo: str, title: str, body: str, *, labels: list[str] | None = None
    ) -> dict:
        return await self._request(
            "POST",
            f"/repos/{repo}/issues",
            json={"title": title, "body": body, "labels": labels or []},
        )

    async def assign_issue(self, repo: str, number: int, assignees: list[str]) -> dict:
        return await self._request(
            "POST", f"/repos/{repo}/issues/{number}/assignees", json={"assignees": assignees}
        )

    async def ensure_label(self, repo: str, name: str) -> None:
        path = f"/repos/{repo}/labels/{quote(name, safe='')}"
        try:
            await self._request("GET", path)
            return
        except GitHubError as exc:
            if exc.status != 404:
                raise
        try:
            await self._request(
                "POST", f"/repos/{repo}/labels", json={"name": name, "color": "ededed"}
            )
        except GitHubError as exc:
            if exc.status != 422:
                raise
            # Another caller may have created the label. Verify that it exists.
            await self._request("GET", path)

    async def add_labels(self, repo: str, number: int, labels: list[str]) -> list[dict]:
        return await self._request(
            "POST", f"/repos/{repo}/issues/{number}/labels", json={"labels": labels}
        )

    async def comment_issue(self, repo: str, number: int, body: str) -> dict:
        return await self._request(
            "POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body}
        )

    async def get_pull(self, repo: str, number: int) -> dict:
        item = await self._request("GET", f"/repos/{repo}/pulls/{number}")
        sha = item["head"]["sha"]
        item["checks"] = await self._pages(
            f"/repos/{repo}/commits/{sha}/check-runs", key="check_runs"
        )
        item["status"] = await self._optional("GET", f"/repos/{repo}/commits/{sha}/status")
        item["reviews"] = await self._pages(f"/repos/{repo}/pulls/{number}/reviews")
        item["comments_data"] = await self._pages(f"/repos/{repo}/issues/{number}/comments")
        return item

    async def list_changed(self, repo: str, *, since: str | None = None) -> list[dict]:
        params = {"state": "all" if since else "open", "sort": "updated", "direction": "asc"}
        if since:
            params["since"] = since
        return await self._pages(f"/repos/{repo}/issues", params=params)

    async def read_file(self, repo: str, branch: str, path: str) -> dict | None:
        try:
            return await self._request(
                "GET", f"/repos/{repo}/contents/{quote(path, safe='/')}", params={"ref": branch}
            )
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise

    async def _create_branch(self, repo: str, branch: str, path: str, content: str) -> bool:
        try:
            await self._request("GET", f"/repos/{repo}/git/ref/heads/{quote(branch, safe='/')}")
            return False
        except GitHubError as exc:
            if exc.status != 404:
                raise
        blob = await self._request(
            "POST",
            f"/repos/{repo}/git/blobs",
            json={"content": base64.b64encode(content.encode()).decode(), "encoding": "base64"},
        )
        tree = await self._request(
            "POST",
            f"/repos/{repo}/git/trees",
            json={"tree": [{"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]}]},
        )
        commit = await self._request(
            "POST",
            f"/repos/{repo}/git/commits",
            json={
                "message": "ops: initialize Illo heartbeat\n\nCo-authored-by: Codex <noreply@openai.com>",
                "tree": tree["sha"],
                "parents": [],
            },
        )
        await self._request(
            "POST",
            f"/repos/{repo}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]},
        )
        return True

    async def write_file(self, repo: str, branch: str, path: str, content: str) -> dict:
        for attempt in range(3):
            existing = await self.read_file(repo, branch, path)
            if not existing:
                try:
                    if await self._create_branch(repo, branch, path, content):
                        return {"created": True}
                except GitHubError as exc:
                    if exc.status == 422 and attempt < 2:
                        continue
                    raise
            payload = {
                "message": "ops: update Illo heartbeat\n\nCo-authored-by: Codex <noreply@openai.com>",
                "branch": branch,
                "content": base64.b64encode(content.encode()).decode(),
            }
            if existing:
                payload["sha"] = existing["sha"]
            try:
                return await self._request(
                    "PUT", f"/repos/{repo}/contents/{quote(path, safe='/')}", json=payload
                )
            except GitHubError as exc:
                if exc.status not in {409, 422} or attempt == 2:
                    raise

"""Recording channel clients. Keep these methods easy for each builder to extend."""

from dataclasses import dataclass, field

from illo.gateway.replies import RUN_ERROR


@dataclass
class FakeSlackClient:
    calls: list[dict] = field(default_factory=list)
    messages: dict[str, dict] = field(default_factory=dict)
    users: dict[str, dict] = field(default_factory=dict)

    async def _record(self, method, **kwargs):
        self.calls.append({"method": method, **kwargs})
        return {
            "ok": True,
            "channel": kwargs.get("channel", "D_EXAMPLE"),
            "ts": str(len(self.calls)),
        }

    async def chat_postMessage(self, **kwargs):
        result = await self._record("chat_postMessage", **kwargs)
        self.messages[result["ts"]] = kwargs
        return result

    async def chat_update(self, **kwargs):
        return await self._record("chat_update", **kwargs)

    async def chat_postEphemeral(self, **kwargs):
        return await self._record("chat_postEphemeral", **kwargs)

    async def conversations_open(self, **kwargs):
        result = await self._record("conversations_open", **kwargs)
        result["channel"] = {"id": "D_EXAMPLE"}
        return result

    async def conversations_replies(self, **kwargs):
        await self._record("conversations_replies", **kwargs)
        return {"ok": True, "messages": list(self.messages.values()), "has_more": False}

    async def users_info(self, **kwargs):
        await self._record("users_info", **kwargs)
        return {"ok": True, "user": self.users.get(kwargs["user"], {"id": kwargs["user"]})}

    async def auth_test(self):
        return {"ok": True, "user_id": "U_ACME_ILLO", "team_id": "T_ACME"}

    async def reactions_add(self, **kwargs):
        return await self._record("reactions_add", **kwargs)

    async def chat_getPermalink(self, **kwargs):
        await self._record("chat_getPermalink", **kwargs)
        link = f"https://acme.slack.com/archives/{kwargs['channel']}/p{kwargs['message_ts']}"
        return {"ok": True, "permalink": link}

    async def conversations_history(self, **kwargs):
        await self._record("conversations_history", **kwargs)
        return {"ok": True, "messages": list(self.messages.values()), "has_more": False}

    async def conversations_info(self, **kwargs):
        await self._record("conversations_info", **kwargs)
        return {"ok": True, "channel": {"id": kwargs["channel"], "name": "example"}}

    async def users_list(self, **kwargs):
        await self._record("users_list", **kwargs)
        return {"ok": True, "members": list(self.users.values())}

    async def files_info(self, **kwargs):
        await self._record("files_info", **kwargs)
        return {"ok": True, "file": {"id": kwargs["file"]}}


@dataclass
class FakeGitHubClient:
    calls: list[dict] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    pulls: list[dict] = field(default_factory=list)
    repositories: dict[str, dict] = field(default_factory=dict)
    files: dict[tuple, dict] = field(default_factory=dict)

    def _items(self, repo, items):
        return [i for i in items if i.get("repo", repo).lower() == repo.lower()]

    async def get_repository(self, repo):
        self.calls.append({"method": "get_repository", "repo": repo})
        return self.repositories.get(repo, {"private": True, "full_name": repo})

    async def search_issues(self, query: str, *, repo=None, state=None):
        import re

        self.calls.append({"method": "search_issues", "query": query, "repo": repo, "state": state})
        items = self._items(repo, self.issues + self.pulls) if repo else self.issues + self.pulls
        phrase = re.search(r'"([^"]+)"', query)
        if phrase:
            items = [
                i
                for i in items
                if phrase[1].lower() in (i.get("title", "") + " " + i.get("body", "")).lower()
            ]
        if "is:issue" in query:
            items = [i for i in items if "pull_request" not in i]
        if state and state != "all":
            items = [i for i in items if i.get("state", "open") == state]
        return [dict(i) for i in items]

    async def get_issue(self, repo: str, number: int):
        self.calls.append({"method": "get_issue", "repo": repo, "number": number})
        return next(
            (dict(i) for i in self._items(repo, self.issues) if i["number"] == number), None
        )

    async def create_issue(self, repo: str, title: str, body: str, *, labels=None):
        number = (
            max((i["number"] for i in self._items(repo, self.issues + self.pulls)), default=0) + 1
        )
        issue = {
            "number": number,
            "title": title,
            "body": body,
            "labels": labels or [],
            "repo": repo,
            "state": "open",
            "assignees": [],
            "comments_data": [],
        }
        issue["html_url"] = f"https://github.com/{repo}/issues/{number}"
        self.calls.append({"method": "create_issue", **issue})
        self.issues.append(issue)
        return dict(issue)

    async def assign_issue(self, repo: str, number: int, assignees: list[str]):
        self.calls.append(
            {"method": "assign_issue", "repo": repo, "number": number, "assignees": assignees}
        )
        for issue in self._items(repo, self.issues):
            if issue["number"] == number:
                issue["assignees"] = [{"login": login} for login in assignees]
        return {"number": number, "assignees": assignees}

    async def add_labels(self, repo, number, labels):
        self.calls.append(
            {"method": "add_labels", "repo": repo, "number": number, "labels": labels}
        )
        for issue in self._items(repo, self.issues):
            if issue["number"] == number:
                issue.setdefault("labels", []).extend(labels)
        return labels

    async def comment_issue(self, repo: str, number: int, body: str):
        self.calls.append({"method": "comment_issue", "repo": repo, "number": number, "body": body})
        for issue in self._items(repo, self.issues):
            if issue["number"] == number:
                issue.setdefault("comments_data", []).append({"body": body})
        return {"id": len(self.calls), "body": body}

    async def get_pull(self, repo: str, number: int):
        self.calls.append({"method": "get_pull", "repo": repo, "number": number})
        return next((dict(p) for p in self._items(repo, self.pulls) if p["number"] == number), None)

    async def list_changed(self, repo, *, since=None):
        self.calls.append({"method": "list_changed", "repo": repo, "since": since})
        return [
            dict(i)
            for i in self._items(repo, self.issues + self.pulls)
            if (since and i.get("updated_at", since) >= since)
            or (not since and i.get("state", "open") == "open")
        ]

    async def read_file(self, repo, branch, path):
        self.calls.append({"method": "read_file", "repo": repo, "branch": branch, "path": path})
        return self.files.get((repo, branch, path))

    async def write_file(self, repo, branch, path, content):
        self.calls.append(
            {
                "method": "write_file",
                "repo": repo,
                "branch": branch,
                "path": path,
                "content": content,
            }
        )
        self.files[(repo, branch, path)] = {"sha": str(len(self.calls)), "content": content}
        return self.files[(repo, branch, path)]


@dataclass
class FakeChannelAdapter:
    working: list = field(default_factory=list)
    final: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    progress: list = field(default_factory=list)

    async def show_working(self, event):
        self.working.append(event)

    async def show_progress(self, event, item):
        self.progress.append((event, item))

    async def deliver_final(self, event, text):
        self.final.append((event, text))

    async def deliver_error(self, event):
        self.errors.append((event, RUN_ERROR))

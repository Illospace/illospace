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

    async def search_issues(self, query: str, **kwargs):
        self.calls.append({"method": "search_issues", "query": query, **kwargs})
        return list(self.issues)

    async def get_issue(self, repo: str, number: int):
        self.calls.append({"method": "get_issue", "repo": repo, "number": number})
        return next((i for i in self.issues if i["number"] == number), None)

    async def create_issue(self, repo: str, title: str, body: str, **kwargs):
        issue = {"number": len(self.issues) + 1, "title": title, "body": body, **kwargs}
        issue["html_url"] = f"https://github.com/{repo}/issues/{issue['number']}"
        self.calls.append({"method": "create_issue", "repo": repo, **issue})
        self.issues.append(issue)
        return issue

    async def assign_issue(self, repo: str, number: int, assignees: list[str]):
        self.calls.append(
            {"method": "assign_issue", "repo": repo, "number": number, "assignees": assignees}
        )
        return {"number": number, "assignees": assignees}

    async def comment_issue(self, repo: str, number: int, body: str):
        self.calls.append({"method": "comment_issue", "repo": repo, "number": number, "body": body})
        return {"id": len(self.calls), "body": body}

    async def get_pull(self, repo: str, number: int):
        self.calls.append({"method": "get_pull", "repo": repo, "number": number})
        return next((p for p in self.pulls if p["number"] == number), None)


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


@dataclass
class FakeMailboxClient:
    messages: list[tuple[str, bytes]] = field(default_factory=list)
    seen_uids: list[str] = field(default_factory=list)

    async def unseen(self):
        return list(self.messages)

    async def seen(self, uid):
        self.seen_uids.append(uid)


@dataclass
class FakeSMTPClient:
    messages: list = field(default_factory=list)

    async def send(self, message):
        self.messages.append(message)

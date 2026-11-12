"""Connect task wake, proof, and signals to GitHub."""

import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from illo.db import all_rows, one
from illo.ledger import audit, kv, tasks
from illo.tasks import checks, connectors
from illo.tasks.connectors import WakeResult

REPO = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"


class Restart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    remove_labels: list[str] = Field(default_factory=list)
    # An outside dispatcher can treat a comment as a second start signal. Then set this to false.
    comment: bool = True


class IssueWake(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = "github_issue"
    assignee: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]*(?:\[bot\])?$")
    repos: list[str] = Field(min_length=1)
    default_repo: str | None = None
    restart: Restart = Field(default_factory=Restart)

    @model_validator(mode="after")
    def valid_repos(self):
        if any(not re.fullmatch(REPO, repo) for repo in self.repos):
            raise ValueError("Use owner/repository")
        if self.default_repo and self.default_repo.lower() not in {r.lower() for r in self.repos}:
            raise ValueError("The default repository must be in repos")
        return self


def parse(url, kind="issues"):
    parts = urlsplit(url)
    match = re.fullmatch(r"/([^/]+/[^/]+)/" + kind + r"/(\d+)/?", parts.path)
    return (match[1], int(match[2])) if parts.scheme == "https" and parts.netloc == "github.com" and match else None


def allowed(ctx):
    return {repo.lower() for a in ctx.workspace.agents if a.wake.type == "github_issue" for repo in a.wake.repos}


def agent_for(ctx, login):
    return next((a for a in ctx.workspace.agents if a.wake.type == "github_issue"
                 and a.wake.assignee.lower() == (login or "").lower()), None)


async def write(ctx, conn, task, event, step, method, *args, **kwargs):
    """Count each attempt and keep a receipt for each action of a wake event."""
    key = f"task:action:{event['id']}:{step}"
    receipt = await kv.get(conn, key)
    if receipt is not None:
        return receipt
    await conn.execute("SELECT pg_advisory_xact_lock(12000004)")
    budget = await kv.get(conn, "tasks:github-writes") or []
    now = datetime.now(UTC).timestamp()
    budget = [at for at in budget if now-at < 3600]
    if len(budget) >= ctx.workspace.config.tasks.github_writes_per_hour:
        raise ValueError("The task limit for GitHub writes per hour is reached.")
    await kv.set(conn, "tasks:github-writes", [*budget, now])
    await audit.write(conn, principal="code", tool="task_github_" + method, args={"id": task["id"], "step": step},
                      decision="allow", reason="The write belongs to a task action.", target=f"task {task['id']}")
    result = await getattr(ctx.github_client, method)(*args, **kwargs)
    await kv.set(conn, key, result or {})
    return result


async def wake(ctx, conn, task, agent, reason, event):
    if not ctx.github_client:
        return WakeResult(False, "GitHub is not configured.")
    config = agent.wake
    repos = {r.lower(): r for r in config.repos}
    ticket = next((link for link in task["links"] if link.get("kind") == "ticket"
                   and parse(link["url"]) and parse(link["url"])[0].lower() in repos), None)
    repo = None
    if ticket:
        repo, number = parse(ticket["url"])
        repo = repos[repo.lower()]
        issue = await ctx.github_client.get_issue(repo, number)
    else:
        for link in task["links"]:
            if link.get("kind") == "repo":
                candidate = link["url"].removeprefix("https://github.com/").rstrip("/")
                if candidate.lower() in repos:
                    repo = repos[candidate.lower()]
                    break
        repo = repo or config.default_repo
        if not repo:
            return WakeResult(False, "Add a link of kind repo to the task.")
        marker = f"Illo task: {task['id']}"
        matches = await ctx.github_client.search_issues(f'"{marker}" is:issue', repo=repo, state="all")
        issue = next((m for m in matches if marker in (m.get("body") or "").splitlines()), None)
        if not issue:
            from illo.tools.github import issue_body

            links = "\n".join(link["url"] for link in task["links"])
            body = issue_body(ctx.workspace.config.github.issue_header,
                f"{task['brief']}\n\n{links}\n\n{marker}", f"Illo task {task['id']}", f"<!-- illo:task:{task['id']} -->")
            try:
                issue = await write(ctx, conn, task, event, "create", "create_issue", repo, task["title"], body)
            except ValueError as exc:
                return WakeResult(False, str(exc))
        number = issue["number"]
        task = await tasks.add_link(conn, task["id"], workspace=ctx.workspace,
            viewer=tasks.Viewer(None, "code", team=True),
            links=[{"kind": "ticket", "url": issue["html_url"], "title": issue["title"]}], _defer=True)
    if not issue:
        return WakeResult(False, "The linked ticket is missing.")
    try:
        async def act(step, method, *args):
            return await write(ctx, conn, task, event, step, method, repo, number, *args)

        if reason in {"task.released", "task.reopened"}:
            if reason == "task.reopened" and issue["state"] == "closed":
                await act("reopen", "reopen_issue")
            labels = {v["name"] if isinstance(v, dict) else v for v in issue.get("labels", [])}
            for label in config.restart.remove_labels:
                if label in labels:
                    await act("label:" + label, "remove_label", label)
            if reason == "task.released":
                await act("unassign", "unassign_issue", [config.assignee])
                await act("reassign", "assign_issue", [config.assignee])
        logins = {v["login"].lower() for v in issue.get("assignees", [])}
        if reason != "task.released" and config.assignee.lower() not in logins:
            await act("assign", "assign_issue", [config.assignee])
        if reason == "task.message":
            quote = "\n".join("> " + line for line in event["text"].splitlines())
            comment = f"{quote}\n\nFrom the Illo task {task['id']}."
        elif reason == "task.released":
            hours = event["data"].get("hours", task.get("silent_hours", 0) or 0)
            comment = (f"Illo restarts this work after {hours} hours of silence. "
                       "Continue from the present state: read the branch, the pull request, and the comments first.")
        else:
            comment = f"Task {task['id']}: {event['text']}"
        marker = f"<!-- illo:wake:{event['id']} -->"
        # The assignment is the signal for a hand-over. The ticket body already names the task.
        silent = reason == "task.handed" or (reason == "task.released" and not config.restart.comment)
        if not silent and not any(marker in c.get("body", "") for c in issue.get("comments_data", [])):
            await act("comment", "comment_issue", comment + "\n\n" + marker)
    except ValueError as exc:
        return WakeResult(False, str(exc))
    return WakeResult(True, "The ticket is assigned.", [{"kind": "ticket", "url": issue["html_url"]}])


async def fresh(ctx, conn, url, kind, ttl=60):
    if ctx is not None and not hasattr(ctx, "workspace"):
        ctx = getattr(ctx, "app", ctx)
    parsed = parse(url, "issues" if kind == "ticket" else "pull")
    if not ctx or not parsed or parsed[0].lower() not in allowed(ctx):
        return None
    key = "tasks:github-cache:" + url
    cached = await kv.get(conn, key)
    now = datetime.now(UTC).timestamp()
    if cached and now-cached["at"] < ttl:
        return cached["item"]
    method = ctx.github_client.get_issue if kind == "ticket" else ctx.github_client.get_pull
    item = await method(*parsed)
    await kv.set(conn, key, {"at": now, "item": item})
    return item


async def ticket_closed(ctx, conn, task):
    links = [link for link in task["links"] if link.get("kind") == "ticket"]
    if not links:
        return False, "The task has no ticket link."
    for link in links:
        item = await fresh(ctx, conn, link["url"], "ticket")
        if not item or item.get("state") != "closed" or item.get("state_reason") != "completed":
            return False, "A ticket is not closed as completed."
    return True, "Every ticket is closed as completed."


async def change_merged(ctx, conn, task):
    for link in task["links"]:
        if link.get("kind") == "change":
            item = await fresh(ctx, conn, link["url"], "change")
            if item and item.get("merged"):
                return True, "A change is merged."
    return False, "The task needs a merged change link."


async def signal(ctx, conn, repo, item):
    if repo.lower() not in allowed(ctx):
        return
    url = item.get("html_url", f"https://github.com/{repo}/issues/{item['number']}")
    await kv.delete(conn, "tasks:github-cache:" + url)
    # The lane parser already knows the closing-keyword syntax.
    if "pull_request" in item or "merged" in item:
        lane = await one(conn, "SELECT * FROM lanes WHERE source='github' AND external_id=%s", (f"{repo}#{item['number']}",))
        for link in (lane or {}).get("links", []):
            if link["kind"] not in {"closes", "refs"}:
                continue
            match = await one(conn, "SELECT task_id FROM task_tickets WHERE url=%s", (link["url"],))
            if match:
                row = await one(conn, "SELECT * FROM tasks WHERE id=%s", (match["task_id"],))
                if (row["state"] not in {"done", "cancelled"}
                        and not any(v.get("kind") == "change" and v["url"] == url for v in row["links"])):
                    await tasks.add_link(conn, row["id"], workspace=ctx.workspace,
                        viewer=tasks.Viewer(None, "code", team=True),
                        links=[{"kind": "change", "url": url, "title": item["title"]}], ctx=ctx)
        return
    mapping = await one(conn, "SELECT task_id FROM task_tickets WHERE url=%s", (url,))
    if not mapping:
        return
    await conn.execute("SELECT pg_advisory_xact_lock(12000001)")
    row = await one(conn, "SELECT * FROM tasks WHERE id=%s FOR UPDATE", (mapping["task_id"],))
    if row["state"] in {"done", "cancelled"}:
        return
    merged = await all_rows(conn, """SELECT links,title FROM lanes WHERE source='github'
        AND EXISTS (SELECT 1 FROM jsonb_array_elements(links) link
            WHERE link->>'kind' IN ('closes','refs') AND link->>'url'=%s)""", (url,))
    for lane in merged:
        for link in lane["links"]:
            if (link.get("kind") == "github" and parse(link["url"], "pull")
                    and not any(v.get("kind") == "change" and v["url"] == link["url"] for v in row["links"])):
                row = await tasks.add_link(conn, row["id"], workspace=ctx.workspace,
                    viewer=tasks.Viewer(None, "code", team=True),
                    links=[{"kind": "change", "url": link["url"], "title": lane["title"]}], _defer=True)
    agent = next((a for a in ctx.workspace.agents if "agent:" + a.key == row["holder"] and a.wake.type == "github_issue"), None)
    worker = []
    for comment in item.get("comments_data", []):
        login = (comment.get("user") or {}).get("login", "")
        app_id = (comment.get("performed_via_github_app") or {}).get("id")
        own_id = getattr(ctx.config, "github_app_id", None)
        if (app_id and own_id and str(app_id) == str(own_id)) or "<!-- illo:" in comment.get("body", ""):
            continue
        body = comment.get("body", "")
        is_worker = agent and (login.lower() == agent.wake.assignee.lower() or "<!-- worker" in body)
        if is_worker:
            worker.append(comment)
        identity = str(comment.get("id") or comment.get("html_url") or body)
        key = f"task:comment:{row['id']}:{identity}"
        if await kv.get(conn, key):
            continue
        await kv.set(conn, key, True)
        await tasks.alive(conn, row["id"], "github")
        if is_worker:
            await tasks._event(conn, row, row["holder"], "message", body[:500], {"url": comment.get("html_url")})
    labels = {v["name"] if isinstance(v, dict) else v for v in item.get("labels", [])}
    waiting = any(label in labels and state == "waiting_person" for label, state in ctx.workspace.config.github.label_map.items())
    key = f"task:needs-input:{row['id']}"
    was_waiting = await kv.get(conn, key)
    if waiting and not was_waiting:
        target = await tasks.reviewer(conn, ctx.workspace, row)
        await conn.execute("UPDATE tasks SET waits_for=%s WHERE id=%s", (target, row["id"]))
        comment = worker[-1] if worker else {}
        await tasks._event(conn, row, row["holder"] or "code", "question",
            (comment.get("body") or "The ticket needs a person's answer.")[:1000],
            {"to": target, "url": comment.get("html_url", url), "source": "github"})
    elif was_waiting and not waiting and row["state"] in {"todo", "working"}:
        await conn.execute("UPDATE tasks SET waits_for=holder WHERE id=%s", (row["id"],))
        await tasks._event(conn, row, "code", "state", "The ticket no longer needs input.")
    await kv.set(conn, key, waiting)
    state_key = f"task:ticket-state:{row['id']}:{url}"
    state = [item.get("state"), item.get("state_reason")]
    previous_state = await kv.get(conn, state_key)
    await kv.set(conn, state_key, state)
    if item.get("state") != "closed":
        await kv.delete(conn, f"task:not-planned:{row['id']}")
    if item.get("state") == "closed" and state != previous_state:
        if item.get("state_reason") == "completed":
            await tasks.returned_by_signal(conn, row["id"], "github", "The ticket is closed as completed.", workspace=ctx.workspace, ctx=ctx)
        elif item.get("state_reason") == "not_planned" and not await kv.get(conn, f"task:not-planned:{row['id']}"):
            await conn.execute("UPDATE tasks SET waits_for=%s WHERE id=%s", (await tasks.reviewer(conn, ctx.workspace, row), row["id"]))
            await tasks._event(conn, row, "code", "message", "The ticket was closed as not planned.", {"notify": True})
            await kv.set(conn, f"task:not-planned:{row['id']}", True)


async def capture(ctx, conn, repo, issue, login=None, event=None):
    key = ctx.workspace.config.github.task_list
    if not key:
        return None
    url = issue["html_url"]
    await conn.execute("SELECT pg_advisory_xact_lock(12000001)")
    mapping = await one(conn, "SELECT task_id FROM task_tickets WHERE url=%s", (url,))
    if mapping:
        return mapping["task_id"]
    source = (event or {}).get("channel", "signal")
    payload = (event or {}).get("payload", {}).get("payload", {})
    channel = payload.get("channel_id") or payload.get("source") or source
    source = " ".join(str(source).split())[:120]
    channel = next(("#" + c.name for c in ctx.workspace.config.slack.channels if c.id == channel), channel)
    channel = " ".join(str(channel).split())[:120]
    agent = agent_for(ctx, login)
    row = await tasks.create(conn, workspace=ctx.workspace, viewer=tasks.Viewer(None, "code", team=True),
        list_key=key, title=issue["title"], brief=f"Filed from {source} in {channel}.",
        holder="agent:" + agent.key if agent else None, use_default=False,
        links=[{"kind": "ticket", "url": url}, {"kind": "repo", "url": "https://github.com/" + repo}])
    await assigned(ctx, conn, row["id"], login)
    return row["id"]


async def assigned(ctx, conn, task_id, login):
    await conn.execute("SELECT pg_advisory_xact_lock(12000001)")
    row = await one(conn, "SELECT * FROM tasks WHERE id=%s FOR UPDATE", (task_id,))
    if row["state"] in {"done", "cancelled"}:
        return
    agent = agent_for(ctx, login)
    if agent:
        entry = next(e for e in ctx.workspace.lists if e.key == row["list_key"])
        await tasks._holder(conn, ctx.workspace, entry, "agent:" + agent.key)
        if row["holder"] != "agent:" + agent.key:
            row = await one(conn, """UPDATE tasks SET holder=%s,holder_since=now(),handed_by='code',
                state='todo',waits_for=%s,alive_at=NULL,stopped_at=NULL WHERE id=%s RETURNING *""",
                ("agent:" + agent.key, "agent:" + agent.key, task_id))
            await tasks._event(conn, row, "code", "handed", "The ticket gives the task a holder.", {"holder": row["holder"]})
        # Assignment already starts the outside worker. Suppress only the hand-over wake.
        await conn.execute("""UPDATE task_wakes SET finished_at=now() WHERE task_id=%s
            AND reason='task.handed' AND finished_at IS NULL""", (task_id,))
        await tasks._event(conn, row, "code", "woken", "The ticket is assigned.")
    else:
        target = "person:" + ctx.workspace.config.default_owner
        await conn.execute("UPDATE tasks SET waits_for=%s WHERE id=%s", (target, task_id))
        await tasks._event(conn, row, "code", "state", "The ticket waits for an owner.")


async def lane_signal(ctx, conn, lane, task_id=None, token=None):
    # A collector can affect only tasks of its person or that person's agents.
    if not token:
        return
    refs = ["person:" + token["person_key"], *["agent:" + a.key for a in ctx.workspace.agents if a.owner == token["person_key"]]]
    urls = {link if isinstance(link, str) else link.get("url")
            for link in lane["links"] if isinstance(link, (str, dict))}
    title = lane.get("title") or ""
    await conn.execute("SELECT pg_advisory_xact_lock(12000001)")
    rows = await all_rows(conn, "SELECT * FROM tasks WHERE state NOT IN ('done','cancelled') AND holder=ANY(%s)", (refs,))
    pulls = await all_rows(conn, """SELECT * FROM lanes WHERE source='github'
        AND (visibility='team' OR owner_person_id=(SELECT id FROM people WHERE key=%s))
        AND EXISTS (SELECT 1 FROM jsonb_array_elements(links) link WHERE link->>'url'=ANY(%s))""",
        (token["person_key"], list(urls)))
    for row in rows:
        for pull in pulls:
            changes = [v["url"] for v in pull["links"] if v.get("kind") == "github"
                       and v.get("url") in urls and parse(v["url"], "pull")]
            for change in changes:
                repo, _ = parse(change, "pull")
                tickets = {v["url"] for v in row["links"] if v.get("kind") == "ticket"
                           and parse(v["url"]) and parse(v["url"])[0].lower() == repo.lower()}
                if not any(v.get("kind") in {"closes", "refs"} and v.get("url") in tickets for v in pull["links"]):
                    continue
                if not any(v.get("kind") == "change" and v["url"] == change for v in row["links"]):
                    row = await tasks.add_link(conn, row["id"], workspace=ctx.workspace,
                        viewer=tasks.Viewer(None, "code", team=True),
                        links=[{"kind": "change", "url": change, "title": pull["title"]}], ctx=ctx)
        if row["state"] not in {"todo", "working"}:
            continue
        linked = any(link.get("kind") in {"ticket", "change"} and link.get("url") in urls for link in row["links"])
        if not (row["id"] == task_id or linked or names_ticket(title, row)):
            continue
        key = f"task:lane-stop:{row['id']}:{lane['id']}:{row['holder_since']}"
        live = f"task:lane-live:{row['id']}:{lane['id']}"
        if lane["state"] == "working":
            await kv.delete(conn, key)
            await kv.set(conn, live, True)
            await tasks.alive(conn, row["id"], lane["source"])
        elif lane["state"] in {"done", "failed", "unknown"}:
            await kv.delete(conn, live)
            # One run can stop while another run still does the work.
            if await one(conn, "SELECT key FROM kv WHERE key LIKE %s LIMIT 1", (f"task:lane-live:{row['id']}:%",)):
                continue
            tickets = [link for link in row["links"] if link.get("kind") == "ticket"]
            is_open = False
            for link in tickets:
                item = await fresh(ctx, conn, link["url"], "ticket", ttl=0)
                is_open |= bool(item and item.get("state") == "open")
            if not is_open:
                continue
            if await kv.get(conn, key):
                continue
            await kv.set(conn, key, True)
            await tasks._event(conn, row, "code", "state", "The agent's run stopped and the work is not returned.", {"lane": lane["id"]})
            await conn.execute("UPDATE tasks SET stopped_at=now() WHERE id=%s", (row["id"],))


def names_ticket(title, row):
    """A run title that names the ticket as `repo#12` or `repo #12` belongs to the task."""
    for link in row["links"]:
        ref = parse(link.get("url", "")) if link.get("kind") == "ticket" else None
        if not ref:
            continue
        owner, name = ref[0].split("/")
        pattern = rf"(?<![\w/.-])(?:{re.escape(owner)}/)?{re.escape(name)}\s?#{ref[1]}(?!\d)"
        if re.search(pattern, title, re.IGNORECASE):
            return True
    return False


def register():
    if "github_issue" not in connectors.wake_types:
        connectors.register_wake("github_issue", IssueWake, wake)
        connectors.register_signal("github", signal)
        connectors.register_links("ticket", signal)
        connectors.register_links("change", signal)
        connectors.register_lane(lane_signal)
    if not checks.known("ticket_closed"):
        checks.register("ticket_closed", ticket_closed)
    if not checks.known("change_merged"):
        checks.register("change_merged", change_merged)

#!/usr/bin/env python3
"""Report local lanes and pull answers. Illo never connects to this machine."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import socket
import sqlite3
import sys
import time
import tomllib
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from collectors.common import (
    VERSION,
    CollectorError,
    MCPClient,
    atomic_json,
    clean,
    read_token,
)

# Illo accepts 1,000 lanes in one report. A machine can have more threads than that.
LANE_BATCH = 500


@dataclass
class Config:
    mcp_url: str
    token_file: Path
    machine: str = field(default_factory=socket.gethostname)
    t3_url: str = "http://127.0.0.1:3773"
    t3_token_file: Path | None = None
    data_path: Path | None = None
    state_file: Path = field(default_factory=lambda: Path.home() / ".local/state/illo/t3.json")
    poll_seconds: float = 30
    projects: dict[str, str] = field(default_factory=dict)
    owner: str | None = None
    environment_id: str | None = None
    share: str = "private"

    def __post_init__(self):
        if self.share not in {"private", "team"}:
            raise CollectorError("Set share to private or team.")

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        values = tomllib.loads(path.read_text()) if path else {}
        names = {
            "ILLO_MCP_URL": "mcp_url",
            "ILLO_TOKEN_FILE": "token_file",
            "ILLO_MACHINE": "machine",
            "ILLO_T3_URL": "t3_url",
            "ILLO_T3_TOKEN_FILE": "t3_token_file",
            "ILLO_T3_DATA_PATH": "data_path",
            "ILLO_STATE_FILE": "state_file",
            "ILLO_POLL_SECONDS": "poll_seconds",
            "ILLO_PROJECTS": "projects",
            "ILLO_OWNER": "owner",
            "ILLO_T3_ENVIRONMENT_ID": "environment_id",
            "ILLO_SHARE": "share",
        }
        for env, key in names.items():
            if env in os.environ:
                values[key] = json.loads(os.environ[env]) if key == "projects" else os.environ[env]
        for key in ("token_file", "t3_token_file", "data_path", "state_file"):
            if values.get(key):
                values[key] = Path(values[key]).expanduser()
        values["poll_seconds"] = float(values.get("poll_seconds", 30))
        if values["poll_seconds"] <= 0:
            raise CollectorError("The poll time must be positive.")
        if not values.get("mcp_url") or not values.get("token_file"):
            raise CollectorError("Set ILLO_MCP_URL and ILLO_TOKEN_FILE.")
        if not isinstance(values.get("projects", {}), dict):
            raise CollectorError("The project map must be a JSON object or a TOML table.")
        return cls(**values)


@dataclass
class Pending:
    id: str
    question: str
    options: list[dict] = field(default_factory=list)
    recommended: str | None = None
    question_id: str | None = None
    kind: str = "text"
    manual: bool = False
    values: dict[str, str] = field(default_factory=dict)


@dataclass
class Thread:
    id: str
    title: str
    state: str
    summary: str
    link: str
    project: str
    updated_at: str = ""
    pending: list[Pending] = field(default_factory=list)
    is_step: bool = False
    parent_ref: str | None = None
    links: list[str] = field(default_factory=list)
    progress: dict | None = None


def step_list(item):
    if item.get("type") == "todo_list":
        return item["steps"] if isinstance(item.get("steps"), list) else None
    if item.get("type") != "dynamic_tool" or not re.search(
        r"todo|update_plan", item.get("toolName", ""), re.I
    ):
        return None
    value = item.get("input") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if isinstance(value, dict):
        steps = value.get("todos", value.get("plan", value.get("steps")))
        return steps if isinstance(steps, list) else None
    return None


def activity(item):
    kind = item.get("type")
    if kind in {"user_input_request", "approval_request", "secret_request"}:
        return "waits" if item.get("status") in {"pending", "running", "in_progress"} else None
    if kind == "dynamic_tool":
        if step_list(item) is not None:
            return None
        name = re.split(r"[./:]|__", item.get("toolName", ""))[-1].lower()
        if name in {"read", "read_file", "readfile", "view_image"}:
            return "reads"
        if name in {"write", "edit", "write_file", "edit_file", "apply_patch", "multiedit"}:
            return "changes"
        return "tool"
    if kind == "command_execution":
        command = item.get("input", "")
        if isinstance(command, dict):
            command = command.get("command", command.get("cmd", ""))
        if not isinstance(command, str):
            return "command"
        if re.match(r"^\s*git (?:commit|push)\b", command):
            return "saves"
        if re.match(r"^\s*gh pr create\b", command):
            return "pull"
        if re.match(r"^\s*(?:(?:rg|grep|cat|ls|find|head|tail)\b|sed -n\b|"
                    r"git (?:diff|log|show|status)\b|gh (?:issue|pr) view\b)", command):
            return "reads"
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
            lexer.whitespace_split = True
            parts = [[]]
            for token in lexer:
                if token in {";", "&&", "||", "|", "&"}:
                    parts.append([])
                else:
                    parts[-1].append(token)
            for part in parts:
                while part and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", part[0]) or part[0] in {"env", "npx"}):
                    part = part[1:]
                if not part:
                    continue
                tool = part[0].rsplit("/", 1)[-1]
                if tool in {"pytest", "vitest", "jest", "playwright"}:
                    return "tests"
                if tool in {"npm", "go", "cargo"} and (part[1:2] == ["test"] or (tool == "npm" and part[1:3] == ["run", "test"])):
                    return "tests"
                if re.fullmatch(r"python[0-9.]*", tool) and part[1:3] == ["-m", "pytest"]:
                    return "tests"
        except ValueError:
            pass
        return "command"
    return {"file_change": "changes", "reasoning": "thinks", "compaction": "thinks",
            "assistant_message": "writes", "web_search": "searches", "subagent": "helper"}.get(kind)


def run_progress(items):
    """Reduce local items to the bounded data that may leave this machine."""
    result = {}
    ordered = sorted(items, key=lambda item: item.get("ordinal", 0), reverse=True)
    steps = next((steps for item in ordered if (steps := step_list(item)) is not None), None)
    if steps is not None:
        steps = [{"text": str(step.get("text", step.get("content", step.get("step", ""))))[:120],
                  "state": {"completed": "done", "done": "done", "in_progress": "now",
                            "running": "now", "now": "now"}.get(step.get("status", step.get("state")), "todo")}
                 for step in steps if isinstance(step, dict)]
        result.update(steps_total=min(len(steps), 100000),
                      steps_done=min(sum(step["state"] == "done" for step in steps), 100000))
        if len(steps) > 12:
            current = next((i for i, step in enumerate(steps) if step["state"] == "now"),
                           next((i for i, step in enumerate(steps) if step["state"] == "todo"), len(steps)))
            last_done = next((step for step in reversed(steps[:current]) if step["state"] == "done"), None)
            shown = ([last_done] if last_done else []) + steps[current:current + (11 if last_done else 12)]
            # A nearly complete list still shows twelve steps when twelve exist.
            if len(shown) < 12:
                shown = steps[max(0, current - (12 - len(steps[current:]))):][:12]
            steps = shown
        result["steps"] = steps
    now = next((key for item in ordered if (key := activity(item))), None)
    if now:
        result["now"] = now
    paths = {change["path"] for item in items if item.get("type") == "file_change"
             for change in item.get("changes", []) if isinstance(change, dict) and isinstance(change.get("path"), str)}
    if paths:
        result["files_changed"] = min(len(paths), 100000)
    return result


def task_links(row, projection):
    """Read only task links. Never copy a user's message into a lane."""
    links = []
    for item in [*(row.get("pullRequests") or []), row.get("linkedPullRequest"), row.get("branchPullRequest")]:
        url = item.get("url") if isinstance(item, dict) else None
        if url and re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/\d+", url):
            links.append(url)
    messages = projection.get("firstUserMessage") or sorted(
        [m for m in projection.get("messages", []) if m.get("role") == "user"],
        key=lambda m: m.get("createdAt") or m.get("updatedAt") or "",
    )[:1]
    if messages:
        urls = re.findall(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/\d+(?![\w/#?])",
                          messages[0].get("text", ""))
        if len(urls) == 1:
            links.extend(urls)
    return list(dict.fromkeys(links))


def lane_state(
    status: str, waiting: bool = False, settled: bool = False, *,
    archived: bool = False, person_settled: bool = False,
    person_created: bool = False, is_step: bool = False,
) -> str:
    if status == "running":
        return "working"
    if waiting or status in {"waiting", "waiting_input", "waiting_approval"}:
        return "waiting_person"
    if status == "failed":
        return "failed"
    if archived or person_settled:
        return "done"
    if status in {"completed", "finished"} and person_created and not is_step:
        return "review"
    if is_step and (status in {"completed", "finished", "settled"} or settled):
        return "done"
    return "unknown"


def start_question(text: str) -> bool:
    """Only these explicit start questions get Continue and Stop."""
    return bool(
        re.fullmatch(
            r"(?:may|can|should) I (?:start|begin|continue|proceed)(?: (?:with|the) [^?\n]+)?\?"
            r"|do you want me to (?:start|begin|continue|proceed)\?",
            text.strip(),
            re.IGNORECASE,
        )
    )


class T3Source:
    """Keep all T3 fields here. Replace this class to support another workspace."""

    def __init__(self, config: Config, client: httpx.Client | None = None):
        self.config = config
        self.client = client or httpx.Client(timeout=15, trust_env=False)
        self.token = read_token(config.t3_token_file) if config.t3_token_file else ""
        self.mcp = MCPClient(config.t3_url.rstrip("/") + "/mcp", self.token, self.client)
        self._cache: dict[str, tuple[str, Thread]] = {}

    def _get(self, path: str) -> dict:
        try:
            response = self.client.get(
                self.config.t3_url.rstrip("/") + path,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "x-t3-orchestration-protocol": "2",
                },
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise CollectorError("The T3 snapshot is unavailable.") from error

    def _shell_rows(self) -> list[dict]:
        shell = self._get("/api/orchestration/shell")
        if not isinstance(shell.get("threads"), list):
            raise CollectorError("The T3 shell schema changed.")
        if shell.get("olderSettled", {}).get("counts"):
            raise CollectorError("The T3 snapshot omits older settled threads.")
        return [
            row
            for row in shell["threads"] + shell.get("archivedThreads", [])
            if not row.get("deletedAt") and self._selected(row)
        ]

    def _read_thread(self, row: dict) -> Thread:
        projection = self._get("/api/orchestration/threads/" + quote(row["id"], safe=""))
        return self._thread(row, projection["projection"])

    def list_threads(self) -> list[Thread]:
        if self.config.data_path:
            return self._sqlite_threads()
        result = []
        for row in self._shell_rows():
            fingerprint = json.dumps(row, sort_keys=True)
            cached = self._cache.get(row["id"])
            # Working threads get fresh text even when T3 keeps the shell timestamp.
            if (
                not cached
                or cached[0] != fingerprint
                or row.get("status") in {"running", "waiting"}
            ):
                self._cache[row["id"]] = (fingerprint, self._read_thread(row))
            result.append(self._cache[row["id"]][1])
        self._cache = {thread.id: self._cache[thread.id] for thread in result}
        return result

    def get_thread(self, thread_id: str) -> Thread | None:
        # A failed read raises. Only a successful snapshot can prove that a lane is gone.
        if self.config.data_path:
            return next(
                (thread for thread in self._sqlite_threads() if thread.id == thread_id),
                None,
            )
        row = next((row for row in self._shell_rows() if row["id"] == thread_id), None)
        return self._read_thread(row) if row else None

    def _selected(self, row: dict) -> bool:
        return not self.config.projects or row["projectId"] in self.config.projects

    def _thread(self, row: dict, projection: dict) -> Thread:
        row = {**row, **projection.get("thread", {})}
        thread_id = row["id"]
        prefix = (
            f"v2/{quote(self.config.environment_id, safe='')}/"
            if self.config.environment_id
            else "v1/"
        )
        link = "t3-thread://" + prefix + quote(thread_id, safe="")
        requests = [
            request
            for request in projection.get("runtimeRequests", [])
            if request.get("status") == "pending"
            and request.get("kind") not in {"dynamic_tool_call", "auth_refresh"}
        ]
        items = {
            item["requestId"]: item
            for item in projection.get("turnItems", [])
            if item.get("type") in {"user_input_request", "approval_request"}
        }
        pending = []
        for request in requests:
            item = items.get(request["id"], {})
            questions = item.get("questions", [])
            capability = request.get("responseCapability", {}).get("type")
            manual = request["kind"] != "user_input" or capability == "not_resumable"
            if len(questions) == 1:
                question = questions[0]
                options = []
                values = {}
                recommended = None
                for index, option in enumerate(question.get("options", [])):
                    option_id = str(option.get("id", f"option-{index + 1}"))
                    options.append(
                        {
                            "id": option_id,
                            "label": option["label"],
                            "detail": option.get("description", ""),
                        }
                    )
                    values[option_id] = option.get("value", option["label"])
                    if option.get("recommended") or "(recommended)" in option["label"].lower():
                        recommended = option_id
                plain_start = not options and start_question(question["question"])
                if plain_start:
                    values = {"continue": "Continue", "stop": "Stop"}
                pending.append(
                    Pending(
                        request["id"],
                        question["question"],
                        options,
                        recommended,
                        question["id"],
                        request["kind"],
                        manual
                        or bool(question.get("multiSelect"))
                        or (not 2 <= len(options) <= 5 and not plain_start),
                        values,
                    )
                )
            else:
                pending.append(
                    Pending(
                        request["id"],
                        item.get("prompt")
                        or item.get("reason")
                        or "Open the thread to answer the pending request.",
                        kind=request["kind"],
                        manual=True,
                    )
                )
        messages = [
            message
            for message in projection.get("messages", [])
            if message.get("role") == "assistant"
        ]
        latest = max(messages, key=lambda msg: msg.get("updatedAt", ""), default={})
        status = row.get("activityRunStatus") or row.get("status", "unknown")
        lineage = row.get("lineage") or {}
        is_step = row.get("createdBy") == "agent" or lineage.get("relationshipToParent") == "subagent"
        settled = row.get("settledOverride") == "settled"
        evidence = projection.get("settlement", {})
        # T3 records each actor as the server. The command ID shows the sender:
        # the app sends a plain UUID, the server rules use "server:", and MCP tools use "mcp:".
        person_settled = (
            settled
            and evidence.get("event_type") == "thread.settled"
            and evidence.get("command_type") == "thread.settle"
            and evidence.get("status") == "accepted"
            and bool(re.fullmatch(
                r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
                evidence.get("command_id") or "",
            ))
            and not (row.get("unsettledAt") and (
                not row.get("settledAt") or row["unsettledAt"] >= row["settledAt"]
            ))
        )
        state = lane_state(
            status, bool(pending), settled,
            archived=bool(row.get("archivedAt")), person_settled=person_settled,
            person_created=row.get("createdBy") == "user", is_step=is_step,
        )
        if row.get("blockedOnThreadWait") and not pending:
            state = "unknown"
        if state == "waiting_person" and not pending:
            text = latest.get("text", "")
            # A stable message ID keeps this fallback ask separate from the next question.
            if text:
                pending.append(Pending("message-" + latest["id"], text))
        latest_run = row.get("latestRunId")
        progress = None
        if status in {"running", "waiting", "waiting_input", "waiting_approval"} and latest_run:
            progress = run_progress([item for item in projection.get("turnItems", [])
                                     if item.get("runId") == latest_run])
        if "progress" in projection:
            progress = projection["progress"]
        return Thread(
            thread_id,
            row["title"],
            state,
            latest.get("text", ""),
            link,
            row["projectId"],
            row.get("updatedAt", ""),
            pending,
            is_step,
            lineage.get("parentThreadId") if is_step else None,
            task_links(row, projection),
            progress,
        )

    def _sqlite_threads(self) -> list[Thread]:
        """Read one consistent snapshot, including the WAL. Never change T3's database."""
        path = self.config.data_path
        if path.is_dir():
            path = path / "userdata/statev2.sqlite"
        try:
            with closing(
                sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            ) as connection:
                connection.execute("BEGIN")
                rows = connection.execute(
                    "SELECT payload_json FROM orchestration_v2_projection_threads "
                    "WHERE deleted_at IS NULL"
                ).fetchall()
                runs = dict(
                    connection.execute(
                        "SELECT thread_id, status FROM (SELECT thread_id, status, "
                        "row_number() OVER (PARTITION BY thread_id "
                        "ORDER BY (status='running') DESC,ordinal DESC) AS position "
                        "FROM orchestration_v2_projection_runs) WHERE position=1"
                    )
                )
                projections = {}
                queries = {
                    "messages": "SELECT thread_id, payload_json FROM (SELECT thread_id, payload_json, "
                    "row_number() OVER (PARTITION BY thread_id ORDER BY updated_at DESC) AS position "
                    "FROM orchestration_v2_projection_messages WHERE role='assistant') WHERE position=1",
                    "firstUserMessage": "SELECT thread_id, payload_json FROM (SELECT thread_id, payload_json, "
                    "row_number() OVER (PARTITION BY thread_id ORDER BY "
                    "json_extract(payload_json, '$.createdAt'),updated_at,rowid) AS position "
                    "FROM orchestration_v2_projection_messages WHERE role='user') WHERE position=1",
                    "runtimeRequests": "SELECT thread_id, payload_json "
                    "FROM orchestration_v2_projection_runtime_requests WHERE status='pending'",
                    "turnItems": "SELECT i.thread_id, i.payload_json "
                    "FROM orchestration_v2_projection_turn_items i "
                    "JOIN orchestration_v2_projection_runtime_requests r ON i.thread_id=r.thread_id "
                    "AND json_extract(i.payload_json, '$.requestId')=json_extract(r.payload_json, '$.id') "
                    "WHERE r.status='pending' AND i.type IN ('user_input_request','approval_request')",
                }
                for key, query in queries.items():
                    for thread_id, payload in connection.execute(query):
                        projections.setdefault(thread_id, {}).setdefault(key, []).append(
                            json.loads(payload)
                        )
                pulls = {}
                # Older T3 versions keep only the JSON link. The separate table is optional.
                has_pulls = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='projection_thread_pull_requests'"
                ).fetchone()
                if has_pulls:
                    for thread_id, url in connection.execute(
                        "SELECT thread_id,url FROM projection_thread_pull_requests ORDER BY thread_id,linked_at,url"
                    ):
                        pulls.setdefault(thread_id, []).append({"url": url})
                result = []
                for (payload,) in rows:
                    row = json.loads(payload)
                    if not self._selected(row):
                        continue
                    thread_id = row["id"]
                    row["status"] = runs.get(thread_id, "unknown")
                    row["pullRequests"] = [*(row.get("pullRequests") or []), *pulls.get(thread_id, [])]
                    projection = projections.setdefault(thread_id, {})
                    lineage = row.get("lineage") or {}
                    personal = (
                        row.get("createdBy") == "user"
                        and lineage.get("relationshipToParent") != "subagent"
                    )
                    if personal and row.get("settledOverride") == "settled":
                        # Only a person's thread needs this proof. The stream index limits the read.
                        evidence = connection.execute(
                            "SELECT e.event_type,c.command_type,c.command_id,c.status "
                            "FROM orchestration_events e LEFT JOIN orchestration_command_receipts c "
                            "ON c.command_id=e.command_id "
                            "WHERE e.aggregate_kind='thread' AND e.stream_id=? "
                            "AND e.event_type IN ('thread.settled','thread.unsettled') "
                            "ORDER BY e.sequence DESC LIMIT 1", (thread_id,),
                        ).fetchone()
                        if evidence:
                            projection["settlement"] = dict(zip(
                                ("event_type", "command_type", "command_id", "status"),
                                evidence,
                            ))
                    links = task_links(row, projection)
                    if links:
                        run = connection.execute(
                            "SELECT run_id,status FROM orchestration_v2_projection_runs "
                            "WHERE thread_id=? ORDER BY ordinal DESC LIMIT 1", (thread_id,),
                        ).fetchone()
                        projection["progress"] = None
                        if run and run[1] in {"running", "waiting", "waiting_input", "waiting_approval"}:
                            recent = [json.loads(item[0]) for item in connection.execute(
                                "SELECT payload_json FROM orchestration_v2_projection_turn_items "
                                "WHERE thread_id=? AND run_id=? ORDER BY ordinal DESC LIMIT 12",
                                (thread_id, run[0]),
                            )]
                            plan = connection.execute(
                                "SELECT payload_json FROM orchestration_v2_projection_turn_items "
                                "WHERE thread_id=? AND run_id=? AND (type='todo_list' OR "
                                "(type='dynamic_tool' AND (lower(json_extract(payload_json,'$.toolName')) "
                                "LIKE '%todo%' OR lower(json_extract(payload_json,'$.toolName')) LIKE '%update_plan%'))) "
                                "ORDER BY ordinal DESC LIMIT 1", (thread_id, run[0]),
                            ).fetchone()
                            value = run_progress([*recent, *([json.loads(plan[0])] if plan else [])])
                            count = connection.execute(
                                "SELECT count(DISTINCT json_extract(c.value,'$.path')) "
                                "FROM orchestration_v2_projection_turn_items i, "
                                "json_each(i.payload_json,'$.changes') c "
                                "WHERE i.thread_id=? AND i.run_id=? AND i.type='file_change' "
                                "AND json_type(c.value,'$.path')='text'", (thread_id, run[0]),
                            ).fetchone()[0]
                            if count:
                                value["files_changed"] = min(count, 100000)
                            projection["progress"] = value
                    result.append(self._thread(row, projection))
                return result
        except (sqlite3.Error, ValueError, KeyError) as error:
            raise CollectorError(
                "The read-only T3 database is unavailable or its schema changed."
            ) from error

    def deliver(self, thread: Thread, pending: Pending, answer: dict, message: str) -> None:
        if not self.token:
            raise CollectorError("Set ILLO_T3_TOKEN_FILE before answer delivery.")
        if pending.question_id:
            option = answer.get("answer_option")
            if option and option.get("id") not in pending.values:
                raise CollectorError("The answer does not match the pending options.")
            # T3 resumes a live question only through its structured response path.
            self.mcp.call(
                "t3_pending_request",
                {
                    "action": "respond",
                    "threadId": thread.id,
                    "requestId": pending.id,
                    "answers": {pending.question_id: message},
                },
            )
        else:
            self.mcp.call(
                "t3_thread_send",
                {
                    "threadId": thread.id,
                    "message": message,
                    "mode": "queue",
                    "clientRequestId": f"illo-answer-{answer['ask_id']}",
                },
            )


class Collector:
    def __init__(self, config: Config, source: T3Source, mcp: MCPClient, clock=time.time):
        self.config, self.source, self.mcp, self.clock = config, source, mcp, clock
        self.secrets = (mcp.token, source.token)
        self.state = json.loads(config.state_file.read_text()) if config.state_file.exists() else {}
        self.state.setdefault("lanes", {})
        self.state.setdefault("asks", {})
        self.state.setdefault("delivered", {})

    def save(self) -> None:
        atomic_json(self.config.state_file, self.state)

    def lane(self, thread: Thread) -> dict:
        lane = {
            "source": "t3",
            "external_id": thread.id,
            "title": clean(thread.title, 200, self.secrets),
            "state": thread.state,
            "summary": clean(thread.summary, 500, self.secrets),
            "links": [thread.link, *[clean(url, 2000, self.secrets) for url in thread.links]],
            "repo": self.config.projects.get(thread.project),
            "machine": self.config.machine,
            "visibility": "team" if self.config.share == "team" else "private",
            "is_step": thread.is_step,
            "parent_ref": thread.parent_ref,
        }
        if thread.links:
            lane["progress"] = thread.progress
            if thread.progress:
                lane["progress"] = dict(thread.progress)
                if "steps" in thread.progress:
                    lane["progress"]["steps"] = [{**step, "text": clean(step["text"], 120, self.secrets)}
                                                 for step in thread.progress["steps"]]
        if self.config.owner:
            lane["owner"] = self.config.owner
        return lane

    def ask(self, thread: Thread, pending: Pending) -> dict:
        options = pending.options
        manual = pending.manual
        if not options:
            if not manual and start_question(pending.question):
                options = [
                    {"id": "continue", "label": "Continue"},
                    {"id": "stop", "label": "Stop"},
                ]
            else:
                manual = True
        if manual:
            # The server requires 2–5 options. Both choices leave the request in T3.
            options = [
                {"id": "open-thread", "label": "Answer in the thread"},
                {"id": "dismiss", "label": "Dismiss this card"},
            ]
        args = {
            "lane": {"source": "t3", "external_id": thread.id},
            "kind": "decision",
            "question": clean(pending.question, 500, self.secrets),
            "options": [
                {
                    key: clean(value, 200, self.secrets)
                    for key, value in option.items()
                    if key in {"id", "label", "detail"}
                }
                for option in options
            ],
            "source_ref": f"t3:{thread.id}:{pending.id}",
            "proof": {
                "text": "Open the thread to answer this request." if manual else "Agent question.",
                "links": [thread.link],
            },
        }
        if not manual and pending.recommended:
            args["recommended"] = pending.recommended
        if self.config.owner:
            args["owner"] = self.config.owner
        return args

    def poll(self) -> None:
        threads = self.source.list_threads()
        now = self.clock()
        refresh = now - self.state.get("refreshed_at", 0) >= 600
        lanes = [self.lane(thread) for thread in threads]
        hashes = {
            lane["external_id"]: hashlib.sha256(
                json.dumps(lane, sort_keys=True).encode()
            ).hexdigest()
            for lane in lanes
        }
        changed = [
            lane
            for lane in lanes
            if refresh
            or self.state["lanes"].get(lane["external_id"]) != hashes[lane["external_id"]]
        ]
        # An empty report still goes out, because it is the heartbeat.
        for start in range(0, max(len(changed), 1), LANE_BATCH):
            self.mcp.call(
                "lane_report",
                {
                    "lanes": changed[start : start + LANE_BATCH],
                    "collector": {
                        "label": self.config.machine,
                        "version": VERSION,
                        "threads_seen": len(threads),
                        "expected_every_s": self.config.poll_seconds,
                    },
                },
            )
        self.state["lanes"] = hashes
        if refresh:
            self.state["refreshed_at"] = now
        self.save()
        active = {}
        for thread in threads:
            for pending in thread.pending:
                args = self.ask(thread, pending)
                ref = args["source_ref"]
                active[ref] = (thread, pending)
                if ref not in self.state["asks"]:
                    self.mcp.call("ask_create", args)
                    self.state["asks"][ref] = {"manual": args["options"][0]["id"] == "open-thread"}
                    self.save()
        for ref in list(self.state["asks"]):
            if ref not in active and not self.state["asks"][ref].get("cancelled"):
                self.mcp.call("ask_cancel", {"source_ref": ref})
                self.state["asks"][ref]["cancelled"] = True
                self.save()
        pulled = self.mcp.call("ask_pull", {"scope": "token"})
        answers = pulled if isinstance(pulled, list) else pulled["answers"]
        failed = False
        for answer in answers:
            try:
                self.deliver_answer(answer)
            except (CollectorError, KeyError, TypeError, ValueError, OSError):
                failed = True
        if failed:
            raise CollectorError("One or more answers could not be delivered or acknowledged.")

    def deliver_answer(self, answer: dict) -> None:
        if answer.get("lane", {}).get("source") != "t3":
            return
        ref = answer.get("source_ref")
        ask_id = str(answer["ask_id"])
        if ref not in self.state["asks"]:
            # Another collector can share this token. It owns its own answers.
            return
        if ask_id not in self.state["delivered"]:
            thread = self.source.get_thread(answer["lane"]["external_id"])
            pending = (
                next(
                    (item for item in thread.pending if f"t3:{thread.id}:{item.id}" == ref),
                    None,
                )
                if thread
                else None
            )
            note = "The request no longer waits."
            if pending and thread.state == "waiting_person":
                manual = self.state["asks"].get(ref, {}).get("manual", False)
                if not manual:
                    via = answer.get("answered_via")
                    if via not in {"person", "rule", "approved_draft"}:
                        raise CollectorError(
                            "The answer needs a person, an approved draft, or a rule."
                        )
                    if answer.get("answered_via") == "rule" and answer.get("answer_text"):
                        raise CollectorError("A rule answer cannot add free instructions.")
                    option = answer.get("answer_option")
                    proposed = {
                        item["id"]: item["label"] for item in self.ask(thread, pending)["options"]
                    }
                    if option and proposed.get(option.get("id")) != option.get("label"):
                        raise CollectorError("The answer does not match the proposed options.")
                    if not option and (
                        via not in {"person", "approved_draft"} or not answer.get("answer_text")
                    ):
                        raise CollectorError("Only a person can give a free answer.")
                    person = answer.get("answered_by") or self.config.owner or "owner"
                    if isinstance(person, dict):
                        person = person.get("name") or person.get("key") or "person"
                    text = answer.get("answer_text") or ""
                    if answer.get("answered_via") == "rule":
                        if not answer.get("rule_text"):
                            raise CollectorError("The rule answer must name its rule.")
                        text = "Rule: " + answer["rule_text"]
                    origin = {
                        "rule": f"Answer by rule of {person}",
                        "person": f"Answer from {person}",
                        "approved_draft": f"Answer approved by {person}",
                    }[via]
                    message = (
                        f"{origin} (via Illo): {option['label'] if option else 'Answer'}. {text}"
                    ).rstrip()
                    self.source.deliver(thread, pending, answer, message)
                    note = "Delivered."
                else:
                    note = "The person must answer in the thread."
            self.state["delivered"][ask_id] = note
            self.save()
        self.mcp.call("ask_ack", {"ask_id": answer["ask_id"]})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    try:
        config = Config.load(args.config)
        config.state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with config.state_file.with_suffix(".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CollectorError("Another collector uses this state file.") from None
            source = T3Source(config)
            mcp = MCPClient(config.mcp_url, read_token(config.token_file))
            collector = Collector(config, source, mcp)
            while True:
                try:
                    collector.poll()
                except (CollectorError, KeyError, TypeError, ValueError, OSError):
                    print(
                        "The collector poll failed. Check the config and the local sources.",
                        file=sys.stderr,
                    )
                    if args.once:
                        return 1
                if args.once:
                    return 0
                time.sleep(config.poll_seconds)
    except (CollectorError, OSError, ValueError, TypeError) as error:
        print(
            str(error) if isinstance(error, CollectorError) else "The collector setup failed.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

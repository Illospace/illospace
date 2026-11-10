#!/usr/bin/env python3
"""Report local lanes and pull answers. Illo never connects to this machine."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
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


def lane_state(status: str, waiting: bool = False, settled: bool = False) -> str:
    if waiting:
        return "waiting_person"
    if status == "running":
        return "working"
    if status == "failed":
        return "failed"
    if status in {"completed", "finished", "settled"} or settled:
        return "done"
    if status in {"waiting", "waiting_input", "waiting_approval"}:
        return "waiting_person"
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
        state = lane_state(status, bool(pending), row.get("settledOverride") == "settled")
        if row.get("blockedOnThreadWait") and not pending:
            state = "unknown"
        if state == "waiting_person" and not pending:
            text = latest.get("text", "")
            # A stable message ID keeps this fallback ask separate from the next question.
            if text:
                pending.append(Pending("message-" + latest["id"], text))
        return Thread(
            thread_id,
            row["title"],
            state,
            latest.get("text", ""),
            link,
            row["projectId"],
            row.get("updatedAt", ""),
            pending,
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
                        "row_number() OVER (PARTITION BY thread_id ORDER BY ordinal DESC) AS position "
                        "FROM orchestration_v2_projection_runs) WHERE position=1"
                    )
                )
                projections = {}
                queries = {
                    "messages": "SELECT thread_id, payload_json FROM (SELECT thread_id, payload_json, "
                    "row_number() OVER (PARTITION BY thread_id ORDER BY updated_at DESC) AS position "
                    "FROM orchestration_v2_projection_messages WHERE role='assistant') WHERE position=1",
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
                result = []
                for (payload,) in rows:
                    row = json.loads(payload)
                    if not self._selected(row):
                        continue
                    thread_id = row["id"]
                    row["status"] = runs.get(thread_id, "unknown")
                    result.append(self._thread(row, projections.get(thread_id, {})))
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
            "links": [thread.link],
            "repo": self.config.projects.get(thread.project),
            "machine": self.config.machine,
        }
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
        self.mcp.call(
            "lane_report",
            {
                "lanes": changed,
                "collector": {
                    "label": self.config.machine,
                    "version": VERSION,
                    "threads_seen": len(threads),
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
                    if via not in {"person", "rule"}:
                        raise CollectorError("The answer must come from a person or a rule.")
                    if answer.get("answered_via") == "rule" and answer.get("answer_text"):
                        raise CollectorError("A rule answer cannot add free instructions.")
                    option = answer.get("answer_option")
                    proposed = {
                        item["id"]: item["label"] for item in self.ask(thread, pending)["options"]
                    }
                    if option and proposed.get(option.get("id")) != option.get("label"):
                        raise CollectorError("The answer does not match the proposed options.")
                    if not option and (via != "person" or not answer.get("answer_text")):
                        raise CollectorError("Only a person can give a free answer.")
                    person = answer.get("answered_by") or self.config.owner or "owner"
                    if isinstance(person, dict):
                        person = person.get("name") or person.get("key") or "person"
                    text = answer.get("answer_text") or ""
                    if answer.get("answered_via") == "rule":
                        if not answer.get("rule_text"):
                            raise CollectorError("The rule answer must name its rule.")
                        text = "Rule: " + answer["rule_text"]
                    message = (
                        f"Answer from {person} (via Illo): {option['label'] if option else 'Answer'}. {text}"
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

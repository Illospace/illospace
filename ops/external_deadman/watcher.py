"""Dependency-free external watcher with at-least-once Slack delivery.

Alarm and recovery state is persisted only after Slack accepts the notice. This
biases failures toward another delivery instead of suppressing the incident:
if Slack succeeds but the state write fails, the next watcher run reposts the
notice. Every alarm and its recovery carry the same stable outage ID so an
operator can recognize those deliveries as belonging to one outage.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


GITHUB_API_BASE = "https://api.github.com"
DEFAULT_REPO = "Illospace/illospace"
DEFAULT_BRANCH = "ops/heartbeat"
HEARTBEAT_PATH = "heartbeat.json"
STATE_PATH = "deadman-state.json"
STALE_AFTER = timedelta(minutes=12)
_KNOWN_SURFACES = {
    "ai_timeline",
    "api",
    "cortex",
    "headless",
    "illo",
    "mcp",
    "scheduler",
    "slack",
    "thread_discussion",
    "unknown",
}


@dataclass(frozen=True)
class Heartbeat:
    ts: datetime
    last_run_id: int | None
    last_surface: str


@dataclass(frozen=True)
class DeadmanState:
    alarmed: bool = False
    missing_since: datetime | None = None
    outage_id: str | None = None


@dataclass(frozen=True)
class DeadmanDecision:
    action: str
    message: str | None
    state: DeadmanState


class WatcherError(RuntimeError):
    """A watcher failure safe to print without leaking response bodies."""


class GitHubStoreError(WatcherError):
    def __init__(self, status_code: int, operation: str):
        super().__init__(f"GitHub returned {status_code} while {operation}")
        self.status_code = status_code


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_z(value: datetime) -> str:
    return _utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_datetime(value: Any, *, field: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def parse_heartbeat(payload: dict[str, Any]) -> Heartbeat:
    raw_run_id = payload.get("last_run_id")
    if raw_run_id is None:
        run_id = None
    elif isinstance(raw_run_id, bool):
        raise ValueError("last_run_id must be an integer or null")
    else:
        try:
            run_id = int(raw_run_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("last_run_id must be an integer or null") from exc
        if run_id < 1:
            raise ValueError("last_run_id must be positive")
    surface = str(payload.get("last_surface") or "unknown").strip().lower()
    if surface not in _KNOWN_SURFACES:
        surface = "unknown"
    return Heartbeat(
        ts=_parse_datetime(payload.get("ts"), field="ts"),
        last_run_id=run_id,
        last_surface=surface,
    )


def parse_state(payload: dict[str, Any] | None) -> DeadmanState:
    if payload is None:
        return DeadmanState()
    alarmed = payload.get("alarmed", False)
    if not isinstance(alarmed, bool):
        raise ValueError("alarmed must be a boolean")
    missing_since = payload.get("missing_since")
    raw_outage_id = payload.get("outage_id")
    if raw_outage_id is not None and not isinstance(raw_outage_id, str):
        raise ValueError("outage_id must be a string or null")
    outage_id = str(raw_outage_id or "").strip() or None
    return DeadmanState(
        alarmed=alarmed,
        missing_since=(
            _parse_datetime(missing_since, field="missing_since")
            if missing_since
            else None
        ),
        outage_id=outage_id,
    )


def serialize_state(state: DeadmanState) -> dict[str, Any]:
    return {
        "alarmed": state.alarmed,
        "missing_since": _utc_z(state.missing_since) if state.missing_since else None,
        "outage_id": state.outage_id,
    }


def _activity(heartbeat: Heartbeat | None) -> str:
    if heartbeat is None or heartbeat.last_run_id is None:
        return "no run recorded via unknown"
    return f"run {heartbeat.last_run_id} via {heartbeat.last_surface}"


def _outage_id(heartbeat: Heartbeat | None, state: DeadmanState) -> str:
    source = heartbeat.ts if heartbeat is not None else state.missing_since
    if source is None:
        return "missing-heartbeat-unknown"
    prefix = "heartbeat" if heartbeat is not None else "missing-heartbeat"
    timestamp = _utc(source).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{timestamp}"


def evaluate_deadman(
    heartbeat: Heartbeat | None,
    state: DeadmanState,
    *,
    now: datetime,
    stale_after: timedelta = STALE_AFTER,
) -> DeadmanDecision:
    """Apply fresh/stale/alarm/recovery transitions without network access."""
    clock = _utc(now)
    if stale_after.total_seconds() <= 0:
        raise ValueError("stale_after must be positive")

    if heartbeat is None:
        missing_since = state.missing_since or clock
        effective_state = replace(state, missing_since=missing_since)
        stale = clock - missing_since >= stale_after
        last_seen = "never"
    else:
        effective_state = replace(state, missing_since=None)
        stale = clock - heartbeat.ts >= stale_after
        last_seen = _utc_z(heartbeat.ts)

    if stale and not effective_state.alarmed:
        outage_id = _outage_id(heartbeat, effective_state)
        next_state = replace(effective_state, alarmed=True, outage_id=outage_id)
        message = (
            f":rotating_light: Illo external deadman: no fresh heartbeat; "
            f"last seen {last_seen}. Last known activity: {_activity(heartbeat)}. "
            f"Outage ID: {outage_id}."
        )
        return DeadmanDecision("alarm", message, next_state)

    if heartbeat is not None and not stale and effective_state.alarmed:
        outage_id = effective_state.outage_id or "legacy-unidentified-outage"
        next_state = replace(effective_state, alarmed=False, outage_id=None)
        message = (
            f":white_check_mark: Illo external deadman recovery: heartbeat resumed at "
            f"{_utc_z(heartbeat.ts)}. Last known activity: {_activity(heartbeat)}. "
            f"Recovered outage ID: {outage_id}."
        )
        return DeadmanDecision("recovery", message, next_state)

    return DeadmanDecision("none", None, effective_state)


class GitHubFileStore:
    def __init__(self, *, repo: str, branch: str, token: str | None):
        self.repo = repo
        self.branch = branch
        self.token = token

    def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        payload: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
        allow_not_found: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        url = f"{GITHUB_API_BASE}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "illo-external-deadman",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=15) as response:
                raw = response.read()
                decoded = json.loads(raw) if raw else {}
                return response.status, decoded if isinstance(decoded, dict) else {}
        except HTTPError as exc:
            if exc.code == 404 and allow_not_found:
                return exc.code, {}
            raise GitHubStoreError(exc.code, operation) from None
        except (URLError, TimeoutError) as exc:
            raise WatcherError(f"Could not reach GitHub while {operation}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WatcherError(f"GitHub returned invalid JSON while {operation}") from exc

    def read_json(self, path: str) -> tuple[dict[str, Any] | None, str | None]:
        encoded_path = quote(path, safe="/")
        _, payload = self._request(
            "GET",
            f"/repos/{self.repo}/contents/{encoded_path}",
            operation=f"reading {path}",
            query={"ref": self.branch},
            allow_not_found=True,
        )
        if not payload:
            return None, None
        if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
            raise WatcherError(f"GitHub returned an unsupported encoding for {path}")
        try:
            raw = base64.b64decode(payload["content"], validate=False)
            decoded = json.loads(raw)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WatcherError(f"{path} is not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise WatcherError(f"{path} must contain a JSON object")
        return decoded, str(payload.get("sha") or "") or None

    def _branch_exists(self) -> bool:
        encoded_ref = quote(f"heads/{self.branch}", safe="/")
        _, payload = self._request(
            "GET",
            f"/repos/{self.repo}/git/ref/{encoded_ref}",
            operation="reading the heartbeat branch",
            allow_not_found=True,
        )
        return bool(payload)

    @staticmethod
    def _required_sha(payload: dict[str, Any], *, operation: str) -> str:
        sha = str(payload.get("sha") or "").strip()
        if not sha:
            raise GitHubStoreError(502, operation)
        return sha

    def _create_orphan(self, path: str, content: bytes) -> None:
        encoded = base64.b64encode(content).decode("ascii")
        _, blob = self._request(
            "POST",
            f"/repos/{self.repo}/git/blobs",
            operation="creating the deadman state blob",
            payload={"content": encoded, "encoding": "base64"},
        )
        _, tree = self._request(
            "POST",
            f"/repos/{self.repo}/git/trees",
            operation="creating the deadman state tree",
            payload={
                "tree": [
                    {
                        "path": path,
                        "mode": "100644",
                        "type": "blob",
                        "sha": self._required_sha(
                            blob,
                            operation="reading the deadman state blob response",
                        ),
                    }
                ]
            },
        )
        _, commit = self._request(
            "POST",
            f"/repos/{self.repo}/git/commits",
            operation="creating the orphan deadman state commit",
            payload={
                "message": "ops: initialize Illo deadman state",
                "tree": self._required_sha(
                    tree,
                    operation="reading the deadman state tree response",
                ),
                "parents": [],
            },
        )
        self._request(
            "POST",
            f"/repos/{self.repo}/git/refs",
            operation="creating the heartbeat branch",
            payload={
                "ref": f"refs/heads/{self.branch}",
                "sha": self._required_sha(
                    commit,
                    operation="reading the deadman state commit response",
                ),
            },
        )

    def write_json(self, path: str, payload: dict[str, Any]) -> None:
        content = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        encoded = base64.b64encode(content).decode("ascii")
        encoded_path = quote(path, safe="/")
        for attempt in range(3):
            if not self._branch_exists():
                try:
                    self._create_orphan(path, content)
                    return
                except GitHubStoreError as exc:
                    if exc.status_code == 422 and attempt < 2:
                        continue
                    raise

            _, existing_sha = self.read_json(path)
            body: dict[str, Any] = {
                "message": "ops: update Illo deadman state",
                "content": encoded,
                "branch": self.branch,
            }
            if existing_sha:
                body["sha"] = existing_sha
            try:
                self._request(
                    "PUT",
                    f"/repos/{self.repo}/contents/{encoded_path}",
                    operation="updating the deadman state",
                    payload=body,
                )
                return
            except GitHubStoreError as exc:
                if exc.status_code in {409, 422} and attempt < 2:
                    continue
                raise
        raise WatcherError("Could not update deadman state after retries")


def _post_slack(url: str, message: str) -> None:
    request = Request(
        url,
        data=json.dumps({"text": message}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "illo-external-deadman"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            if not 200 <= response.status < 300:
                raise WatcherError(f"Slack returned {response.status} while posting the deadman notice")
    except HTTPError as exc:
        raise WatcherError(f"Slack returned {exc.code} while posting the deadman notice") from None
    except (URLError, TimeoutError) as exc:
        raise WatcherError("Could not reach Slack while posting the deadman notice") from exc


def _ping_self_monitor(url: str | None) -> None:
    if not url:
        print(
            "::warning title=Deadman self-visibility unconfigured::"
            "ILLO_DEADMAN_HEALTHCHECK_URL is absent; this watcher has no external monitor.",
            file=sys.stderr,
        )
        return
    request = Request(url, headers={"User-Agent": "illo-external-deadman"}, method="GET")
    try:
        with urlopen(request, timeout=15) as response:
            if not 200 <= response.status < 300:
                raise WatcherError(
                    f"Self-monitor returned {response.status} while recording watcher success"
                )
    except HTTPError as exc:
        raise WatcherError(
            f"Self-monitor returned {exc.code} while recording watcher success"
        ) from None
    except (URLError, TimeoutError) as exc:
        raise WatcherError("Could not reach the configured deadman self-monitor") from exc


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def run() -> int:
    now = datetime.now(timezone.utc)
    dry_run = _env_flag("ILLO_DEADMAN_DRY_RUN")
    force_stale = _env_flag("ILLO_DEADMAN_FORCE_STALE")
    github_token = os.getenv("GITHUB_TOKEN", "").strip() or None
    slack_webhook = os.getenv("SLACK_DEADMAN_WEBHOOK_URL", "").strip()
    self_monitor_url = os.getenv("ILLO_DEADMAN_HEALTHCHECK_URL", "").strip() or None
    if not dry_run and not github_token:
        raise WatcherError("GITHUB_TOKEN is required to persist deadman delivery state")
    if not dry_run and not slack_webhook:
        raise WatcherError(
            "SLACK_DEADMAN_WEBHOOK_URL is required even while the heartbeat is fresh"
        )

    store = GitHubFileStore(
        repo=os.getenv("GITHUB_REPOSITORY", DEFAULT_REPO),
        branch=DEFAULT_BRANCH,
        token=github_token,
    )
    heartbeat_payload, _ = store.read_json(HEARTBEAT_PATH)
    heartbeat = parse_heartbeat(heartbeat_payload) if heartbeat_payload is not None else None
    state_payload, _ = store.read_json(STATE_PATH)
    state = DeadmanState() if dry_run else parse_state(state_payload)

    if force_stale:
        forced_ts = now - STALE_AFTER - timedelta(seconds=1)
        heartbeat = (
            replace(heartbeat, ts=forced_ts)
            if heartbeat is not None
            else Heartbeat(forced_ts, None, "unknown")
        )

    decision = evaluate_deadman(heartbeat, state, now=now)
    print(
        json.dumps(
            {
                "action": decision.action,
                "dry_run": dry_run,
                "force_stale": force_stale,
                "message": decision.message,
            },
            sort_keys=True,
        )
    )
    if dry_run:
        if decision.action == "alarm":
            print(f"::notice title=Deadman dry-run alarm::{decision.message}")
        return 0

    # Deliberately post before persisting the transition. A Slack failure leaves
    # the old state for a retry, and a later state-write failure can only cause a
    # duplicate delivery with the same outage ID, never a silently lost notice.
    if decision.message is not None:
        _post_slack(slack_webhook, decision.message)
    if decision.state != state:
        store.write_json(STATE_PATH, serialize_state(decision.state))
    _ping_self_monitor(self_monitor_url)
    return 0


def main() -> int:
    try:
        return run()
    except (WatcherError, ValueError) as exc:
        print(f"Illo external deadman failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

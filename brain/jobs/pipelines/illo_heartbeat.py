"""Publish Illo's minimal heartbeat to a public orphan branch."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import func, select

from brain.platform.async_io import async_http_client
from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.models.org import User
from brain.platform.db.models.vault import Secret, VaultProjectBinding
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.cortex.project_context.github import GITHUB_API_BASE
from brain.systems.vault import async_resolve_project_bound_env_tokens


logger = logging.getLogger("illo.jobs.illo_heartbeat")

REPO_SLUG = "Illospace/illospace"
PROJECT_SLUG = REPO_SLUG.lower()
HEARTBEAT_BRANCH = "ops/heartbeat"
HEARTBEAT_PATH = "heartbeat.json"
HEARTBEAT_INSTALLATION_PERMISSIONS = {"contents": "write"}
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
}


@dataclass(frozen=True)
class HeartbeatActor:
    user_id: str
    org_id: str


class HeartbeatGitHubError(RuntimeError):
    """A safe GitHub transport error that never includes response bodies."""

    def __init__(self, status_code: int, operation: str):
        super().__init__(f"GitHub returned {status_code} while {operation}")
        self.status_code = status_code


def _utc_z(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _coarse_surface(run: AgentRunRow | Any | None) -> str:
    if run is None:
        return "unknown"
    metadata = run.metadata_ if isinstance(getattr(run, "metadata_", None), dict) else {}
    target_ref = run.target_ref if isinstance(getattr(run, "target_ref", None), dict) else {}
    for key in (
        "originating_surface",
        "source_surface",
        "triggering_surface",
        "origin",
    ):
        value = str(metadata.get(key) or target_ref.get(key) or "").strip().lower()
        if value in _KNOWN_SURFACES:
            return value
    return "unknown"


def build_heartbeat_payload(
    latest_run: AgentRunRow | Any | None,
    *,
    now: datetime | None = None,
) -> dict[str, str | int | None]:
    """Build the complete public payload; no other fields may be emitted."""
    clock = now or datetime.now(timezone.utc)
    run_id = getattr(latest_run, "id", None)
    return {
        "ts": _utc_z(clock),
        "last_run_id": int(run_id) if run_id is not None else None,
        "last_surface": _coarse_surface(latest_run),
    }


async def _heartbeat_actor() -> HeartbeatActor | None:
    async with UnitOfWork() as uow:
        binding = await uow.session.scalar(
            select(VaultProjectBinding)
            .join(Secret, Secret.id == VaultProjectBinding.secret_id)
            .where(
                func.lower(VaultProjectBinding.project_slug) == PROJECT_SLUG,
                VaultProjectBinding.active.is_(True),
                Secret.category == "github_app",
            )
            .order_by(VaultProjectBinding.id.asc())
            .limit(1)
        )
        if binding is None:
            return None

        actor = None
        if binding.created_by_user_id:
            actor = await uow.session.get(User, str(binding.created_by_user_id))
        if actor is None:
            actor = (
                await uow.session.scalars(
                    select(User)
                    .where(User.org_id == str(binding.org_id))
                    .order_by(User.created_at.asc(), User.id.asc())
                    .limit(1)
                )
            ).first()
        if actor is None:
            return None
        return HeartbeatActor(user_id=str(actor.id), org_id=str(actor.org_id))


async def _heartbeat_token(actor: HeartbeatActor) -> str | None:
    env = await async_resolve_project_bound_env_tokens(
        actor_user_id=actor.user_id,
        org_id=actor.org_id,
        project_slug=PROJECT_SLUG,
        github_app_only=True,
        github_app_permissions=HEARTBEAT_INSTALLATION_PERMISSIONS,
    )
    return str(env.get("GITHUB_TOKEN") or env.get("GH_TOKEN") or "").strip() or None


async def _latest_run() -> AgentRunRow | None:
    async with UnitOfWork() as uow:
        return await uow.session.scalar(
            select(AgentRunRow)
            .order_by(AgentRunRow.created_at.desc(), AgentRunRow.id.desc())
            .limit(1)
        )


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "illo-external-heartbeat",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    token: str,
    operation: str,
    json_payload: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
    allow_not_found: bool = False,
) -> tuple[int, dict[str, Any]]:
    try:
        response = await client.request(
            method,
            f"{GITHUB_API_BASE}{path}",
            headers=_headers(token),
            json=json_payload,
            params=params,
        )
    except httpx.HTTPError as exc:
        raise HeartbeatGitHubError(502, operation) from exc
    if response.status_code == 404 and allow_not_found:
        return response.status_code, {}
    if not response.is_success:
        raise HeartbeatGitHubError(response.status_code, operation)
    try:
        payload = response.json()
    except ValueError as exc:
        raise HeartbeatGitHubError(502, operation) from exc
    return response.status_code, payload if isinstance(payload, dict) else {}


def _required_sha(payload: dict[str, Any], *, operation: str) -> str:
    sha = str(payload.get("sha") or "").strip()
    if not sha:
        raise HeartbeatGitHubError(502, operation)
    return sha


async def _create_orphan_branch(
    client: httpx.AsyncClient,
    *,
    token: str,
    content: bytes,
) -> None:
    encoded = base64.b64encode(content).decode("ascii")
    _, blob = await _request(
        client,
        "POST",
        f"/repos/{REPO_SLUG}/git/blobs",
        token=token,
        operation="creating the heartbeat blob",
        json_payload={"content": encoded, "encoding": "base64"},
    )
    _, tree = await _request(
        client,
        "POST",
        f"/repos/{REPO_SLUG}/git/trees",
        token=token,
        operation="creating the heartbeat tree",
        json_payload={
            "tree": [
                {
                    "path": HEARTBEAT_PATH,
                    "mode": "100644",
                    "type": "blob",
                    "sha": _required_sha(blob, operation="reading the heartbeat blob response"),
                }
            ]
        },
    )
    _, commit = await _request(
        client,
        "POST",
        f"/repos/{REPO_SLUG}/git/commits",
        token=token,
        operation="creating the orphan heartbeat commit",
        json_payload={
            "message": "ops: initialize Illo heartbeat",
            "tree": _required_sha(tree, operation="reading the heartbeat tree response"),
            "parents": [],
        },
    )
    await _request(
        client,
        "POST",
        f"/repos/{REPO_SLUG}/git/refs",
        token=token,
        operation="creating the heartbeat branch",
        json_payload={
            "ref": f"refs/heads/{HEARTBEAT_BRANCH}",
            "sha": _required_sha(commit, operation="reading the heartbeat commit response"),
        },
    )


async def publish_heartbeat(payload: dict[str, str | int | None], *, token: str) -> None:
    content = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    encoded = base64.b64encode(content).decode("ascii")
    file_path = quote(HEARTBEAT_PATH, safe="/")
    ref_path = quote(f"heads/{HEARTBEAT_BRANCH}", safe="/")

    async with async_http_client(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
        for attempt in range(3):
            _, ref = await _request(
                client,
                "GET",
                f"/repos/{REPO_SLUG}/git/ref/{ref_path}",
                token=token,
                operation="reading the heartbeat branch",
                allow_not_found=True,
            )
            if not ref:
                try:
                    await _create_orphan_branch(client, token=token, content=content)
                    return
                except HeartbeatGitHubError as exc:
                    if exc.status_code == 422 and attempt < 2:
                        continue
                    raise

            _, existing = await _request(
                client,
                "GET",
                f"/repos/{REPO_SLUG}/contents/{file_path}",
                token=token,
                operation="reading the heartbeat file",
                params={"ref": HEARTBEAT_BRANCH},
                allow_not_found=True,
            )
            body: dict[str, Any] = {
                "message": "ops: update Illo heartbeat",
                "content": encoded,
                "branch": HEARTBEAT_BRANCH,
            }
            if existing.get("sha"):
                body["sha"] = existing["sha"]
            try:
                await _request(
                    client,
                    "PUT",
                    f"/repos/{REPO_SLUG}/contents/{file_path}",
                    token=token,
                    operation="updating the heartbeat file",
                    json_payload=body,
                )
                return
            except HeartbeatGitHubError as exc:
                if exc.status_code in {409, 422} and attempt < 2:
                    continue
                raise
    raise HeartbeatGitHubError(409, "updating the heartbeat after retries")


async def run_heartbeat(*, now: datetime | None = None) -> dict[str, Any]:
    actor = await _heartbeat_actor()
    if actor is None:
        return {
            "job": "illo_external_heartbeat",
            "ok": True,
            "outcome": "skipped",
            "reason": f"No GitHub App project binding is configured for {PROJECT_SLUG}",
        }
    token = await _heartbeat_token(actor)
    if token is None:
        return {
            "job": "illo_external_heartbeat",
            "ok": True,
            "outcome": "skipped",
            "reason": f"No project-bound GitHub token is available for {PROJECT_SLUG}",
        }

    payload = build_heartbeat_payload(await _latest_run(), now=now)
    await publish_heartbeat(payload, token=token)
    return {
        "job": "illo_external_heartbeat",
        "ok": True,
        "outcome": "published",
        "payload": payload,
    }


async def async_main() -> int:
    try:
        result = await run_heartbeat()
    except Exception as exc:  # noqa: BLE001 - scheduler needs a settled failure
        logger.error("External heartbeat failed: %s", exc)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())

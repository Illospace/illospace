#!/usr/bin/env python3
"""Normalize deploy-tracker fix PRs and backfill merged commit SHAs.

The default mode is a dry run. Pass ``--apply`` to write the proposed record
patches through the domain-record service. GitHub access is read-only and uses
the org's project-bound GitHub App credential.

Usage:
    venv/bin/python scripts/backfill_deploy_fix_refs.py \
        --org-id <org-uuid> --actor-user-id <user-uuid>
    venv/bin/python scripts/backfill_deploy_fix_refs.py \
        --org-id <org-uuid> --actor-user-id <user-uuid> --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.cortex.project_context.github import (
    async_get_pull_request_deploy_info,
)
from brain.systems.deploy_state import DeployState
from brain.systems.deploy_state_sweep import (
    append_progress_note,
    ensure_deploy_state_fields,
    fix_pr_from_text,
    github_repo_from_issue_text,
    ticket_records,
    update_record,
)
from brain.systems.vault import async_resolve_project_bound_env_tokens


PullRequestLookup = Callable[[str, int], Awaitable[Mapping[str, Any]]]

_ENUMERATED_STATES = {
    DeployState.STAGING.value,
    DeployState.PROD_PENDING.value,
    DeployState.DEPLOYED.value,
}
_SHA_BACKFILL_STATES = {
    DeployState.STAGING.value,
    DeployState.PROD_PENDING.value,
}
_MERGE_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_NEEDS_HUMAN_NOTE = "needs-human: no fix PR reference"
_READ_ONLY_GITHUB_APP_PERMISSIONS = {"pull_requests": "read"}
_ALLOWED_PATCH_FIELDS = {"fix_pr", "fix_merge_sha", "progress_note"}


def github_app_pull_request_lookup(
    *,
    org_id: str,
    actor_user_id: str,
) -> PullRequestLookup:
    """Build a cached, read-only GitHub App PR lookup for one org."""

    tokens_by_repo: dict[str, str] = {}

    async def lookup(repo: str, number: int) -> Mapping[str, Any]:
        token = tokens_by_repo.get(repo)
        if token is None:
            bound_tokens = await async_resolve_project_bound_env_tokens(
                actor_user_id=actor_user_id,
                org_id=org_id,
                project_slug=repo,
                github_app_only=True,
                github_app_permissions=_READ_ONLY_GITHUB_APP_PERMISSIONS,
            )
            token = next((value for value in bound_tokens.values() if value), None)
            if token is None:
                raise RuntimeError(f"No project-bound GitHub App token for {repo}")
            tokens_by_repo[repo] = token
        return await async_get_pull_request_deploy_info(repo, number, token=token)

    return lookup


def _pull_request_detail(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    detail = payload.get("pull_request")
    return detail if isinstance(detail, Mapping) else payload


def _progress_note_patch(data: Mapping[str, object]) -> dict[str, object]:
    current_lines = {
        line.strip()
        for line in str(data.get("progress_note") or "").splitlines()
        if line.strip()
    }
    if _NEEDS_HUMAN_NOTE in current_lines:
        return {}
    note = append_progress_note(data, _NEEDS_HUMAN_NOTE)
    return {"progress_note": note} if note is not None else {}


async def backfill_deploy_fix_refs(
    session,
    *,
    org_id: str,
    apply: bool = False,
    pull_request_lookup: PullRequestLookup | None = None,
    actor_user_id: str | None = None,
) -> dict[str, object]:
    """Build and optionally apply the deploy fix-reference backfill."""

    if apply:
        await ensure_deploy_state_fields(session, org_id=org_id)
    records = await ticket_records(
        session,
        org_id=org_id,
        states=_ENUMERATED_STATES,
    )
    lookup = pull_request_lookup
    rows: list[dict[str, object]] = []

    for record in records:
        data = record.data or {}
        state = str(data.get("deploy_state") or "").strip()
        old_fix_pr = (
            None
            if data.get("fix_pr") is None
            else str(data.get("fix_pr"))
        )
        title_repo = github_repo_from_issue_text(record.title)
        canonical_fix_pr = fix_pr_from_text(
            str(data.get("fix_pr") or ""),
            repo=title_repo,
        )
        patch: dict[str, object] = {}
        sha_found = False
        unresolvable_reason: str | None = None

        if canonical_fix_pr is None:
            unresolvable_reason = "no fix PR reference"
            if state in _SHA_BACKFILL_STATES:
                patch.update(_progress_note_patch(data))
        else:
            if old_fix_pr != canonical_fix_pr:
                patch["fix_pr"] = canonical_fix_pr
            if state in _SHA_BACKFILL_STATES:
                if lookup is None:
                    if not actor_user_id:
                        raise ValueError(
                            "actor_user_id is required when pull_request_lookup is not supplied"
                        )
                    lookup = github_app_pull_request_lookup(
                        org_id=org_id,
                        actor_user_id=actor_user_id,
                    )
                repo, number_text = canonical_fix_pr.rsplit("#", 1)
                try:
                    payload = await lookup(repo, int(number_text))
                except Exception as exc:
                    unresolvable_reason = f"github lookup failed: {exc}"
                else:
                    detail = _pull_request_detail(payload)
                    if detail.get("merged_at"):
                        merge_sha = str(detail.get("merge_commit_sha") or "").strip()
                        if _MERGE_SHA_RE.fullmatch(merge_sha):
                            sha_found = True
                            if data.get("fix_merge_sha") != merge_sha:
                                patch["fix_merge_sha"] = merge_sha
                        else:
                            unresolvable_reason = (
                                "merged PR has no valid merge_commit_sha"
                            )

        rows.append(
            {
                "record_id": record.id,
                "old_fix_pr": old_fix_pr,
                "new_fix_pr": canonical_fix_pr or old_fix_pr,
                "sha_found": sha_found,
                "unresolvable_reason": unresolvable_reason,
            }
        )
        unexpected_fields = set(patch) - _ALLOWED_PATCH_FIELDS
        if unexpected_fields:
            raise AssertionError(
                f"Backfill patch escaped scope: {sorted(unexpected_fields)}"
            )
        if apply and patch:
            await update_record(
                session,
                record,
                patch,
                reason="deploy_backfill",
            )

    return {"applied": apply, "records": rows}


async def _run(
    *,
    org_id: str,
    actor_user_id: str,
    apply: bool,
) -> dict[str, object]:
    async with UnitOfWork() as uow:
        return await backfill_deploy_fix_refs(
            uow.session,
            org_id=org_id,
            actor_user_id=actor_user_id,
            apply=apply,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--org-id",
        required=True,
        help="Organization UUID that owns the deploy tracker",
    )
    parser.add_argument(
        "--actor-user-id",
        required=True,
        help="User UUID used to audit access to the org's GitHub App credential",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write proposed record patches (default: dry run)",
    )
    args = parser.parse_args()
    report = asyncio.run(
        _run(
            org_id=args.org_id,
            actor_user_id=args.actor_user_id,
            apply=args.apply,
        )
    )
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()

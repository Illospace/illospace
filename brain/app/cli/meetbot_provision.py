"""Provision the external-source connection used by meetbot callbacks."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from sqlalchemy import select

from brain.platform.db.models.org import User
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.external_agents import service as external_agents


DISPLAY_NAME = "Meetbot"
AGENT_KIND = "meetbot"
TRANSPORT = "webhook"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m brain.app.cli.meetbot_provision",
        description=(
            "Ensure the meetbot webhook connection and mint a signal:submit bridge token."
        ),
    )
    parser.add_argument(
        "--org-id",
        default=os.getenv("ILLO_MEETBOT_ORG_ID") or os.getenv("ILLO_ORG_ID"),
        help="Illospace organization ID. Defaults to ILLO_MEETBOT_ORG_ID or ILLO_ORG_ID.",
    )
    parser.add_argument(
        "--owner-user-id",
        default=(
            os.getenv("ILLO_MEETBOT_OWNER_USER_ID")
            or os.getenv("ILLO_OWNER_USER_ID")
        ),
        help=(
            "Authority user ID. Defaults to ILLO_MEETBOT_OWNER_USER_ID or "
            "ILLO_OWNER_USER_ID."
        ),
    )
    return parser


async def provision(
    *,
    org_id: str | None = None,
    owner_user_id: str | None = None,
) -> dict[str, object]:
    async with UnitOfWork() as uow:
        resolved_org_id, resolved_owner_user_id = await _resolve_authority(
            uow.session,
            org_id=org_id,
            owner_user_id=owner_user_id,
        )
        connection = await external_agents.find_reusable_connection(
            uow.session,
            org_id=resolved_org_id,
            owner_user_id=resolved_owner_user_id,
            display_name=DISPLAY_NAME,
            agent_kind=AGENT_KIND,
            transport=TRANSPORT,
        )
        created = connection is None
        if connection is None:
            connection = await external_agents.create_connection(
                uow.session,
                org_id=resolved_org_id,
                owner_user_id=resolved_owner_user_id,
                display_name=DISPLAY_NAME,
                agent_kind=AGENT_KIND,
                transport=TRANSPORT,
                capabilities={"meeting_transcript": True},
                metadata={"callback_kind": "meeting_transcript"},
            )
        connection.status = "configured"
        connection.capabilities = {
            **dict(connection.capabilities or {}),
            "meeting_transcript": True,
        }
        connection.metadata_ = {
            **dict(connection.metadata_ or {}),
            "callback_kind": "meeting_transcript",
        }
        raw_token, token_row = await external_agents.mint_connection_token(
            uow.session,
            connection_id=str(connection.id),
            org_id=resolved_org_id,
            name="Meetbot callback token",
            scopes=[external_agents.SCOPE_SIGNAL_SUBMIT],
        )
        return {
            "connection_id": str(connection.id),
            "connection_created": created,
            "org_id": resolved_org_id,
            "owner_user_id": resolved_owner_user_id,
            "bridge_token": raw_token,
            "token_id": str(token_row.id),
            "scopes": [external_agents.SCOPE_SIGNAL_SUBMIT],
        }


async def _resolve_authority(
    session,
    *,
    org_id: str | None,
    owner_user_id: str | None,
) -> tuple[str, str]:
    normalized_org_id = str(org_id or "").strip() or None
    normalized_owner_user_id = str(owner_user_id or "").strip() or None
    if normalized_owner_user_id:
        user = await session.get(User, normalized_owner_user_id)
        if user is None:
            raise RuntimeError("Meetbot owner user was not found")
        if normalized_org_id and str(user.org_id) != normalized_org_id:
            raise RuntimeError("Meetbot owner user does not belong to the requested organization")
        return str(user.org_id), str(user.id)

    stmt = select(User)
    if normalized_org_id:
        stmt = stmt.where(User.org_id == normalized_org_id)
    stmt = stmt.order_by(User.created_at.asc(), User.id.asc()).limit(1)
    user = (await session.scalars(stmt)).first()
    if user is None:
        raise RuntimeError(
            "No Illospace user found; set --org-id and --owner-user-id after creating the owner"
        )
    return str(user.org_id), str(user.id)


async def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = await provision(
            org_id=args.org_id,
            owner_user_id=args.owner_user_id,
        )
    except Exception as exc:
        print(f"Meetbot provisioning failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))

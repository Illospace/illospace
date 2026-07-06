"""Hosted MCP identity resolution helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.external_agent import ExternalAgentConnectionRow
from brain.platform.db.models.org import User, UserCodexConnection
from brain.systems.external_agents import service as external_agents


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _provider(value: Any) -> str:
    return _clean(value).lower()


def _metadata(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _links_root(connection: ExternalAgentConnectionRow) -> dict[str, Any]:
    metadata = _metadata(connection.metadata_)
    links = metadata.get("identity_links")
    return dict(links) if isinstance(links, Mapping) else {}


def _set_links_root(connection: ExternalAgentConnectionRow, links: Mapping[str, Any]) -> None:
    metadata = _metadata(connection.metadata_)
    metadata["identity_links"] = dict(links)
    connection.metadata_ = metadata


def _identity_payload(
    *,
    provider: str,
    external_user_id: str,
    user_id: str,
    source: str,
    connection: ExternalAgentConnectionRow | None = None,
    display_name: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "provider": provider,
        "external_user_id": external_user_id,
        "user_id": user_id,
        "display_name": display_name,
        "source": source,
        "metadata": dict(metadata or {}),
    }
    if connection is not None:
        payload["connection_id"] = str(connection.id)
        payload["connection_display_name"] = connection.display_name
        payload["connection_agent_kind"] = connection.agent_kind
        payload["connection_transport"] = connection.transport
    return payload


def _connection_identity_links(connection: ExternalAgentConnectionRow) -> list[dict[str, Any]]:
    identities: list[dict[str, Any]] = []
    links = _links_root(connection)
    for provider, provider_links in links.items():
        if not isinstance(provider_links, Mapping):
            continue
        for external_user_id, raw_link in provider_links.items():
            link = _metadata(raw_link)
            user_id = _clean(link.get("user_id"))
            external_id = _clean(external_user_id)
            if not user_id or not external_id:
                continue
            identities.append(
                _identity_payload(
                    provider=_provider(provider),
                    external_user_id=external_id,
                    user_id=user_id,
                    display_name=_clean(link.get("display_name")) or None,
                    metadata=_metadata(link.get("metadata")),
                    source="external_connection.identity_links",
                    connection=connection,
                )
            )

    slack_metadata = _metadata(_metadata(connection.metadata_).get("slack"))
    slack_map = slack_metadata.get("identity_map")
    if isinstance(slack_map, Mapping):
        for slack_user_id, user_id in slack_map.items():
            clean_slack_user_id = _clean(slack_user_id)
            clean_user_id = _clean(user_id)
            if not clean_slack_user_id or not clean_user_id:
                continue
            identities.append(
                _identity_payload(
                    provider="slack",
                    external_user_id=clean_slack_user_id,
                    user_id=clean_user_id,
                    source="external_connection.slack.identity_map",
                    connection=connection,
                    metadata={
                        "team_id": slack_metadata.get("team_id"),
                        "bot_user_id": slack_metadata.get("bot_user_id"),
                    },
                )
            )
    return identities


def _connection_owner_identities(connection: ExternalAgentConnectionRow) -> list[dict[str, Any]]:
    owner_user_id = _clean(connection.owner_user_id)
    if not owner_user_id:
        return []
    external_user_id = _clean(connection.remote_agent_id) or _clean(connection.remote_session_key) or str(connection.id)
    return [
        _identity_payload(
            provider=_provider(connection.agent_kind) or "external_agent",
            external_user_id=external_user_id,
            user_id=owner_user_id,
            display_name=connection.display_name,
            source="external_connection.owner",
            connection=connection,
        )
    ]


def _matches_identity(
    identity: Mapping[str, Any],
    *,
    provider: str | None,
    external_user_id: str | None,
    user_id: str | None,
    query: str | None,
    users_by_id: Mapping[str, User],
) -> bool:
    if provider and _provider(identity.get("provider")) != provider:
        return False
    if external_user_id and _clean(identity.get("external_user_id")).lower() != external_user_id.lower():
        return False
    if user_id and _clean(identity.get("user_id")) != user_id:
        return False
    if query:
        user = users_by_id.get(_clean(identity.get("user_id")))
        haystack = " ".join(
            [
                _clean(identity.get("provider")),
                _clean(identity.get("external_user_id")),
                _clean(identity.get("display_name")),
                _clean(getattr(user, "name", None)),
                _clean(getattr(user, "email", None)),
            ]
        ).lower()
        if query.lower() not in haystack:
            return False
    return True


def _member_payload(user: User, identities: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": str(user.id),
        "user_id": str(user.id),
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "color": user.color,
        "identities": identities,
    }


async def resolve_identities(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    provider = _provider(arguments.get("provider")) or None
    external_user_id = _clean(arguments.get("external_user_id")) or None
    user_id = _clean(arguments.get("user_id")) or None
    query = _clean(arguments.get("query")) or None
    limit = max(1, min(int(arguments.get("limit") or 50), 200))

    users = list(
        (
            await db.scalars(
                select(User)
                .where(User.org_id == principal.org_id, User.approved.is_(True))
                .order_by(User.name.asc(), User.email.asc())
            )
        ).all()
    )
    users_by_id = {str(user.id): user for user in users}
    connections = list(
        (
            await db.scalars(
                select(ExternalAgentConnectionRow)
                .where(ExternalAgentConnectionRow.org_id == principal.org_id)
                .order_by(ExternalAgentConnectionRow.created_at.asc(), ExternalAgentConnectionRow.id.asc())
            )
        ).all()
    )
    codex_connections = list(
        (
            await db.scalars(
                select(UserCodexConnection).where(
                    UserCodexConnection.user_id.in_(list(users_by_id)),
                    UserCodexConnection.is_active.is_(True),
                )
            )
        ).all()
    )

    identities: list[dict[str, Any]] = []
    for user in users:
        identities.append(
            _identity_payload(
                provider="illo",
                external_user_id=str(user.id),
                user_id=str(user.id),
                display_name=user.name,
                source="user.id",
            )
        )
        identities.append(
            _identity_payload(
                provider="email",
                external_user_id=user.email,
                user_id=str(user.id),
                display_name=user.name,
                source="user.email",
            )
        )
    for connection in connections:
        identities.extend(_connection_owner_identities(connection))
        identities.extend(_connection_identity_links(connection))
    for connection in codex_connections:
        user = users_by_id.get(str(connection.user_id))
        identities.append(
            _identity_payload(
                provider="codex",
                external_user_id=str(connection.user_id),
                user_id=str(connection.user_id),
                display_name=connection.label or (user.name if user else None),
                source="user_codex_connections",
                metadata={"connection_id": connection.id},
            )
        )

    filtered = [
        identity
        for identity in identities
        if _matches_identity(
            identity,
            provider=provider,
            external_user_id=external_user_id,
            user_id=user_id,
            query=query,
            users_by_id=users_by_id,
        )
    ][:limit]
    identities_by_user: dict[str, list[dict[str, Any]]] = {str(user.id): [] for user in users}
    for identity in filtered:
        identities_by_user.setdefault(_clean(identity.get("user_id")), []).append(identity)
    return {
        "members": [
            _member_payload(user, identities_by_user.get(str(user.id), []))
            for user in users
            if not user_id or str(user.id) == user_id
        ],
        "identities": filtered,
    }


async def manage_identity(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    action = _provider(arguments.get("action"))
    if action not in {"link", "unlink"}:
        raise ValueError("identity.manage action must be link or unlink")
    connection_id = _clean(arguments.get("connection_id"))
    provider = _provider(arguments.get("provider"))
    external_user_id = _clean(arguments.get("external_user_id"))
    if not connection_id or not provider or not external_user_id:
        raise ValueError("identity.manage requires connection_id, provider, and external_user_id")

    connection = await db.get(ExternalAgentConnectionRow, connection_id)
    if connection is None or str(connection.org_id) != principal.org_id:
        raise ValueError("External source connection not found")

    links = _links_root(connection)
    provider_links = dict(links.get(provider) or {})
    removed = False
    if action == "unlink":
        removed = external_user_id in provider_links
        provider_links.pop(external_user_id, None)
    else:
        user_id = _clean(arguments.get("user_id"))
        if not user_id:
            raise ValueError("identity.manage action link requires user_id")
        user = await db.get(User, user_id)
        if user is None or str(user.org_id) != principal.org_id:
            raise ValueError("Illospace user not found")
        provider_links[external_user_id] = {
            "user_id": user_id,
            "display_name": _clean(arguments.get("display_name")) or None,
            "metadata": _metadata(arguments.get("metadata")),
        }
    if provider_links:
        links[provider] = provider_links
    else:
        links.pop(provider, None)
    _set_links_root(connection, links)

    if provider == "slack" and connection.agent_kind == "slack":
        metadata = _metadata(connection.metadata_)
        slack_metadata = _metadata(metadata.get("slack"))
        identity_map = dict(slack_metadata.get("identity_map") or {})
        if action == "unlink":
            identity_map.pop(external_user_id, None)
        else:
            identity_map[external_user_id] = _clean(arguments.get("user_id"))
        slack_metadata["identity_map"] = identity_map
        metadata["slack"] = slack_metadata
        connection.metadata_ = metadata

    await db.flush()
    return {
        "ok": True,
        "action": action,
        "removed": removed if action == "unlink" else None,
        "identity": None
        if action == "unlink"
        else _identity_payload(
            provider=provider,
            external_user_id=external_user_id,
            user_id=_clean(arguments.get("user_id")),
            display_name=_clean(arguments.get("display_name")) or None,
            metadata=_metadata(arguments.get("metadata")),
            source="external_connection.identity_links",
            connection=connection,
        ),
    }

"""Validate MCP configuration and commit changes with audit history."""
from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.external_mcp import ExternalMcpAuditEvent, ExternalMcpServer
from brain.platform.db.models.vault import Secret

CONFIG_FIELDS = (
    "name", "url", "transport", "auth_mode", "bearer_token_key", "token_url",
    "client_id_key", "client_secret_key", "scope", "resource", "allowed_tools",
    "enabled", "timeout_seconds",
)
_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128", "fc00::/7",
))
_BLOCKED_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "::/128", "169.254.0.0/16", "fe80::/10", "fd00:ec2::254/128",
))


async def validate_url(url: str) -> str:
    """Reject metadata targets and allow private HTTP only with an instance opt-in."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid MCP URL") from None
    if parsed.scheme not in {"http", "https"} or not host or parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("Use an HTTP or HTTPS URL without credentials or a fragment")
    if host == "metadata.google.internal" or "%" in host:
        raise ValueError("Metadata and link-local targets are not allowed")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            resolved = await asyncio.get_running_loop().getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
            addresses = [ipaddress.ip_address(item[4][0]) for item in resolved]
        except OSError:
            # An unavailable HTTPS server can be saved and reported by discovery.
            if parsed.scheme == "https":
                return url
            raise ValueError("Cannot resolve the HTTP host to private addresses") from None
    addresses = [getattr(ip, "ipv4_mapped", None) or ip for ip in addresses]
    if not addresses or any(any(ip.version == net.version and ip in net for net in _BLOCKED_NETWORKS) for ip in addresses):
        raise ValueError("Metadata and link-local targets are not allowed")
    if parsed.scheme == "http":
        enabled = os.getenv("ILLO_EXTERNAL_MCP_ALLOW_PRIVATE_HTTP", "").lower() in {"1", "true", "yes", "on"}
        private = all(any(ip.version == net.version and ip in net for net in _PRIVATE_NETWORKS) for ip in addresses)
        if not enabled or not private:
            raise ValueError("HTTP requires ILLO_EXTERNAL_MCP_ALLOW_PRIVATE_HTTP and a private or loopback host")
    return url


async def validate_config(values: dict) -> dict:
    data = {key: values.get(key) for key in CONFIG_FIELDS}
    if not isinstance(data["name"], str) or not re.fullmatch(r"[a-z0-9_]{1,32}", data["name"]) or "__" in data["name"]:
        raise ValueError("Name must use 1–32 lowercase letters, numbers, or _, without __")
    if not isinstance(data["url"], str):
        raise ValueError("URL is required")
    data["url"] = await validate_url(data["url"])
    data["transport"] = data["transport"] or "streamable_http"
    if data["transport"] != "streamable_http":
        raise ValueError("Only streamable_http is supported")
    data["timeout_seconds"] = 30 if data["timeout_seconds"] is None else data["timeout_seconds"]
    if type(data["timeout_seconds"]) is not int or not 1 <= data["timeout_seconds"] <= 120:
        raise ValueError("Timeout must be an integer from 1 to 120 seconds")
    data["enabled"] = True if data["enabled"] is None else data["enabled"]
    if type(data["enabled"]) is not bool:
        raise ValueError("Enabled must be a boolean")
    tools = data["allowed_tools"] if data["allowed_tools"] is not None else []
    if not isinstance(tools, list) or any(not isinstance(item, str) or not item.strip() for item in tools):
        raise ValueError("Allowed tools must be a list of non-empty tool names")
    data["allowed_tools"] = list(dict.fromkeys(item.strip() for item in tools))
    mode = data["auth_mode"] or "none"
    data["auth_mode"] = mode
    required = {"none": (), "bearer": ("bearer_token_key",), "client_credentials": ("token_url", "client_id_key", "client_secret_key")}
    if mode not in required:
        raise ValueError("Unknown authentication mode")
    for field in ("bearer_token_key", "token_url", "client_id_key", "client_secret_key", "scope", "resource"):
        value = data[field]
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{field} must be text")
        data[field] = (value or "").strip() or None
        if field in required[mode] and not data[field]:
            raise ValueError(f"{field} is required")
        if field not in required[mode] and not (mode == "client_credentials" and field in {"scope", "resource"}):
            data[field] = None
        if field.endswith("_key") and data[field] and len(data[field]) > 128:
            raise ValueError(f"{field} must be at most 128 characters")
    if mode == "client_credentials":
        await validate_url(data["token_url"])
    return data


async def list_servers(session: AsyncSession, org_id: str, *, enabled_only: bool = False) -> list[ExternalMcpServer]:
    query = select(ExternalMcpServer).where(ExternalMcpServer.org_id == org_id).order_by(ExternalMcpServer.id)
    if enabled_only:
        query = query.where(ExternalMcpServer.enabled.is_(True))
    return list((await session.scalars(query)).all())


async def get_server(session: AsyncSession, org_id: str, server_id: int, *, lock: bool = False) -> ExternalMcpServer | None:
    query = select(ExternalMcpServer).where(ExternalMcpServer.org_id == org_id, ExternalMcpServer.id == server_id)
    if lock:
        query = query.with_for_update(read=True, key_share=True)
    return await session.scalar(query)


async def audit_event(session: AsyncSession, server: ExternalMcpServer, event_type: str, *, actor_user_id: str | None = None, **metadata) -> ExternalMcpAuditEvent:
    event = ExternalMcpAuditEvent(org_id=server.org_id, server_id=server.id, server_name=server.name,
                                      event_type=event_type, actor_user_id=actor_user_id, **metadata)
    session.add(event)
    return event


async def save_server(session: AsyncSession, *, org_id: str, actor_user_id: str, values: dict, server: ExternalMcpServer | None = None) -> ExternalMcpServer:
    previous = {key: getattr(server, key) for key in CONFIG_FIELDS} if server else {}
    data = await validate_config({**previous, **values})
    duplicate = await session.scalar(select(ExternalMcpServer.id).where(ExternalMcpServer.org_id == org_id, ExternalMcpServer.name == data["name"]))
    if duplicate is not None and (server is None or duplicate != server.id):
        raise ValueError("A server with this name already exists")
    created = server is None
    if created:
        server = ExternalMcpServer(org_id=org_id, created_by=actor_user_id)
        session.add(server)
    for key, value in data.items():
        setattr(server, key, value)
    server.updated_by = actor_user_id
    server.updated_at = datetime.now(timezone.utc)
    await session.flush()
    await audit_event(session, server, "server_created" if created else "server_updated", actor_user_id=actor_user_id,
                outcome="ok", changed_fields=[key for key in CONFIG_FIELDS if previous.get(key) != data[key]])
    return server


async def delete_server(session: AsyncSession, server: ExternalMcpServer, *, actor_user_id: str) -> None:
    await audit_event(session, server, "server_deleted", actor_user_id=actor_user_id, outcome="ok")
    await session.flush()
    await session.delete(server)
    await session.flush()


def secret_keys(server: ExternalMcpServer) -> list[str]:
    return [getattr(server, key) for key in ("bearer_token_key", "client_id_key", "client_secret_key") if getattr(server, key)]


async def missing_secret_keys(session: AsyncSession, server: ExternalMcpServer) -> list[str]:
    keys = secret_keys(server)
    if not keys:
        return []
    # The runtime resolver also accepts legacy uppercase aliases.
    candidates = set(keys) | {key.upper() for key in keys}
    present = set((await session.scalars(select(Secret.key_name).where(Secret.org_id == server.org_id, Secret.key_name.in_(candidates)))).all())
    return [key for key in keys if key not in present and key.upper() not in present]

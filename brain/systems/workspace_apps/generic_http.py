"""Generic HTTP connector executor for generated workspace app actions.

This is an intentionally narrow interpreter, not arbitrary generated code. Apps
declare a connector spec in the manifest; the server executes bounded HTTP
requests, resolves approved Vault/project-bound credentials, and maps response
items into Domain records.
"""
from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import httpx

from brain.systems.workspace_apps.actions import (
    WorkspaceAppActionContext,
    WorkspaceAppActionContractError,
    WorkspaceAppActionError,
)
from brain.systems.user_domains.service import AsyncDomainService


GENERIC_HTTP_EXECUTOR_KEY = "generic.http"
_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_TEMPLATE_RE = re.compile(r"\{([a-zA-Z_][\w.-]*)\}")
_MAX_ITEMS = 200


async def async_execute_generic_http_action(
    context: WorkspaceAppActionContext,
    payload: dict[str, Any],
) -> Mapping[str, Any]:
    spec = _connector_spec(context.declaration)
    request_spec = _mapping(spec.get("request"), "connector_spec.request")
    kind = str(spec.get("kind") or spec.get("type") or "http_sync").strip()
    _validate_request_effects(context, request_spec)

    response = await _request_json(request_spec, spec=spec, context=context, payload=payload)
    raw_sync_spec = spec.get("sync") or spec.get("domain_sync")
    if raw_sync_spec is not None:
        sync_spec = _mapping(raw_sync_spec, "connector_spec.sync")
        items = _items_from_response(response, spec.get("response"))
        return await _sync_items_to_domain(context, sync_spec, items)
    if kind == "http_sync":
        raise WorkspaceAppActionContractError("connector_spec.sync is required when kind is 'http_sync'")
    return {"response": _compact_response(response)}


def _connector_spec(declaration: Mapping[str, Any]) -> dict[str, Any]:
    raw = declaration.get("connector_spec") or declaration.get("connector") or declaration.get("http")
    spec = _mapping(raw, "connector_spec")
    kind = str(spec.get("kind") or spec.get("type") or "http_sync").strip()
    if kind not in {"http_request", "http_sync"}:
        raise WorkspaceAppActionContractError("connector_spec.kind must be 'http_request' or 'http_sync'")
    return spec


def _validate_request_effects(context: WorkspaceAppActionContext, request_spec: Mapping[str, Any]) -> None:
    effects = {str(effect).strip() for effect in context.declaration.get("effects", [])}
    method = str(request_spec.get("method") or "GET").strip().upper()
    needed = "external.read" if method == "GET" else "external.write"
    if needed not in effects:
        raise WorkspaceAppActionContractError(f"connector_spec.request.method {method} requires effect '{needed}'")


async def _request_json(
    request_spec: Mapping[str, Any],
    *,
    spec: Mapping[str, Any],
    context: WorkspaceAppActionContext,
    payload: Mapping[str, Any],
) -> Any:
    method = str(request_spec.get("method") or "GET").strip().upper()
    if method not in _ALLOWED_METHODS:
        raise WorkspaceAppActionContractError(
            "connector_spec.request.method must be one of: " + ", ".join(sorted(_ALLOWED_METHODS))
        )
    url = _render_template(str(request_spec.get("url") or ""), payload)
    _validate_url(url)

    headers = _render_string_mapping(request_spec.get("headers"), payload)
    params = _render_string_mapping(request_spec.get("params"), payload)
    body = _render_json_value(request_spec.get("json"), payload)
    await _apply_auth(headers, spec.get("auth"), context=context, payload=payload)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0), follow_redirects=False) as client:
            response = await client.request(
                method,
                url,
                headers=headers or None,
                params=params or None,
                json=body if body is not None and method != "GET" else None,
            )
    except httpx.HTTPError as exc:
        raise WorkspaceAppActionError("External connector request failed.") from exc

    if not response.is_success:
        raise WorkspaceAppActionError(f"External connector returned HTTP {response.status_code}.")
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise WorkspaceAppActionError("External connector response was not valid JSON.") from exc


async def _apply_auth(
    headers: dict[str, str],
    auth_spec: Any,
    *,
    context: WorkspaceAppActionContext,
    payload: Mapping[str, Any],
) -> None:
    if auth_spec in (None, {}, "none"):
        return
    auth = _mapping(auth_spec, "connector_spec.auth")
    auth_type = str(auth.get("type") or "bearer").strip()
    if auth_type == "none":
        return

    token = await _resolve_secret(auth, context=context, payload=payload)
    if not token:
        raise WorkspaceAppActionContractError("Connector auth could not resolve an approved Vault credential.")

    if auth_type == "bearer":
        headers["Authorization"] = f"Bearer {token}"
        return
    if auth_type == "header":
        header_name = str(auth.get("header") or auth.get("name") or "").strip()
        if not header_name or header_name.lower() == "authorization":
            raise WorkspaceAppActionContractError("header auth requires a non-Authorization header name")
        headers[header_name] = token
        return
    raise WorkspaceAppActionContractError("connector_spec.auth.type must be 'none', 'bearer', or 'header'")


async def _resolve_secret(
    auth: Mapping[str, Any],
    *,
    context: WorkspaceAppActionContext,
    payload: Mapping[str, Any],
) -> str | None:
    source = str(auth.get("source") or "vault_key").strip()
    if source == "vault_key":
        key = _render_template(str(auth.get("vault_key") or auth.get("key") or ""), payload)
        if not key:
            raise WorkspaceAppActionContractError("vault_key auth requires connector_spec.auth.vault_key")
        if not context.user_id:
            raise WorkspaceAppActionContractError("Vault-backed connector auth requires a human user")
        from brain.systems.vault import get_secret

        return await get_secret(
            key,
            context.user_id,
            org_id=context.org_id,
            allow_shared=True,
            accessed_by="workspace_app_connector",
        )
    if source in {"project_env", "project_vault_binding"}:
        env_name = str(auth.get("env") or auth.get("env_name") or "GITHUB_TOKEN").strip()
        project_slug = _render_template(str(auth.get("project_slug") or payload.get("project_slug") or ""), payload)
        project_slugs = [project_slug] if project_slug else []
        for candidate in auth.get("project_slugs") or payload.get("project_slugs") or []:
            rendered = _render_template(str(candidate), payload)
            if rendered:
                project_slugs.append(rendered)
        if not env_name or not project_slugs:
            raise WorkspaceAppActionContractError("project_env auth requires env and project_slug")
        if not context.user_id:
            raise WorkspaceAppActionContractError("Project-bound connector auth requires a human user")
        from brain.systems.vault import resolve_project_bound_env_tokens

        env = await resolve_project_bound_env_tokens(
            user_id=context.user_id,
            org_id=context.org_id,
            project_slug=project_slugs[0],
            project_slugs=project_slugs,
        )
        return env.get(env_name)
    raise WorkspaceAppActionContractError("connector_spec.auth.source must be 'vault_key' or 'project_env'")


async def _sync_items_to_domain(
    context: WorkspaceAppActionContext,
    sync_spec: Mapping[str, Any],
    items: list[Any],
) -> dict[str, Any]:
    effects = {str(effect).strip() for effect in context.declaration.get("effects", [])}
    if "domain.write" not in effects:
        raise WorkspaceAppActionContractError("connector_spec.sync requires effect 'domain.write'")

    binding_alias = str(sync_spec.get("binding") or sync_spec.get("domain_binding") or "").strip()
    if not binding_alias:
        raise WorkspaceAppActionContractError("connector_spec.sync.binding is required")
    binding = _domain_binding(context.version.manifest or {}, binding_alias)
    if not binding:
        raise WorkspaceAppActionContractError(f"Domain binding '{binding_alias}' is not declared")

    domain_id = _positive_int(binding.get("domain_id"), f"binding '{binding_alias}'.domain_id")
    object_key = str(binding.get("object_key") or "").strip()
    if not object_key:
        raise WorkspaceAppActionContractError(f"binding '{binding_alias}'.object_key is required")

    service = AsyncDomainService(context.session)
    remote_id_expr = sync_spec.get("remote_id") or sync_spec.get("external_id") or "id"
    remote_id_field = str(sync_spec.get("remote_id_field") or sync_spec.get("external_id_field") or "").strip()
    fields_map = _mapping(sync_spec.get("fields"), "connector_spec.sync.fields")
    title_expr = sync_spec.get("title") or sync_spec.get("title_path")
    limit = min(_positive_int(sync_spec.get("limit") or _MAX_ITEMS, "connector_spec.sync.limit"), _MAX_ITEMS)

    existing_by_remote: dict[str, Any] = {}
    if remote_id_field:
        for record in await service.list_records(context.org_id, domain_id, object_key=object_key, limit=500):
            value = (record.data or {}).get(remote_id_field)
            if value is not None:
                existing_by_remote[str(value)] = record

    created = 0
    updated = 0
    skipped = 0
    synced_records: list[dict[str, Any]] = []

    for item in items[:limit]:
        if not isinstance(item, Mapping):
            skipped += 1
            continue
        remote_id = _mapped_value(remote_id_expr, item)
        data = {
            str(field_key): _mapped_value(field_expr, item)
            for field_key, field_expr in fields_map.items()
            if str(field_key).strip()
        }
        data = {key: value for key, value in data.items() if value is not None}
        if remote_id_field and remote_id is not None:
            data.setdefault(remote_id_field, str(remote_id))
        title = _mapped_value(title_expr, item) if title_expr is not None else None
        title_text = str(title).strip() if title is not None else None

        existing = existing_by_remote.get(str(remote_id)) if remote_id is not None else None
        if existing is not None:
            record = await service.update_record(
                context.org_id,
                domain_id,
                existing.id,
                data_patch=data,
                title=title_text,
                actor_id=context.user_id,
                actor_kind="workspace_app_action",
            )
            updated += 1
        else:
            record = await service.create_record(
                context.org_id,
                domain_id,
                object_key,
                data=data,
                title=title_text,
                actor_id=context.user_id,
                actor_kind="workspace_app_action",
            )
            created += 1
            if remote_id_field and remote_id is not None:
                existing_by_remote[str(remote_id)] = record
        synced_records.append(await service.serialize_record(record))

    return {
        "synced": created + updated,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "records": synced_records,
    }


def _items_from_response(response: Any, response_spec: Any) -> list[Any]:
    if isinstance(response_spec, Mapping):
        path = response_spec.get("items_path") or response_spec.get("path") or ""
        value = _extract_path(response, str(path)) if path else response
    else:
        value = response
    if isinstance(value, list):
        return value
    if isinstance(value, Mapping):
        for key in ("items", "data", "results", "records"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                return candidate
    raise WorkspaceAppActionContractError("Connector response did not contain a list of items")


def _compact_response(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return None
    if isinstance(value, Mapping):
        compact: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 50:
                compact["__truncated__"] = True
                break
            compact[str(key)] = _compact_response(item, depth=depth + 1)
        return compact
    if isinstance(value, list):
        compact_items = [_compact_response(item, depth=depth + 1) for item in value[:50]]
        if len(value) > 50:
            compact_items.append({"__truncated__": True})
        return compact_items
    return value


def _mapped_value(expr: Any, item: Mapping[str, Any]) -> Any:
    if isinstance(expr, Mapping):
        if "const" in expr:
            return expr.get("const")
        if "path" in expr:
            return _extract_path(item, str(expr.get("path") or ""))
        if "template" in expr:
            return _render_template(str(expr.get("template") or ""), item)
        if "if" in expr:
            condition = _mapping(expr.get("if"), "mapping expression.if")
            branch = expr.get("then") if _condition_matches(condition, item) else expr.get("else")
            return _literal_or_mapped_value(branch, item)
        raise WorkspaceAppActionContractError("mapping expressions must use const, path, template, or if/then/else")
    if expr is None:
        return None
    return _extract_path(item, str(expr))


def _literal_or_mapped_value(expr: Any, item: Mapping[str, Any]) -> Any:
    if isinstance(expr, Mapping):
        return _mapped_value(expr, item)
    return expr


def _condition_matches(condition: Mapping[str, Any], item: Mapping[str, Any]) -> bool:
    path = condition.get("path") if "path" in condition else condition.get("field")
    if path is None:
        raise WorkspaceAppActionContractError("mapping condition requires field or path")
    value = _extract_path(item, str(path))
    if "exists" in condition:
        return (value is not None) is bool(condition.get("exists"))
    if "equals" in condition:
        return value == condition.get("equals")
    if "not_equals" in condition:
        return value != condition.get("not_equals")
    if "in" in condition:
        options = condition.get("in")
        if not isinstance(options, list):
            raise WorkspaceAppActionContractError("mapping condition.in must be a list")
        return value in options
    return bool(value)


def _extract_path(source: Any, path: str) -> Any:
    clean = path.strip()
    if clean in {"", "$", "."}:
        return source
    clean = clean.removeprefix("$.").removeprefix(".")
    current = source
    for part in clean.split("."):
        if part.endswith("[]"):
            key = part[:-2]
            value = _extract_path(current, key) if key else current
            return value if isinstance(value, list) else []
        if isinstance(current, Mapping):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def _render_string_mapping(value: Any, payload: Mapping[str, Any]) -> dict[str, str]:
    if value is None:
        return {}
    raw = _mapping(value, "request mapping")
    return {str(key): _render_template(str(item), payload) for key, item in raw.items()}


def _render_json_value(value: Any, payload: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        return _render_template(value, payload)
    if isinstance(value, Mapping):
        return {key: _render_json_value(item, payload) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_json_value(item, payload) for item in value]
    return value


def _render_template(template: str, payload: Mapping[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        value = _extract_path(payload, match.group(1))
        return "" if value is None else str(value)

    return _TEMPLATE_RE.sub(replace, template)


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise WorkspaceAppActionContractError("connector_spec.request.url must be an https URL")
    host = parsed.hostname.lower()
    if host in {"localhost", "metadata.google.internal"} or host.endswith(".local"):
        raise WorkspaceAppActionContractError("connector_spec.request.url cannot target local/internal hosts")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        raise WorkspaceAppActionContractError("connector_spec.request.url cannot target private/internal addresses")


def _domain_binding(manifest: Mapping[str, Any], alias: str) -> Mapping[str, Any] | None:
    data_plan = manifest.get("data_plan")
    bindings = data_plan.get("bindings") if isinstance(data_plan, Mapping) else None
    if not isinstance(bindings, Mapping):
        return None
    binding = bindings.get(alias)
    return binding if isinstance(binding, Mapping) else None


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    raise WorkspaceAppActionContractError(f"{field} must be an object")


def _positive_int(value: Any, field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise WorkspaceAppActionContractError(f"{field} must be a positive integer") from exc
    if number <= 0:
        raise WorkspaceAppActionContractError(f"{field} must be a positive integer")
    return number


__all__ = ["GENERIC_HTTP_EXECUTOR_KEY", "async_execute_generic_http_action"]

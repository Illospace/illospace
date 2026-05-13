"""Workspace app persistence and serialization helpers."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from brain.platform.db.models.workspace_app import WorkspaceApp, WorkspaceAppState, WorkspaceAppVersion
from brain.systems.workspace_apps.contracts import (
    CONTRACT_VERSION,
    build_contract_validation_report,
    record_like_state_keys,
)

DEFAULT_RENDERER_KEY = "generated-ui-app"
DEFAULT_SOURCE_KIND = "json"
DEFAULT_STATE_KEY = "default"
_VERSION_MISSING = object()
ALLOWED_DOMAIN_BINDING_OPERATIONS = frozenset(
    {
        "schema",
        "list",
        "query",
        "get",
        "create",
        "update",
        "archive",
        "aggregate",
        "bulkUpdate",
        "history",
        "listRelations",
        "createRelation",
        "archiveRelation",
    }
)


class WorkspaceAppError(ValueError):
    """Base error for workspace app operations."""


class WorkspaceAppNotFound(WorkspaceAppError):
    """Raised when a workspace app cannot be found."""


class WorkspaceAppConflict(WorkspaceAppError):
    """Raised when a workspace app key is already in use."""


class WorkspaceAppContractError(WorkspaceAppError):
    """Raised when a generated app fails the enforced app contract."""

    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        errors = [str(error) for error in report.get("errors", [])]
        super().__init__("Workspace app contract validation failed: " + "; ".join(errors))


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:96] or "workspace-app"


def validate_nonempty_trimmed(value: str | None, field_name: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise WorkspaceAppError(f"{field_name} is required")
    return normalized


def normalize_key(value: str | None, *, fallback_name: str | None = None) -> str:
    raw = (value or "").strip()
    key = slugify(raw or fallback_name or "workspace-app")
    if len(key) > 100:
        raise WorkspaceAppError("key must be at most 100 characters")
    return key


def normalize_renderer_key(value: str | None) -> str:
    renderer_key = (value or DEFAULT_RENDERER_KEY).strip()
    if not renderer_key:
        return DEFAULT_RENDERER_KEY
    if len(renderer_key) > 120:
        raise WorkspaceAppError("renderer_key must be at most 120 characters")
    return renderer_key


def normalize_source_kind(value: str | None) -> str:
    source_kind = (value or DEFAULT_SOURCE_KIND).strip().lower()
    if not source_kind:
        return DEFAULT_SOURCE_KIND
    if len(source_kind) > 40:
        raise WorkspaceAppError("source_kind must be at most 40 characters")
    return source_kind


def _is_prototype_app(app: WorkspaceApp) -> bool:
    metadata = app.app_metadata or {}
    return bool(metadata.get("prototype"))


def _domain_binding_payload(manifest: dict[str, Any] | None) -> tuple[bool, dict[str, Any]]:
    if not isinstance(manifest, dict):
        return False, {}
    data_plan = manifest.get("data_plan")
    if not isinstance(data_plan, dict):
        return False, {}
    bindings = data_plan.get("bindings")
    requires_domain = data_plan.get("mode") == "domain" or bindings is not None
    if not requires_domain:
        return False, {}
    if not isinstance(bindings, dict) or not bindings:
        raise WorkspaceAppError(
            "Workspace app Domain binding validation failed: data_plan.bindings must be a non-empty object"
        )
    return True, bindings


def _binding_error(alias: str, message: str) -> WorkspaceAppError:
    return WorkspaceAppError(
        f"Workspace app Domain binding validation failed: binding '{alias}' {message}"
    )


def _coerce_domain_id(alias: str, value: Any) -> int:
    if value is None or value == "":
        raise _binding_error(alias, "requires domain_id")
    try:
        domain_id = int(value)
    except (TypeError, ValueError) as exc:
        raise _binding_error(alias, "domain_id must be an integer") from exc
    if domain_id <= 0:
        raise _binding_error(alias, "domain_id must be a positive integer")
    return domain_id


def _string_list(alias: str, value: Any, field_name: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise _binding_error(alias, f"{field_name} must be a list")
    items: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            items.append(text)
    return items


def validate_domain_bindings(
    session: Session,
    org_id: str,
    manifest: dict[str, Any] | None,
) -> None:
    """Validate generated app Domain bindings against the real org schema."""

    requires_domain, bindings = _domain_binding_payload(manifest)
    if not requires_domain:
        return

    from brain.systems.user_domains.service import DomainNotFound, DomainService

    service = DomainService(session)
    for alias, binding in bindings.items():
        alias_text = str(alias).strip() or "<empty>"
        if not isinstance(binding, dict):
            raise _binding_error(alias_text, "must be an object")

        domain_id = _coerce_domain_id(alias_text, binding.get("domain_id"))
        object_key = str(binding.get("object_key") or "").strip()
        if not object_key:
            raise _binding_error(alias_text, "requires object_key")

        try:
            domain = service.get_domain(org_id, domain_id, include_archived=True)
        except DomainNotFound as exc:
            raise _binding_error(alias_text, f"references missing Domain {domain_id}") from exc
        if domain.archived_at is not None:
            raise _binding_error(alias_text, f"references archived Domain {domain_id}")

        domain_slug = str(binding.get("domain_slug") or "").strip()
        if domain_slug and domain.slug != domain_slug:
            raise _binding_error(
                alias_text,
                f"domain_slug '{domain_slug}' does not match Domain {domain.id} slug '{domain.slug}'",
            )

        try:
            obj = service.get_object_type(domain.id, object_key)
        except DomainNotFound as exc:
            raise _binding_error(
                alias_text,
                f"references missing object_key '{object_key}' in Domain {domain.id}",
            ) from exc

        fields = service.list_fields(obj.id)
        field_keys = {field.key for field in fields}
        bindable_field_keys = set(field_keys)
        bindable_field_keys.add("title")
        declared_fields = _string_list(alias_text, binding.get("fields"), "fields")
        if declared_fields is not None:
            unknown = sorted(set(declared_fields) - bindable_field_keys)
            if unknown:
                raise _binding_error(alias_text, f"declares missing field(s): {', '.join(unknown)}")
            required = sorted(
                field.key
                for field in fields
                if field.required and field.default_value is None
            )
            omitted = [field for field in required if field not in declared_fields]
            if omitted:
                raise _binding_error(
                    alias_text,
                    f"omits required field(s): {', '.join(omitted)}",
                )

        operations = _string_list(alias_text, binding.get("operations"), "operations")
        if operations is not None:
            unknown_ops = sorted(set(operations) - ALLOWED_DOMAIN_BINDING_OPERATIONS)
            if unknown_ops:
                allowed = ", ".join(sorted(ALLOWED_DOMAIN_BINDING_OPERATIONS))
                raise _binding_error(
                    alias_text,
                    f"declares unsupported operation(s): {', '.join(unknown_ops)}; allowed: {allowed}",
                )


def active_version(session: Session, app_id: str) -> WorkspaceAppVersion | None:
    stmt = (
        select(WorkspaceAppVersion)
        .where(WorkspaceAppVersion.app_id == app_id)
        .order_by(WorkspaceAppVersion.version.desc(), WorkspaceAppVersion.id.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def active_versions_for_apps(
    session: Session,
    app_ids: list[str],
) -> dict[str, WorkspaceAppVersion]:
    normalized_ids = [str(app_id) for app_id in app_ids if app_id]
    if not normalized_ids:
        return {}

    ranked = (
        select(
            WorkspaceAppVersion.id.label("id"),
            func.row_number()
            .over(
                partition_by=WorkspaceAppVersion.app_id,
                order_by=(WorkspaceAppVersion.version.desc(), WorkspaceAppVersion.id.desc()),
            )
            .label("rank"),
        )
        .where(WorkspaceAppVersion.app_id.in_(normalized_ids))
        .subquery()
    )
    stmt = (
        select(WorkspaceAppVersion)
        .join(ranked, WorkspaceAppVersion.id == ranked.c.id)
        .where(ranked.c.rank == 1)
    )
    return {str(version.app_id): version for version in session.scalars(stmt).all()}


def serialize_version(version: WorkspaceAppVersion | None) -> dict[str, Any] | None:
    if version is None:
        return None
    return {
        "id": version.id,
        "app_id": version.app_id,
        "version": version.version,
        "renderer_key": version.renderer_key,
        "source_kind": version.source_kind,
        "source_code": version.source_code,
        "manifest": version.manifest or {},
        "created_by_user_id": version.created_by_user_id,
        "created_at": version.created_at,
    }


def serialize_app(
    session: Session,
    app: WorkspaceApp,
    *,
    version: WorkspaceAppVersion | None | object = _VERSION_MISSING,
) -> dict[str, Any]:
    resolved_version = active_version(session, app.id) if version is _VERSION_MISSING else version
    return {
        "id": app.id,
        "org_id": app.org_id,
        "key": app.key,
        "name": app.name,
        "description": app.description,
        "renderer_key": app.renderer_key,
        "visual_spec": app.visual_spec or {},
        "metadata": app.app_metadata or {},
        "created_by_user_id": app.created_by_user_id,
        "anchor_user_id": app.anchor_user_id,
        "archived_at": app.archived_at,
        "created_at": app.created_at,
        "updated_at": app.updated_at,
        "active_version": serialize_version(resolved_version),
        "contract_validation": contract_validation_report_for_app(app, resolved_version),
    }


def serialize_apps(session: Session, apps: list[WorkspaceApp]) -> list[dict[str, Any]]:
    versions_by_app_id = active_versions_for_apps(session, [str(app.id) for app in apps])
    return [
        serialize_app(session, app, version=versions_by_app_id.get(str(app.id)))
        for app in apps
    ]


def serialize_state(state: WorkspaceAppState) -> dict[str, Any]:
    return {
        "id": state.id,
        "org_id": state.org_id,
        "app_id": state.app_id,
        "scope": state.scope,
        "key": state.key,
        "data": state.data or {},
        "updated_by_user_id": state.updated_by_user_id,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
    }


def list_apps(
    session: Session,
    org_id: str,
    *,
    include_archived: bool = False,
    include_prototypes: bool = False,
) -> list[WorkspaceApp]:
    stmt = select(WorkspaceApp).where(WorkspaceApp.org_id == org_id)
    if not include_archived:
        stmt = stmt.where(WorkspaceApp.archived_at.is_(None))
    stmt = stmt.order_by(WorkspaceApp.updated_at.desc(), WorkspaceApp.created_at.desc())
    apps = list(session.scalars(stmt).all())
    if include_prototypes:
        return apps
    return [app for app in apps if not _is_prototype_app(app)]


def list_archived_apps(
    session: Session,
    org_id: str,
    *,
    limit: int = 12,
    include_prototypes: bool = False,
) -> list[WorkspaceApp]:
    capped_limit = max(1, min(int(limit or 12), 50))
    stmt = (
        select(WorkspaceApp)
        .where(WorkspaceApp.org_id == org_id, WorkspaceApp.archived_at.is_not(None))
        .order_by(WorkspaceApp.archived_at.desc(), WorkspaceApp.updated_at.desc())
        .limit(capped_limit)
    )
    apps = list(session.scalars(stmt).all())
    if include_prototypes:
        return apps
    return [app for app in apps if not _is_prototype_app(app)]


def get_app(
    session: Session,
    org_id: str,
    app_id: str | None = None,
    *,
    key: str | None = None,
    include_archived: bool = False,
) -> WorkspaceApp:
    if not app_id and not key:
        raise WorkspaceAppError("app_id or key is required")
    stmt = select(WorkspaceApp).where(WorkspaceApp.org_id == org_id)
    if app_id:
        stmt = stmt.where(WorkspaceApp.id == app_id)
    else:
        stmt = stmt.where(WorkspaceApp.key == key)
    if not include_archived:
        stmt = stmt.where(WorkspaceApp.archived_at.is_(None))
    app = session.scalars(stmt).first()
    if app is None or _is_prototype_app(app):
        raise WorkspaceAppNotFound("Workspace app not found")
    return app


def _app_for_key(session: Session, org_id: str, key: str) -> WorkspaceApp | None:
    return session.scalars(
        select(WorkspaceApp).where(
            WorkspaceApp.org_id == org_id,
            WorkspaceApp.key == key,
        )
    ).first()


def contract_validation_report_for_app(
    app: WorkspaceApp,
    version: WorkspaceAppVersion | None = None,
) -> dict[str, Any]:
    return build_contract_validation_report(
        renderer_key=(version.renderer_key if version else app.renderer_key),
        source_kind=(version.source_kind if version else DEFAULT_SOURCE_KIND),
        source_code=(version.source_code if version else ""),
        manifest=(version.manifest if version else {}),
        visual_spec=app.visual_spec or {},
        metadata=app.app_metadata or {},
        initial_state=None,
        require_contract=False,
    )


def _validate_app_contract_or_raise(
    *,
    renderer_key: str,
    source_kind: str,
    source_code: str,
    manifest: dict[str, Any] | None,
    visual_spec: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
    initial_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report = build_contract_validation_report(
        renderer_key=renderer_key,
        source_kind=source_kind,
        source_code=source_code,
        manifest=manifest or {},
        visual_spec=visual_spec or {},
        metadata=metadata or {},
        initial_state=initial_state,
        require_contract=True,
    )
    if report.get("status") == "failed":
        raise WorkspaceAppContractError(report)
    return report


def _validate_contract_state_payload_or_raise(
    app: WorkspaceApp,
    version: WorkspaceAppVersion | None,
    data: dict[str, Any],
) -> None:
    if _is_prototype_app(app):
        return
    manifest = version.manifest if version else {}
    if not isinstance(manifest, dict) or manifest.get("contract_version") != CONTRACT_VERSION:
        return
    disallowed = sorted(record_like_state_keys(data))
    if not disallowed:
        return
    raise WorkspaceAppContractError(
        {
            "status": "failed",
            "contract_version": CONTRACT_VERSION,
            "errors": [
                "workspace app state may only store UI preferences, filters, drafts, and ephemeral state; "
                f"use Domain records for: {', '.join(disallowed)}"
            ],
            "warnings": [],
        }
    )


def create_app(
    session: Session,
    *,
    org_id: str,
    name: str,
    description: str | None = None,
    key: str | None = None,
    renderer_key: str | None = None,
    source_kind: str | None = None,
    source_code: str | None = None,
    manifest: dict[str, Any] | None = None,
    visual_spec: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    created_by_user_id: str | None = None,
    anchor_user_id: str | None = None,
    initial_state: dict[str, Any] | None = None,
    state_key: str = DEFAULT_STATE_KEY,
) -> WorkspaceApp:
    normalized_name = validate_nonempty_trimmed(name, "name")
    normalized_key = normalize_key(key, fallback_name=normalized_name)
    existing = _app_for_key(session, org_id, normalized_key)
    if existing is not None and not _is_prototype_app(existing):
        raise WorkspaceAppConflict(f"Workspace app key '{normalized_key}' already exists")

    normalized_renderer = normalize_renderer_key(renderer_key)
    normalized_source_kind = normalize_source_kind(source_kind)
    normalized_source = source_code if source_code is not None else ""
    normalized_manifest = manifest or {}
    normalized_visual_spec = visual_spec or {}
    normalized_metadata = metadata or {}

    validate_domain_bindings(session, org_id, normalized_manifest)
    _validate_app_contract_or_raise(
        renderer_key=normalized_renderer,
        source_kind=normalized_source_kind,
        source_code=normalized_source,
        manifest=normalized_manifest,
        visual_spec=normalized_visual_spec,
        metadata=normalized_metadata,
        initial_state=initial_state,
    )

    if existing is not None:
        app = existing
        app.archived_at = None
        app.name = normalized_name
        app.description = (description or "").strip() or None
        app.renderer_key = normalized_renderer
        app.visual_spec = normalized_visual_spec
        app.app_metadata = normalized_metadata
        app.created_by_user_id = app.created_by_user_id or created_by_user_id
        app.anchor_user_id = anchor_user_id or created_by_user_id
    else:
        app = WorkspaceApp(
            org_id=org_id,
            key=normalized_key,
            name=normalized_name,
            description=(description or "").strip() or None,
            renderer_key=normalized_renderer,
            visual_spec=normalized_visual_spec,
            app_metadata=normalized_metadata,
            created_by_user_id=created_by_user_id,
            anchor_user_id=anchor_user_id or created_by_user_id,
        )
        session.add(app)
        session.flush()

    current = active_version(session, app.id)
    session.add(
        WorkspaceAppVersion(
            app_id=app.id,
            version=(current.version if current else 0) + 1,
            renderer_key=normalized_renderer,
            source_kind=normalized_source_kind,
            source_code=normalized_source,
            manifest=normalized_manifest,
            created_by_user_id=created_by_user_id,
        )
    )
    if initial_state is not None:
        state = get_or_create_state(
            session,
            org_id=org_id,
            app_id=app.id,
            key=state_key or DEFAULT_STATE_KEY,
            user_id=created_by_user_id,
        )
        state.data = initial_state
        state.updated_by_user_id = created_by_user_id
    session.flush()
    return app


def update_app(
    session: Session,
    *,
    org_id: str,
    app_id: str | None = None,
    key: str | None = None,
    name: str | None = None,
    description: str | None = None,
    renderer_key: str | None = None,
    source_kind: str | None = None,
    source_code: str | None = None,
    manifest: dict[str, Any] | None = None,
    visual_spec: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    anchor_user_id: str | None = None,
    updated_by_user_id: str | None = None,
) -> WorkspaceApp:
    app = get_app(session, org_id, app_id, key=key)
    current = active_version(session, app.id)
    next_renderer = normalize_renderer_key(renderer_key) if renderer_key is not None else app.renderer_key
    next_source_kind = normalize_source_kind(source_kind or (current.source_kind if current else None))
    next_source = source_code if source_code is not None else (current.source_code if current else "")
    next_manifest = manifest if manifest is not None else (current.manifest if current else {})
    next_visual_spec = visual_spec if visual_spec is not None else (app.visual_spec or {})
    next_metadata = metadata if metadata is not None else (app.app_metadata or {})
    contract_inputs_changed = any(
        value is not None
        for value in (renderer_key, source_kind, source_code, manifest, visual_spec, metadata)
    )
    if contract_inputs_changed:
        validate_domain_bindings(session, org_id, next_manifest)
        _validate_app_contract_or_raise(
            renderer_key=next_renderer,
            source_kind=next_source_kind,
            source_code=next_source,
            manifest=next_manifest,
            visual_spec=next_visual_spec,
            metadata=next_metadata,
        )

    if name is not None:
        app.name = validate_nonempty_trimmed(name, "name")
    if description is not None:
        app.description = description.strip() or None
    if renderer_key is not None:
        app.renderer_key = next_renderer
    if visual_spec is not None:
        app.visual_spec = next_visual_spec
    if metadata is not None:
        app.app_metadata = next_metadata
    if anchor_user_id is not None:
        app.anchor_user_id = anchor_user_id

    if source_code is not None or source_kind is not None or manifest is not None or renderer_key is not None:
        next_version = (current.version if current else 0) + 1
        next_manifest = manifest if manifest is not None else (current.manifest if current else {})
        session.add(
            WorkspaceAppVersion(
                app_id=app.id,
                version=next_version,
                renderer_key=app.renderer_key,
                source_kind=next_source_kind,
                source_code=next_source,
                manifest=next_manifest,
                created_by_user_id=updated_by_user_id,
            )
        )
    session.flush()
    return app


def archive_app(
    session: Session,
    *,
    org_id: str,
    app_id: str | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    app = get_app(session, org_id, app_id, key=key)
    app.archived_at = datetime.now(timezone.utc)
    session.flush()
    return {"archived": {"id": app.id, "key": app.key}}


def restore_app(
    session: Session,
    *,
    org_id: str,
    app_id: str | None = None,
    key: str | None = None,
) -> WorkspaceApp:
    app = get_app(session, org_id, app_id, key=key, include_archived=True)
    app.archived_at = None
    session.flush()
    return app


def get_or_create_state(
    session: Session,
    *,
    org_id: str,
    app_id: str,
    key: str,
    user_id: str | None,
) -> WorkspaceAppState:
    state_key = key or DEFAULT_STATE_KEY
    state = session.scalars(
        select(WorkspaceAppState).where(
            WorkspaceAppState.org_id == org_id,
            WorkspaceAppState.app_id == app_id,
            WorkspaceAppState.scope == "org",
            WorkspaceAppState.key == state_key,
        )
    ).first()
    if state is not None:
        return state

    state = WorkspaceAppState(
        org_id=org_id,
        app_id=app_id,
        scope="org",
        key=state_key,
        data={},
        updated_by_user_id=user_id,
    )
    session.add(state)
    session.flush()
    return state


def get_state(
    session: Session,
    *,
    org_id: str,
    app_id: str,
    key: str = DEFAULT_STATE_KEY,
    user_id: str | None = None,
) -> WorkspaceAppState:
    app = get_app(session, org_id, app_id)
    return get_or_create_state(session, org_id=org_id, app_id=app.id, key=key, user_id=user_id)


def update_state(
    session: Session,
    *,
    org_id: str,
    app_id: str,
    key: str = DEFAULT_STATE_KEY,
    data: dict[str, Any] | None = None,
    data_patch: dict[str, Any] | None = None,
    user_id: str | None = None,
) -> WorkspaceAppState:
    app = get_app(session, org_id=org_id, app_id=app_id)
    state = get_or_create_state(session, org_id=org_id, app_id=app.id, key=key, user_id=user_id)
    if data_patch is not None:
        next_data = {**(state.data or {}), **data_patch}
    else:
        next_data = data or {}
    _validate_contract_state_payload_or_raise(app, active_version(session, app.id), next_data)
    state.data = next_data
    state.updated_by_user_id = user_id
    session.flush()
    return state

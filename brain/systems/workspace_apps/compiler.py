"""Workspace-app input compiler for generated app creation.

This layer is intentionally app-specific. It turns high-variance generated app
tool payloads into the stricter persistence contract before service validation.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from brain.systems.workspace_apps.app_capsule_compiler import compile_app_capsule_source
from brain.systems.workspace_apps.capabilities import (
    normalize_domain_operations,
    normalize_system_operations,
)
from brain.systems.workspace_apps.contracts import (
    APP_KIT_NAME,
    APP_CAPSULE_RENDERER_KEY,
    APP_CAPSULE_SOURCE_KIND,
    CONTRACT_VERSION,
    STRUCTURED_UI_RENDERER_KEY,
    STRUCTURED_UI_SOURCE_KIND,
)

DEFAULT_APP_LOCAL_SCOPE = "ui_state"
DEFAULT_THUMBNAIL_STATUS = "Ready"
_ENVELOPE_KEYS = frozenset(
    {
        "manifest",
        "visual_spec",
        "metadata",
        "renderer_key",
        "source_kind",
        "source_code",
        "source",
        "spec",
        "generated_ui_spec",
        "ui_spec",
    }
)
_STRUCTURED_SOURCE_KINDS = frozenset({STRUCTURED_UI_SOURCE_KIND, "generated-ui", "generated_ui"})
_VIEW_TYPE_ALIASES = {
    "metric": "metrics",
    "stat": "metrics",
    "stats": "metrics",
    "grid": "cards",
    "card": "cards",
    "kanban": "board",
    "board-view": "board",
}

@dataclass(frozen=True)
class WorkspaceAppCompileResult:
    renderer_key: str | None
    source_kind: str | None
    source_code: str | None
    manifest: dict[str, Any] | None
    visual_spec: dict[str, Any] | None
    metadata: dict[str, Any] | None
    repairs: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()


def infer_renderer_defaults(
    renderer_key: str | None,
    source_kind: str | None,
    source_code: Any,
) -> tuple[str | None, str | None]:
    if renderer_key == APP_CAPSULE_RENDERER_KEY and not source_kind:
        return renderer_key, APP_CAPSULE_SOURCE_KIND
    if renderer_key == STRUCTURED_UI_RENDERER_KEY and not source_kind:
        return renderer_key, STRUCTURED_UI_SOURCE_KIND
    if renderer_key == "sandboxed-html-app" and not source_kind:
        return renderer_key, "html"
    if not renderer_key and source_kind == APP_CAPSULE_SOURCE_KIND:
        return APP_CAPSULE_RENDERER_KEY, source_kind
    if not renderer_key and not source_kind and not source_code:
        return APP_CAPSULE_RENDERER_KEY, APP_CAPSULE_SOURCE_KIND
    if renderer_key or source_kind or not source_code:
        return renderer_key, source_kind
    stripped = str(source_code).strip()
    if stripped.startswith("{"):
        return STRUCTURED_UI_RENDERER_KEY, STRUCTURED_UI_SOURCE_KIND
    return APP_CAPSULE_RENDERER_KEY, APP_CAPSULE_SOURCE_KIND


def compile_workspace_app_input(
    *,
    action: str,
    name: str | None = None,
    key: str | None = None,
    renderer_key: str | None = None,
    source_kind: str | None = None,
    source_code: Any = None,
    manifest: Mapping[str, Any] | None = None,
    visual_spec: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    initial_state: Mapping[str, Any] | None = None,
) -> WorkspaceAppCompileResult:
    """Compile generated workspace-app tool args into persistence inputs.

    Safe repairs are limited to shape/default issues. Semantic data-model
    problems still flow through contract/service validation.
    """

    del initial_state
    repairs: list[dict[str, Any]] = []
    warnings: list[str] = []
    is_create = action == "create"

    if not is_create and not renderer_key and not source_kind and source_code is None:
        next_renderer, next_source_kind = None, None
    else:
        next_renderer, next_source_kind = infer_renderer_defaults(renderer_key, source_kind, source_code)
    source_text = _source_text(source_code)
    parsed_source = _json_object(source_text)

    embedded_manifest = parsed_source.get("manifest") if parsed_source else None
    embedded_visual_spec = parsed_source.get("visual_spec") if parsed_source else None
    embedded_metadata = parsed_source.get("metadata") if parsed_source else None
    embedded_renderer = parsed_source.get("renderer_key") if parsed_source else None
    embedded_source_kind = parsed_source.get("source_kind") if parsed_source else None

    if not next_renderer and isinstance(embedded_renderer, str):
        next_renderer = embedded_renderer
        _record(repairs, "renderer_key", "lifted renderer_key from source envelope")
    if not next_source_kind and isinstance(embedded_source_kind, str):
        next_source_kind = embedded_source_kind
        _record(repairs, "source_kind", "lifted source_kind from source envelope")

    if is_create or renderer_key or source_kind or source_code is not None or next_renderer or next_source_kind:
        next_renderer, next_source_kind = infer_renderer_defaults(next_renderer, next_source_kind, source_text)
    generated_ui = _is_generated_ui_source(next_renderer, next_source_kind)
    app_capsule = _is_app_capsule_source(next_renderer, next_source_kind)

    next_manifest = _prefer_mapping(manifest, embedded_manifest)
    next_visual_spec = _prefer_mapping(visual_spec, embedded_visual_spec)
    next_metadata = _prefer_metadata(metadata, embedded_metadata, is_create=is_create)

    if parsed_source:
        spec_payload = _source_spec_payload(parsed_source)
        if spec_payload is not parsed_source:
            _record(repairs, "source_code", "extracted generated UI spec from source envelope")
        elif _ENVELOPE_KEYS.intersection(parsed_source):
            spec_payload = {
                item_key: item_value
                for item_key, item_value in parsed_source.items()
                if item_key not in _ENVELOPE_KEYS
            }
            _record(repairs, "source_code", "removed app envelope fields from generated UI source")
        if generated_ui:
            source_text = _json_dumps(spec_payload)
        else:
            envelope_source = parsed_source.get("source_code", parsed_source.get("source"))
            if isinstance(envelope_source, str):
                source_text = envelope_source
                _record(repairs, "source_code", "extracted source text from app envelope")

    should_compile_contract_fields = generated_ui or app_capsule or (is_create and source_text is not None)
    if should_compile_contract_fields:
        next_manifest = _compile_manifest(
            next_manifest,
            is_create=is_create,
            renderer_key=next_renderer,
            source_kind=next_source_kind,
            repairs=repairs,
        )
        next_visual_spec = _compile_visual_spec(
            next_visual_spec,
            app_name=_display_name(name=name, key=key),
            is_create=is_create,
            repairs=repairs,
        )
    if generated_ui:
        source_text = _compile_generated_ui_source(
            source_text,
            app_name=_display_name(name=name, key=key),
            manifest=next_manifest,
            repairs=repairs,
        )
    elif app_capsule:
        source_text = compile_app_capsule_source(
            source_text,
            app_name=_display_name(name=name, key=key),
            parsed_source=parsed_source,
            repairs=repairs,
        )

    return WorkspaceAppCompileResult(
        renderer_key=next_renderer,
        source_kind=next_source_kind,
        source_code=source_text,
        manifest=next_manifest,
        visual_spec=next_visual_spec,
        metadata=next_metadata,
        repairs=tuple(repairs),
        warnings=tuple(warnings),
    )


def contract_repair_guidance(report: Mapping[str, Any] | None) -> dict[str, Any]:
    errors = [str(error) for error in (report or {}).get("errors", [])]
    text = "\n".join(errors).lower()
    if "record-like collections" in text or "domain binding" in text:
        return {
            "failure_kind": "data_model_requires_domain",
            "retryable": True,
            "suggested_repair": "Create or bind a Domain for durable records; keep app-local state to UI preferences, filters, drafts, or ephemeral state.",
        }
    if (
        "illo app kit" in text
        or "hardcode visual colors" in text
        or "fixed body background" in text
        or "letter spacing" in text
        or "illo-panel" in text
        or "illo-button" in text
        or "illo-select" in text
    ):
        return {
            "failure_kind": "html_app_contract",
            "retryable": True,
            "suggested_repair": "Repair the sandboxed HTML source: use App Kit classes on controls, use host/App Kit CSS variables instead of hardcoded colors, avoid fixed body backgrounds, and keep layout fluid across dock and overlay widths.",
        }
    if "generated ui" in text or "source_code" in text:
        return {
            "failure_kind": "generated_ui_source_shape",
            "retryable": True,
            "suggested_repair": "Submit a generated UI JSON spec with title and at least one view, or include rows/records/metrics/bindings so the compiler can infer a view.",
        }
    if "thumbnail" in text:
        return {
            "failure_kind": "thumbnail_contract",
            "retryable": True,
            "suggested_repair": "Use structured visual_spec.thumbnail metadata with label plus value or status; do not submit thumbnail HTML.",
        }
    if "manifest." in text:
        return {
            "failure_kind": "manifest_contract",
            "retryable": True,
            "suggested_repair": "Provide or let the app compiler default contract_version, data_plan, and design_contract.",
        }
    return {
        "failure_kind": "workspace_app_contract",
        "retryable": True,
        "suggested_repair": "Revise the generated app payload using the contract_validation errors.",
    }


def service_error_guidance(message: str) -> dict[str, Any] | None:
    text = str(message or "")
    lowered = text.lower()
    if "domain binding validation failed" in lowered:
        return {
            "failure_kind": "invalid_domain_binding",
            "retryable": True,
            "suggested_repair": "List or inspect the target Domain, then retry with real domain_id, object_key, fields, and allowed operations.",
        }
    return None


def _is_generated_ui_source(renderer_key: str | None, source_kind: str | None) -> bool:
    normalized_renderer = (renderer_key or "").strip()
    normalized_source_kind = (source_kind or "").strip().lower()
    return normalized_renderer == STRUCTURED_UI_RENDERER_KEY or normalized_source_kind in _STRUCTURED_SOURCE_KINDS


def _is_app_capsule_source(renderer_key: str | None, source_kind: str | None) -> bool:
    normalized_renderer = (renderer_key or "").strip()
    normalized_source_kind = (source_kind or "").strip().lower()
    return normalized_renderer == APP_CAPSULE_RENDERER_KEY and normalized_source_kind == APP_CAPSULE_SOURCE_KIND


def _source_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return _json_dumps(dict(value))
    return str(value)


def _json_object(value: str | None) -> dict[str, Any] | None:
    if not value or not value.strip().startswith("{"):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return dict(parsed) if isinstance(parsed, dict) else None


def _source_spec_payload(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("generated_ui_spec", "ui_spec", "spec", "source", "source_code"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            return dict(value)
        if isinstance(value, str):
            parsed = _json_object(value)
            if parsed is not None:
                return parsed
    return payload


def _prefer_mapping(current: Mapping[str, Any] | None, embedded: object) -> dict[str, Any] | None:
    if isinstance(current, Mapping) and dict(current):
        return dict(current)
    if isinstance(embedded, Mapping):
        return dict(embedded)
    if isinstance(current, Mapping):
        return dict(current)
    return None


def _prefer_metadata(
    current: Mapping[str, Any] | None,
    embedded: object,
    *,
    is_create: bool,
) -> dict[str, Any] | None:
    if isinstance(current, Mapping) and dict(current):
        return dict(current)
    if isinstance(embedded, Mapping):
        return dict(embedded)
    if isinstance(current, Mapping):
        return dict(current)
    return {} if is_create else None


def _compile_manifest(
    manifest: dict[str, Any] | None,
    *,
    is_create: bool,
    renderer_key: str | None,
    source_kind: str | None,
    repairs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if manifest is None and not is_create:
        return None
    next_manifest = dict(manifest or {})
    if next_manifest.get("contract_version") != CONTRACT_VERSION:
        next_manifest["contract_version"] = CONTRACT_VERSION
        _record(repairs, "manifest.contract_version", "defaulted generated app contract_version")

    data_plan = next_manifest.get("data_plan")
    if not isinstance(data_plan, Mapping) or not dict(data_plan):
        if is_create:
            if _is_app_capsule_source(renderer_key, source_kind):
                next_manifest["data_plan"] = {"mode": "capability", "bindings": {}}
                _record(repairs, "manifest.data_plan", "defaulted create to capability data plan")
            else:
                next_manifest["data_plan"] = {"mode": "app_local", "scope": DEFAULT_APP_LOCAL_SCOPE}
                _record(repairs, "manifest.data_plan", "defaulted create to app-local UI state")
    else:
        next_plan = dict(data_plan)
        mode = str(next_plan.get("mode") or "").strip()
        if not mode and isinstance(next_plan.get("bindings"), Mapping):
            if _bindings_look_capability_style(next_plan.get("bindings")):
                next_plan["mode"] = "capability"
                _record(repairs, "manifest.data_plan.mode", "inferred capability mode from bindings")
            else:
                next_plan["mode"] = "domain"
                _record(repairs, "manifest.data_plan.mode", "inferred domain mode from bindings")
        elif not mode and (next_plan.get("scope") or next_plan.get("app_local_scope") or next_plan.get("ui_state_only")):
            next_plan["mode"] = "app_local"
            _record(repairs, "manifest.data_plan.mode", "inferred app_local mode from UI state scope")
        if next_plan.get("mode") == "app_local" and not (
            next_plan.get("scope") or next_plan.get("app_local_scope") or next_plan.get("ui_state_only") is True
        ):
            next_plan["scope"] = DEFAULT_APP_LOCAL_SCOPE
            _record(repairs, "manifest.data_plan.scope", "defaulted app-local scope to UI state")
        if next_plan.get("mode") == "domain":
            _normalize_domain_bindings(next_plan, repairs=repairs)
        if next_plan.get("mode") == "capability":
            _normalize_capability_bindings(next_plan, repairs=repairs)
        next_manifest["data_plan"] = next_plan

    design_contract = next_manifest.get("design_contract")
    if not isinstance(design_contract, Mapping) or not dict(design_contract):
        next_manifest["design_contract"] = _default_design_contract()
        _record(repairs, "manifest.design_contract", "defaulted Constellation App Kit design contract")
    else:
        next_design = dict(design_contract)
        if next_design.get("kit") != APP_KIT_NAME:
            next_design["kit"] = APP_KIT_NAME
            _record(repairs, "manifest.design_contract.kit", "defaulted App Kit name")
        modes = next_design.get("theme_modes")
        normalized_modes = [str(mode) for mode in modes] if isinstance(modes, list) else []
        if not {"dark", "light"} <= set(normalized_modes):
            next_design["theme_modes"] = sorted(set(normalized_modes) | {"dark", "light"})
            _record(repairs, "manifest.design_contract.theme_modes", "ensured dark and light theme modes")
        next_manifest["design_contract"] = next_design
    return next_manifest


def _normalize_domain_bindings(
    data_plan: dict[str, Any],
    *,
    repairs: list[dict[str, Any]],
) -> None:
    bindings = data_plan.get("bindings")
    if not isinstance(bindings, Mapping):
        return

    next_bindings: dict[str, Any] = {}
    changed_bindings = False
    for alias, raw_binding in bindings.items():
        if not isinstance(raw_binding, Mapping):
            next_bindings[alias] = raw_binding
            continue

        binding = dict(raw_binding)
        operations, operations_changed = normalize_domain_operations(binding.get("operations"))
        if operations:
            if operations != binding.get("operations"):
                binding["operations"] = operations
                _record(
                    repairs,
                    f"manifest.data_plan.bindings.{alias}.operations",
                    "normalized Domain binding operations",
                )
                changed_bindings = True
            elif operations_changed:
                changed_bindings = True
        next_bindings[alias] = binding

    if changed_bindings:
        data_plan["bindings"] = next_bindings


def _bindings_look_capability_style(bindings: Any) -> bool:
    if not isinstance(bindings, Mapping):
        return False
    for raw_binding in bindings.values():
        if not isinstance(raw_binding, Mapping):
            continue
        if raw_binding.get("kind") in {"domain", "system"} or raw_binding.get("source") or raw_binding.get("source_key"):
            return True
    return False


def _normalize_capability_bindings(
    data_plan: dict[str, Any],
    *,
    repairs: list[dict[str, Any]],
) -> None:
    bindings = data_plan.get("bindings")
    if not isinstance(bindings, Mapping):
        return

    next_bindings: dict[str, Any] = {}
    changed_bindings = False
    for alias, raw_binding in bindings.items():
        if not isinstance(raw_binding, Mapping):
            next_bindings[alias] = raw_binding
            continue

        binding = dict(raw_binding)
        kind = str(binding.get("kind") or "").strip()
        if not kind:
            if binding.get("source") or binding.get("source_key"):
                kind = "system"
            elif binding.get("domain_id") or binding.get("object_key"):
                kind = "domain"
            if kind:
                binding["kind"] = kind
                _record(repairs, f"manifest.data_plan.bindings.{alias}.kind", f"defaulted capability binding kind to {kind}")
                changed_bindings = True

        if kind == "domain":
            operations, operations_changed = normalize_domain_operations(binding.get("operations"), broker_only=True)
        elif kind == "system":
            operations, operations_changed = normalize_system_operations(binding.get("operations"))
        else:
            operations, operations_changed = [], False

        if operations:
            if operations != binding.get("operations"):
                binding["operations"] = operations
                _record(
                    repairs,
                    f"manifest.data_plan.bindings.{alias}.operations",
                    "normalized capability binding operations",
                )
                changed_bindings = True
            elif operations_changed:
                changed_bindings = True
        next_bindings[alias] = binding

    if changed_bindings:
        data_plan["bindings"] = next_bindings


def _compile_visual_spec(
    visual_spec: dict[str, Any] | None,
    *,
    app_name: str,
    is_create: bool,
    repairs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if visual_spec is None and not is_create:
        return None
    next_visual = dict(visual_spec or {})
    thumbnail = next_visual.get("thumbnail")
    if not isinstance(thumbnail, Mapping) and isinstance(next_visual.get("thumbnail_manifest"), Mapping):
        next_visual["thumbnail"] = dict(next_visual["thumbnail_manifest"])
        thumbnail = next_visual["thumbnail"]
        _record(repairs, "visual_spec.thumbnail", "copied thumbnail_manifest into thumbnail")
    if thumbnail is None:
        next_visual["thumbnail"] = _default_thumbnail(app_name)
        _record(repairs, "visual_spec.thumbnail", "defaulted structured thumbnail metadata")
    elif isinstance(thumbnail, Mapping) and not (thumbnail.get("source_code") or thumbnail.get("html")):
        next_thumbnail = dict(thumbnail)
        if not str(next_thumbnail.get("label") or "").strip():
            next_thumbnail["label"] = app_name
            _record(repairs, "visual_spec.thumbnail.label", "defaulted thumbnail label")
        if next_thumbnail.get("value") is None and next_thumbnail.get("status") is None:
            next_thumbnail["status"] = DEFAULT_THUMBNAIL_STATUS
            _record(repairs, "visual_spec.thumbnail.status", "defaulted thumbnail status")
        next_visual["thumbnail"] = next_thumbnail
    return next_visual


def _compile_generated_ui_source(
    source_code: str | None,
    *,
    app_name: str,
    manifest: Mapping[str, Any] | None,
    repairs: list[dict[str, Any]],
) -> str | None:
    spec = _json_object(source_code)
    if spec is None:
        return source_code
    changed = False
    if spec.get("schema_version", spec.get("version")) in {None, ""}:
        spec["schema_version"] = CONTRACT_VERSION
        changed = True
        _record(repairs, "source_code.schema_version", "defaulted generated UI schema version")
    if not str(spec.get("title") or "").strip():
        spec["title"] = app_name
        changed = True
        _record(repairs, "source_code.title", "defaulted generated UI title from app name")

    raw_views = spec.get("views")
    if isinstance(raw_views, Mapping):
        spec["views"] = [dict(raw_views)]
        raw_views = spec["views"]
        changed = True
        _record(repairs, "source_code.views", "wrapped single view object into a views list")
    if isinstance(raw_views, list) and raw_views:
        normalized_views, views_changed = _normalize_views(raw_views, spec=spec, manifest=manifest, app_name=app_name)
        if views_changed:
            spec["views"] = normalized_views
            changed = True
            _record(repairs, "source_code.views", "normalized generated UI view defaults")
    else:
        inferred = _infer_view(spec=spec, manifest=manifest, app_name=app_name)
        if inferred is not None:
            spec["views"] = [inferred]
            changed = True
            _record(repairs, "source_code.views", "inferred a generated UI view from data or Domain binding")
    return _json_dumps(spec) if changed else source_code


def _normalize_views(
    raw_views: list[Any],
    *,
    spec: Mapping[str, Any],
    manifest: Mapping[str, Any] | None,
    app_name: str,
) -> tuple[list[Any], bool]:
    changed = False
    next_views: list[Any] = []
    for index, raw_view in enumerate(raw_views):
        if not isinstance(raw_view, Mapping):
            next_views.append(raw_view)
            continue
        view = dict(raw_view)
        view_type = _normalize_view_type(view.get("type"))
        if view_type != view.get("type"):
            view["type"] = view_type
            changed = True
        if not view_type:
            view["type"] = _infer_view_type(view)
            changed = True
        if not str(view.get("title") or "").strip():
            view["title"] = app_name if index == 0 else f"View {index + 1}"
            changed = True
        if view.get("type") in {"table", "list", "cards", "detail", "form"}:
            columns_key = "columns" if "columns" in view or "fields" not in view else "fields"
            raw_columns = view.get(columns_key)
            if isinstance(raw_columns, list):
                columns, columns_changed = _normalize_columns(raw_columns)
                if columns_changed:
                    view[columns_key] = columns
                    changed = True
            elif not isinstance(view.get("columns"), list) and not isinstance(view.get("fields"), list):
                columns = _infer_columns(view=view, spec=spec, manifest=manifest)
                if columns:
                    view["columns"] = columns
                    changed = True
        if view.get("type") == "board" and not (view.get("group_by") or view.get("groupBy")):
            group_by = _infer_board_group_by(view=view, spec=spec, manifest=manifest)
            if group_by:
                view["group_by"] = group_by
                changed = True
        if view.get("type") == "board" and not isinstance(view.get("card"), Mapping):
            card = _infer_board_card(view=view, spec=spec, manifest=manifest)
            if card:
                view["card"] = card
                changed = True
        next_views.append(view)
    return next_views, changed


def _normalize_columns(raw_columns: list[Any]) -> tuple[list[Any], bool]:
    changed = False
    columns: list[Any] = []
    for raw_column in raw_columns:
        normalized = _normalize_column(raw_column)
        if normalized != raw_column:
            changed = True
        columns.append(normalized)
    return columns, changed


def _normalize_column(raw_column: Any) -> Any:
    if isinstance(raw_column, str):
        key = raw_column.strip()
        return {"key": key, "label": _label(key)} if key else raw_column
    if not isinstance(raw_column, Mapping):
        return raw_column

    column = dict(raw_column)
    key = str(
        column.get("key")
        or column.get("field")
        or column.get("field_key")
        or column.get("fieldKey")
        or column.get("id")
        or ""
    ).strip()
    if key and not str(column.get("key") or "").strip():
        column["key"] = key
    if key and not str(column.get("label") or "").strip():
        column["label"] = _label(key)
    return column


def _infer_view(
    *,
    spec: Mapping[str, Any],
    manifest: Mapping[str, Any] | None,
    app_name: str,
) -> dict[str, Any] | None:
    binding_alias = _primary_binding_alias(spec, manifest)
    rows = _row_candidates(spec)
    if isinstance(spec.get("metrics"), list):
        return {
            "id": "metrics",
            "type": "metrics",
            "title": app_name,
            "metrics": spec.get("metrics"),
        }
    if rows is not None or binding_alias:
        view: dict[str, Any] = {
            "id": _slug(spec.get("primary_binding") or binding_alias or "records"),
            "type": "table",
            "title": app_name,
        }
        if binding_alias:
            view["binding"] = binding_alias
        columns = _infer_columns(view=view, spec=spec, manifest=manifest)
        if columns:
            view["columns"] = columns
        return view
    return None


def _infer_view_type(view: Mapping[str, Any]) -> str:
    if view.get("group_by") or view.get("groupBy") or view.get("card") or view.get("groups"):
        return "board"
    if isinstance(view.get("metrics"), list):
        return "metrics"
    if view.get("chart_type") or view.get("chart"):
        return "chart"
    if view.get("binding") or view.get("data_binding") or view.get("columns") or view.get("fields"):
        return "table"
    if view.get("rows") or view.get("records"):
        return "table"
    return "list"


def _normalize_view_type(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    return _VIEW_TYPE_ALIASES.get(raw, raw)


def _infer_columns(
    *,
    view: Mapping[str, Any],
    spec: Mapping[str, Any],
    manifest: Mapping[str, Any] | None,
) -> list[dict[str, str]]:
    rows = _row_candidates(view) or _row_candidates(spec)
    if rows:
        keys: list[str] = []
        for row in rows[:5]:
            if not isinstance(row, Mapping):
                continue
            for key in row:
                text = str(key)
                if text.startswith("__") or text in {"id", "version", "object_key"}:
                    continue
                if text not in keys:
                    keys.append(text)
        return [{"key": key, "label": _label(key)} for key in keys[:8]]

    alias = view.get("binding") or view.get("data_binding") or spec.get("primary_binding") or _primary_binding_alias(spec, manifest)
    binding = _domain_binding(manifest, str(alias)) if alias else None
    fields = binding.get("fields") if isinstance(binding, Mapping) else None
    if isinstance(fields, list):
        return [{"key": str(field), "label": _label(str(field))} for field in fields if str(field).strip()]
    return []


def _infer_board_group_by(
    *,
    view: Mapping[str, Any],
    spec: Mapping[str, Any],
    manifest: Mapping[str, Any] | None,
) -> str | None:
    candidates: list[str] = []
    for source in (view, spec):
        for key in ("group_by", "groupBy", "status_field", "statusField"):
            value = str(source.get(key) or "").strip()
            if value:
                candidates.append(value)
    rows = _row_candidates(view) or _row_candidates(spec) or []
    for row in rows[:8]:
        if not isinstance(row, Mapping):
            continue
        for key in ("status", "state", "stage", "phase", "column"):
            if key in row:
                candidates.append(key)
    alias = view.get("binding") or view.get("data_binding") or spec.get("primary_binding") or _primary_binding_alias(spec, manifest)
    binding = _domain_binding(manifest, str(alias)) if alias else None
    fields = binding.get("fields") if isinstance(binding, Mapping) else None
    if isinstance(fields, list):
        normalized = [str(field).strip() for field in fields if str(field).strip()]
        for key in ("status", "state", "stage", "phase", "column"):
            if key in normalized:
                candidates.append(key)
        if normalized:
            candidates.append(normalized[0])
    return candidates[0] if candidates else None


def _infer_board_card(
    *,
    view: Mapping[str, Any],
    spec: Mapping[str, Any],
    manifest: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    fields = _infer_columns(view=view, spec=spec, manifest=manifest)
    keys = [field["key"] for field in fields if field.get("key")]
    if not keys:
        return None
    title = "title" if "title" in keys else keys[0]
    subtitle = next((key for key in ("repo", "repository", "project", "milestone") if key in keys), None)
    badges = [key for key in ("priority", "status", "labels", "label", "milestone") if key in keys and key != title]
    card: dict[str, Any] = {"title": title}
    if subtitle:
        card["subtitle"] = subtitle
    if badges:
        card["badges"] = badges[:3]
    return card


def _primary_binding_alias(spec: Mapping[str, Any], manifest: Mapping[str, Any] | None) -> str | None:
    explicit = str(spec.get("primary_binding") or "").strip()
    if explicit:
        return explicit
    data_plan = (manifest or {}).get("data_plan")
    bindings = data_plan.get("bindings") if isinstance(data_plan, Mapping) else None
    if isinstance(bindings, Mapping) and len(bindings) == 1:
        return str(next(iter(bindings.keys())))
    return None


def _domain_binding(manifest: Mapping[str, Any] | None, alias: str) -> Mapping[str, Any] | None:
    data_plan = (manifest or {}).get("data_plan")
    bindings = data_plan.get("bindings") if isinstance(data_plan, Mapping) else None
    if not isinstance(bindings, Mapping):
        return None
    binding = bindings.get(alias)
    return binding if isinstance(binding, Mapping) else None


def _row_candidates(source: Mapping[str, Any]) -> list[Any] | None:
    for key in ("rows", "records"):
        value = source.get(key)
        if isinstance(value, list):
            return value
    data = source.get("data")
    if isinstance(data, Mapping):
        for key in ("rows", "records"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return None


def _default_design_contract() -> dict[str, Any]:
    return {"kit": APP_KIT_NAME, "theme_modes": ["dark", "light"]}


def _default_thumbnail(app_name: str) -> dict[str, Any]:
    return {
        "label": app_name,
        "status": DEFAULT_THUMBNAIL_STATUS,
        "secondary": "Generated app",
    }


def _display_name(*, name: str | None, key: str | None) -> str:
    return str(name or key or "Workspace App").strip() or "Workspace App"


def _label(value: str) -> str:
    words = re.sub(r"[_-]+", " ", value).strip()
    return words[:1].upper() + words[1:] if words else value


def _slug(value: Any) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return slug or "view"


def _escape_html(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _json_dumps(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), separators=(",", ":"), default=str)


def _record(repairs: list[dict[str, Any]], field: str, message: str) -> None:
    repairs.append({"field": field, "message": message})


__all__ = [
    "WorkspaceAppCompileResult",
    "compile_workspace_app_input",
    "contract_repair_guidance",
    "infer_renderer_defaults",
    "service_error_guidance",
]

"""Generated workspace app contract validation."""
from __future__ import annotations

import re
import json
from html.parser import HTMLParser
from typing import Any, Mapping

from brain.systems.workspace_apps.capabilities import (
    CAPABILITY_BINDING_KINDS,
    DOMAIN_BROKER_OPERATIONS,
    DOMAIN_OPERATIONS,
    SYSTEM_READ_OPERATIONS,
)

CONTRACT_VERSION = 1
APP_KIT_NAME = "constellation-app-kit"
APP_CAPSULE_RENDERER_KEY = "app-capsule"
APP_CAPSULE_SOURCE_KIND = "html"
STRUCTURED_UI_RENDERER_KEY = "generated-ui-app"
STRUCTURED_UI_SOURCE_KIND = "json"
ACTION_KINDS = {"connector", "domain", "workflow", "agent", "server"}
ACTION_EFFECTS = {
    "domain.read",
    "domain.write",
    "app_state.read",
    "app_state.write",
    "external.read",
    "external.write",
    "workflow.trigger",
    "agent.run",
}
ACTION_EXECUTOR_TYPES = {"registered", "deferred"}
APP_LOCAL_SCOPES = {"ui_state", "preferences", "filters", "draft", "ephemeral"}
GENERATED_UI_VIEW_TYPES = {"table", "list", "cards", "board", "chart", "metrics", "detail", "form"}
GENERATED_UI_CHART_TYPES = {"bar", "line", "pie", "scatter"}
GENERIC_HTTP_EXECUTOR_KEY = "generic.http"
GENERIC_HTTP_KINDS = {"http_request", "http_sync"}
GENERIC_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
GENERIC_HTTP_MAPPING_KEYS = ("const", "path", "template", "if", "now")
GENERIC_HTTP_MAPPING_DESCRIPTION = "const, path, template, now, or if/then/else"
RECORD_LIKE_STATE_KEYS = {
    "records",
    "items",
    "tasks",
    "todos",
    "contacts",
    "rows",
    "entries",
    "list",
    "checklist",
    "notes",
}

_HEX_COLOR_RE = re.compile(r"(?<![\w-])#[0-9a-fA-F]{3,8}\b")
_RGB_COLOR_RE = re.compile(r"\b(?:rgb|rgba|hsl|hsla)\s*\(", re.IGNORECASE)
_NEGATIVE_LETTER_SPACING_RE = re.compile(r"letter-spacing\s*:\s*-", re.IGNORECASE)
_BODY_BACKGROUND_RE = re.compile(
    r"body\s*\{[^}]*background(?:-color)?\s*:\s*(?!transparent\b|var\(|inherit\b|unset\b|none\b)[^;}]+",
    re.IGNORECASE | re.DOTALL,
)
_EXTERNAL_SCRIPT_RE = re.compile(r"<script\b[^>]*\bsrc\s*=", re.IGNORECASE)
_EXTERNAL_STYLESHEET_RE = re.compile(
    r"<link\b[^>]*\brel\s*=\s*['\"]?stylesheet|<link\b[^>]*\bhref\s*=\s*['\"]https?://",
    re.IGNORECASE,
)
_CSS_IMPORT_RE = re.compile(r"@import\s+", re.IGNORECASE)
_REMOTE_FONT_RE = re.compile(r"fonts\.(?:googleapis|gstatic)\.com", re.IGNORECASE)
_SECRET_KEY_RE = re.compile(
    r"(?:^|[_-])(?:token|secret|password|api[_-]?key|authorization|bearer|client[_-]?secret|private[_-]?key)(?:$|[_-])",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(r"\b(?:ghp_|github_pat_|sk-|xox[baprs]-)[A-Za-z0-9_=-]{8,}")


def contract_validation_passed() -> dict[str, Any]:
    return {
        "status": "passed",
        "contract_version": CONTRACT_VERSION,
        "errors": [],
        "warnings": [],
    }


def is_prototype_metadata(metadata: Mapping[str, Any] | None) -> bool:
    return bool((metadata or {}).get("prototype"))


def record_like_state_keys(value: Mapping[str, Any] | None) -> set[str]:
    return {str(key).lower() for key in (value or {})} & RECORD_LIKE_STATE_KEYS


def build_contract_validation_report(
    *,
    renderer_key: str | None,
    source_kind: str | None,
    source_code: str | None,
    manifest: Mapping[str, Any] | None,
    visual_spec: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None = None,
    initial_state: Mapping[str, Any] | None = None,
    require_contract: bool = True,
) -> dict[str, Any]:
    """Return a machine-readable validation report for a generated app."""
    if is_prototype_metadata(metadata):
        return {
            "status": "skipped",
            "contract_version": None,
            "errors": [],
            "warnings": ["prototype app contract validation skipped"],
        }

    manifest_dict = _as_mapping(manifest)
    visual_spec_dict = _as_mapping(visual_spec)
    errors: list[str] = []
    warnings: list[str] = []

    contract_version = manifest_dict.get("contract_version")
    if contract_version != CONTRACT_VERSION:
        if require_contract:
            errors.append("manifest.contract_version must be 1")
        else:
            return {
                "status": "legacy",
                "contract_version": contract_version,
                "errors": [],
                "warnings": ["legacy app has no enforced generated-app contract"],
            }

    collaboration = _as_mapping(manifest_dict.get("collaboration"))
    _validate_data_plan(
        manifest_dict.get("data_plan"),
        errors,
        initial_state=initial_state,
        collaboration_enabled=bool(collaboration),
    )
    if _forbidden_secret_paths(manifest_dict):
        errors.append("manifest must not contain raw credentials or secret values")
    _validate_actions(manifest_dict, errors)
    _validate_collaboration(collaboration, errors)
    _validate_design_contract(manifest_dict.get("design_contract"), errors)
    _validate_thumbnail(visual_spec_dict.get("thumbnail"), errors)

    normalized_renderer = (renderer_key or "").strip() or APP_CAPSULE_RENDERER_KEY
    normalized_source_kind = (source_kind or "").strip().lower() or APP_CAPSULE_SOURCE_KIND
    if normalized_renderer == APP_CAPSULE_RENDERER_KEY and normalized_source_kind == APP_CAPSULE_SOURCE_KIND:
        _validate_app_capsule_source(source_code or "", errors)
    if normalized_renderer == "sandboxed-html-app" and normalized_source_kind == "html":
        _validate_html_source(source_code or "", errors)
    if _is_structured_ui_renderer(normalized_renderer, normalized_source_kind):
        _validate_generated_ui_source(source_code or "", errors, manifest=manifest_dict)

    return {
        "status": "passed" if not errors else "failed",
        "contract_version": contract_version,
        "errors": errors,
        "warnings": warnings,
    }


def _as_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _validate_data_plan(
    value: Any,
    errors: list[str],
    *,
    initial_state: Mapping[str, Any] | None,
    collaboration_enabled: bool = False,
) -> None:
    plan = _as_mapping(value)
    if not plan:
        errors.append("manifest.data_plan is required")
        return

    mode = str(plan.get("mode") or "").strip()
    if mode not in {"capability", "domain", "app_local"}:
        errors.append("manifest.data_plan.mode must be 'capability', 'domain', or 'app_local'")
        return

    if mode == "capability":
        bindings = plan.get("bindings")
        if bindings is None:
            return
        if not isinstance(bindings, Mapping):
            errors.append("manifest.data_plan.bindings must be an object for capability apps")
            return
        bindings = _as_mapping(bindings)
        for alias, raw_binding in bindings.items():
            _validate_capability_binding(str(alias), raw_binding, errors)
        if record_like_state_keys(initial_state) and not collaboration_enabled:
            errors.append("initial_state must not contain record-like collections; use a Domain capability binding")
        return

    if mode == "domain":
        bindings = _as_mapping(plan.get("bindings"))
        if not bindings:
            errors.append("manifest.data_plan.bindings is required for Domain-backed apps")
            return
        for alias, raw_binding in bindings.items():
            _validate_domain_binding(str(alias), raw_binding, errors)
        if record_like_state_keys(initial_state) and not collaboration_enabled:
            errors.append("initial_state must not contain record-like collections; use a Domain binding")
        return

    scope = str(plan.get("scope") or plan.get("app_local_scope") or "").strip()
    if scope not in APP_LOCAL_SCOPES and plan.get("ui_state_only") is not True:
        errors.append(
            "manifest.data_plan for app_local apps must declare a UI-only scope "
            "(ui_state, preferences, filters, draft, or ephemeral)"
        )
    if record_like_state_keys(initial_state) and not collaboration_enabled:
        errors.append("app_local initial_state must not contain record-like collections; use a Domain binding")


def _validate_collaboration(value: Mapping[str, Any], errors: list[str]) -> None:
    if not value:
        return
    mode = str(value.get("mode") or "event_sourced").strip()
    if mode != "event_sourced":
        errors.append("manifest.collaboration.mode must be 'event_sourced'")
    state_key = value.get("state_key")
    if state_key is not None and (not isinstance(state_key, str) or len(state_key.strip()) > 120):
        errors.append("manifest.collaboration.state_key must be a string up to 120 characters")
    actions = value.get("actions")
    if not isinstance(actions, Mapping) or not actions:
        errors.append("manifest.collaboration.actions must declare allowed event types")
        return
    for key, raw_action in actions.items():
        action_key = str(key or "").strip()
        prefix = f"manifest.collaboration.actions.{action_key or '<empty>'}"
        if not action_key or not re.match(r"^[a-zA-Z][\w.-]*$", action_key):
            errors.append("manifest.collaboration.actions keys must be stable identifiers")
        if not isinstance(raw_action, Mapping):
            errors.append(f"{prefix} must be an object")
            continue
        reducer = raw_action.get("reducer")
        if reducer is None:
            continue
        reducer_obj = _as_mapping(reducer)
        reducer_type = str(reducer_obj.get("type") or "").strip()
        if reducer_type not in {"choice_by_actor", "append", "set"}:
            errors.append(f"{prefix}.reducer.type must be one of: append, choice_by_actor, set")
        if not str(reducer_obj.get("state_path") or "").strip():
            errors.append(f"{prefix}.reducer.state_path is required")


def _validate_domain_binding(alias: str, value: Any, errors: list[str]) -> None:
    binding = _as_mapping(value)
    prefix = f"manifest.data_plan.bindings.{alias}"
    if not alias or not re.match(r"^[a-zA-Z][\w-]*$", alias):
        errors.append("manifest.data_plan.bindings aliases must be stable identifiers")
    domain_id = binding.get("domain_id")
    if not isinstance(domain_id, int) or domain_id <= 0:
        errors.append(f"{prefix}.domain_id must be a positive integer")
    if not str(binding.get("object_key") or "").strip():
        errors.append(f"{prefix}.object_key is required")
    operations = binding.get("operations")
    if not isinstance(operations, list) or not operations:
        errors.append(f"{prefix}.operations must list allowed operations")
        return
    invalid = sorted({str(item) for item in operations} - DOMAIN_OPERATIONS)
    if invalid:
        errors.append(f"{prefix}.operations contains unsupported operation(s): {', '.join(invalid)}")


def _validate_capability_binding(alias: str, value: Any, errors: list[str]) -> None:
    binding = _as_mapping(value)
    prefix = f"manifest.data_plan.bindings.{alias}"
    if not alias or not re.match(r"^[a-zA-Z][\w-]*$", alias):
        errors.append("manifest.data_plan.bindings aliases must be stable identifiers")

    kind = str(binding.get("kind") or "").strip()
    if kind not in CAPABILITY_BINDING_KINDS:
        errors.append(f"{prefix}.kind must be 'domain' or 'system'")
        return

    operations = binding.get("operations")
    if not isinstance(operations, list) or not operations:
        errors.append(f"{prefix}.operations must list allowed operations")
        return
    operation_set = {str(item) for item in operations}

    if kind == "domain":
        invalid = sorted(operation_set - DOMAIN_BROKER_OPERATIONS)
        if invalid:
            errors.append(f"{prefix}.operations contains unsupported domain capability operation(s): {', '.join(invalid)}")
        domain_id = binding.get("domain_id")
        if not isinstance(domain_id, int) or domain_id <= 0:
            errors.append(f"{prefix}.domain_id must be a positive integer")
        if not str(binding.get("object_key") or "").strip():
            errors.append(f"{prefix}.object_key is required")
        return

    invalid = sorted(operation_set - SYSTEM_READ_OPERATIONS)
    if invalid:
        errors.append(f"{prefix}.operations contains unsupported system operation(s): {', '.join(invalid)}")
    if not str(binding.get("source") or binding.get("source_key") or "").strip():
        errors.append(f"{prefix}.source is required for system bindings")


def _validate_actions(manifest: Mapping[str, Any], errors: list[str]) -> None:
    actions = manifest.get("actions")
    if actions is None:
        actions = _as_mapping(manifest.get("action_plan")).get("actions")
    if actions is None:
        return
    if not isinstance(actions, Mapping):
        errors.append("manifest.actions must be an object when provided")
        return
    for key, raw_action in actions.items():
        action_key = str(key or "").strip()
        prefix = f"manifest.actions.{action_key or '<empty>'}"
        if not action_key or not re.match(r"^[a-zA-Z][\w.-]*$", action_key):
            errors.append("manifest.actions keys must be stable identifiers")
        if not isinstance(raw_action, Mapping):
            errors.append(f"{prefix} must be an object")
            continue
        kind = str(raw_action.get("kind") or "connector").strip()
        if kind not in ACTION_KINDS:
            errors.append(f"{prefix}.kind must be one of: {', '.join(sorted(ACTION_KINDS))}")
        effects = raw_action.get("effects")
        if not isinstance(effects, list) or not effects:
            errors.append(f"{prefix}.effects must list allowed effects")
        else:
            invalid_effects = sorted({str(effect) for effect in effects} - ACTION_EFFECTS)
            if invalid_effects:
                errors.append(f"{prefix}.effects contains unsupported effect(s): {', '.join(invalid_effects)}")
        executor = raw_action.get("executor")
        executor_type = ""
        executor_key = ""
        if executor is not None:
            executor_obj = _as_mapping(executor)
            executor_type = str(executor_obj.get("type") or "").strip()
            executor_key = str(executor_obj.get("key") or "").strip()
            if executor_type not in ACTION_EXECUTOR_TYPES:
                errors.append(
                    f"{prefix}.executor.type must be one of: {', '.join(sorted(ACTION_EXECUTOR_TYPES))}"
                )
            if executor_type == "registered" and not executor_key:
                errors.append(f"{prefix}.executor.key is required for registered executors")
        if _forbidden_secret_paths(raw_action):
            errors.append(f"{prefix} must not contain raw credentials or secret values")
        if executor_type == "registered" and executor_key == GENERIC_HTTP_EXECUTOR_KEY:
            _validate_generic_http_action(prefix, raw_action, manifest, errors)


def _validate_generic_http_action(
    prefix: str,
    action: Mapping[str, Any],
    manifest: Mapping[str, Any],
    errors: list[str],
) -> None:
    spec = action.get("connector_spec") or action.get("connector") or action.get("http")
    if not isinstance(spec, Mapping):
        errors.append(f"{prefix}.connector_spec is required for generic.http actions")
        return

    kind = str(spec.get("kind") or spec.get("type") or "http_sync").strip()
    if kind not in GENERIC_HTTP_KINDS:
        errors.append(f"{prefix}.connector_spec.kind must be one of: {', '.join(sorted(GENERIC_HTTP_KINDS))}")

    request = spec.get("request")
    method = "GET"
    if not isinstance(request, Mapping):
        errors.append(f"{prefix}.connector_spec.request must be an object")
    else:
        method = str(request.get("method") or "GET").strip().upper()
        if method not in GENERIC_HTTP_METHODS:
            errors.append(
                f"{prefix}.connector_spec.request.method must be one of: {', '.join(sorted(GENERIC_HTTP_METHODS))}"
            )
        url = request.get("url")
        if not isinstance(url, str) or not url.strip():
            errors.append(f"{prefix}.connector_spec.request.url must be an https URL")
        elif not url.strip().startswith("https://"):
            errors.append(f"{prefix}.connector_spec.request.url must be an https URL")

    effects = {str(effect).strip() for effect in action.get("effects", [])}
    needed_effect = "external.read" if method == "GET" else "external.write"
    if needed_effect not in effects:
        errors.append(f"{prefix}.connector_spec.request.method {method} requires effect '{needed_effect}'")

    auth = spec.get("auth")
    if auth not in (None, {}, "none"):
        if not isinstance(auth, Mapping):
            errors.append(f"{prefix}.connector_spec.auth must be an object or 'none'")
        else:
            auth_type = str(auth.get("type") or "bearer").strip()
            if auth_type not in {"none", "bearer", "header"}:
                errors.append(f"{prefix}.connector_spec.auth.type must be 'none', 'bearer', or 'header'")

    sync = spec.get("sync") or spec.get("domain_sync")
    if kind == "http_sync" and sync is None:
        errors.append(f"{prefix}.connector_spec.sync is required when kind is 'http_sync'")
    if sync is not None:
        _validate_generic_http_sync(prefix, sync, manifest, effects, errors)


def _validate_generic_http_sync(
    prefix: str,
    sync: Any,
    manifest: Mapping[str, Any],
    effects: set[str],
    errors: list[str],
) -> None:
    sync_prefix = f"{prefix}.connector_spec.sync"
    if "domain.write" not in effects:
        errors.append(f"{sync_prefix} requires effect 'domain.write'")
    if not isinstance(sync, Mapping):
        errors.append(f"{sync_prefix} must be an object")
        return

    binding_alias = str(sync.get("binding") or sync.get("domain_binding") or "").strip()
    if not binding_alias:
        errors.append(f"{sync_prefix}.binding is required")
    else:
        data_plan = _as_mapping(manifest.get("data_plan"))
        bindings = _as_mapping(data_plan.get("bindings"))
        if binding_alias not in bindings:
            errors.append(f"{sync_prefix}.binding must reference a manifest.data_plan binding")

    remote_id = sync.get("remote_id") or sync.get("external_id") or "id"
    _validate_generic_http_mapping_expr(f"{sync_prefix}.remote_id", remote_id, errors)
    if "title" in sync:
        _validate_generic_http_mapping_expr(f"{sync_prefix}.title", sync.get("title"), errors)
    elif "title_path" in sync:
        _validate_generic_http_mapping_expr(f"{sync_prefix}.title_path", sync.get("title_path"), errors)

    fields = sync.get("fields")
    if not isinstance(fields, Mapping):
        errors.append(f"{sync_prefix}.fields must be an object")
        return
    for field_key, expr in fields.items():
        field_name = str(field_key or "").strip()
        if not field_name:
            errors.append(f"{sync_prefix}.fields keys must be non-empty")
            continue
        _validate_generic_http_mapping_expr(f"{sync_prefix}.fields.{field_name}", expr, errors)


def _validate_generic_http_mapping_expr(
    field_path: str,
    expr: Any,
    errors: list[str],
    *,
    branch_literal: bool = False,
) -> None:
    if isinstance(expr, Mapping):
        present = [key for key in GENERIC_HTTP_MAPPING_KEYS if key in expr]
        if not present:
            errors.append(f"{field_path} mapping expressions must use {GENERIC_HTTP_MAPPING_DESCRIPTION}")
            return
        if len(present) > 1:
            errors.append(f"{field_path} mapping expression must use only one of const, path, template, now, or if")
            return
        key = present[0]
        if key == "path" and not isinstance(expr.get("path"), str):
            errors.append(f"{field_path}.path must be a string")
        elif key == "template" and not isinstance(expr.get("template"), str):
            errors.append(f"{field_path}.template must be a string")
        elif key == "now" and expr.get("now") is not True:
            errors.append(f"{field_path}.now must be true")
        elif key == "if":
            _validate_generic_http_condition(f"{field_path}.if", expr.get("if"), errors)
            if "then" not in expr:
                errors.append(f"{field_path}.then is required for conditional mapping expressions")
            else:
                _validate_generic_http_mapping_expr(
                    f"{field_path}.then",
                    expr.get("then"),
                    errors,
                    branch_literal=True,
                )
            if "else" in expr:
                _validate_generic_http_mapping_expr(
                    f"{field_path}.else",
                    expr.get("else"),
                    errors,
                    branch_literal=True,
                )
        return
    if expr is None:
        return
    if isinstance(expr, str):
        if not branch_literal and not expr.strip():
            errors.append(f"{field_path} mapping path must not be empty")
        return
    if not branch_literal:
        errors.append(f"{field_path} mapping expression must be a string path or object")


def _validate_generic_http_condition(field_path: str, condition: Any, errors: list[str]) -> None:
    if not isinstance(condition, Mapping):
        errors.append(f"{field_path} must be an object")
        return
    path = condition.get("path") if "path" in condition else condition.get("field")
    if not isinstance(path, str) or not path.strip():
        errors.append(f"{field_path} requires field or path")
    if "in" in condition and not isinstance(condition.get("in"), list):
        errors.append(f"{field_path}.in must be a list")
    if "exists" in condition and not isinstance(condition.get("exists"), bool):
        errors.append(f"{field_path}.exists must be a boolean")


def _validate_design_contract(value: Any, errors: list[str]) -> None:
    contract = _as_mapping(value)
    if not contract:
        errors.append("manifest.design_contract is required")
        return
    if contract.get("kit") != APP_KIT_NAME:
        errors.append(f"manifest.design_contract.kit must be '{APP_KIT_NAME}'")
    modes = contract.get("theme_modes")
    if not isinstance(modes, list) or not {"dark", "light"} <= {str(mode) for mode in modes}:
        errors.append("manifest.design_contract.theme_modes must include dark and light")


def _validate_thumbnail(value: Any, errors: list[str]) -> None:
    if value is None:
        errors.append("visual_spec.thumbnail must be structured metadata")
        return
    if isinstance(value, str):
        errors.append("visual_spec.thumbnail must be structured metadata, not HTML")
        return
    thumbnail = _as_mapping(value)
    if not thumbnail:
        errors.append("visual_spec.thumbnail must be structured metadata")
        return
    if thumbnail.get("source_code") or thumbnail.get("html"):
        errors.append("visual_spec.thumbnail must be host-rendered metadata, not iframe HTML")
    if not str(thumbnail.get("label") or "").strip():
        errors.append("visual_spec.thumbnail.label is required")
    if thumbnail.get("value") is None and thumbnail.get("status") is None:
        errors.append("visual_spec.thumbnail.value or visual_spec.thumbnail.status is required")
    if "progress" in thumbnail:
        try:
            progress = float(thumbnail["progress"])
        except (TypeError, ValueError):
            errors.append("visual_spec.thumbnail.progress must be numeric when provided")
        else:
            if progress < 0 or progress > 100:
                errors.append("visual_spec.thumbnail.progress must be between 0 and 100")


def _validate_html_source(source: str, errors: list[str]) -> None:
    if not source.strip():
        errors.append("source_code is required")
        return
    lowered = source.lower()
    if "illo-app" not in lowered:
        errors.append("source_code must use the Illo App Kit root class 'illo-app'")
    if "localstorage" in lowered or "sessionstorage" in lowered or "indexeddb" in lowered:
        errors.append("source_code must not use browser storage for app data")
    if _EXTERNAL_SCRIPT_RE.search(source):
        errors.append("source_code must not load external scripts")
    if _EXTERNAL_STYLESHEET_RE.search(source) or _CSS_IMPORT_RE.search(source) or _REMOTE_FONT_RE.search(source):
        errors.append("source_code must not load external stylesheets or fonts")
    if _HEX_COLOR_RE.search(source) or _RGB_COLOR_RE.search(source):
        errors.append("source_code must not hardcode visual colors; use Illo App Kit classes/tokens")
    if _NEGATIVE_LETTER_SPACING_RE.search(source):
        errors.append("source_code must not use negative letter spacing")
    if _BODY_BACKGROUND_RE.search(source):
        errors.append("source_code must not set a fixed body background")
    _validate_app_kit_html(source, errors)


def _validate_app_capsule_source(source: str, errors: list[str]) -> None:
    if not source.strip():
        errors.append("source_code is required for app-capsule apps")
        return
    lowered = source.lower()
    if "localstorage" in lowered or "sessionstorage" in lowered or "indexeddb" in lowered:
        errors.append("source_code must not use browser storage for app data")
    if _EXTERNAL_SCRIPT_RE.search(source):
        errors.append("source_code must not load external scripts")
    if _EXTERNAL_STYLESHEET_RE.search(source) or _CSS_IMPORT_RE.search(source) or _REMOTE_FONT_RE.search(source):
        errors.append("source_code must not load external stylesheets or fonts")
    if _NEGATIVE_LETTER_SPACING_RE.search(source):
        errors.append("source_code must not use negative letter spacing")
    if _BODY_BACKGROUND_RE.search(source):
        errors.append("source_code must not set a fixed body background")
    if _SECRET_VALUE_RE.search(source):
        errors.append("source_code must not contain raw credentials or secret values")


def _forbidden_secret_paths(value: Any, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            path = f"{prefix}.{key_text}" if prefix else key_text
            if _SECRET_KEY_RE.search(key_text):
                paths.append(path)
            paths.extend(_forbidden_secret_paths(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(_forbidden_secret_paths(item, f"{prefix}[{index}]"))
    elif isinstance(value, str) and _SECRET_VALUE_RE.search(value):
        paths.append(prefix or "<value>")
    return paths


def _is_structured_ui_renderer(renderer_key: str, source_kind: str) -> bool:
    return renderer_key == STRUCTURED_UI_RENDERER_KEY or source_kind in {
        STRUCTURED_UI_SOURCE_KIND,
        "generated-ui",
        "generated_ui",
    }


def _validate_generated_ui_source(
    source: str,
    errors: list[str],
    *,
    manifest: Mapping[str, Any] | None = None,
) -> None:
    if not source.strip():
        errors.append("source_code is required for generated-ui apps")
        return
    try:
        spec = json.loads(source)
    except json.JSONDecodeError as exc:
        errors.append(f"source_code must be valid generated UI JSON: {exc.msg}")
        return
    if not isinstance(spec, dict):
        errors.append("generated UI source_code must be a JSON object")
        return

    schema_version = spec.get("schema_version", spec.get("version"))
    if schema_version not in {1, "1", None}:
        errors.append("generated UI schema_version must be 1 when provided")
    if not str(spec.get("title") or "").strip():
        errors.append("generated UI spec.title is required")

    _validate_generated_ui_actions(spec, errors, manifest=manifest)

    views = spec.get("views")
    if not isinstance(views, list) or not views:
        errors.append("generated UI spec.views must contain at least one view")
        return

    for index, raw_view in enumerate(views):
        if not isinstance(raw_view, dict):
            errors.append(f"generated UI spec.views[{index}] must be an object")
            continue
        view_type = str(raw_view.get("type") or "").strip()
        if view_type not in GENERATED_UI_VIEW_TYPES:
            errors.append(
                f"generated UI spec.views[{index}].type must be one of: "
                f"{', '.join(sorted(GENERATED_UI_VIEW_TYPES))}"
            )
        if view_type in {"table", "list", "cards", "detail", "form"}:
            columns = raw_view.get("columns") or raw_view.get("fields")
            if columns is not None and not isinstance(columns, list):
                errors.append(f"generated UI spec.views[{index}].columns/fields must be a list when provided")
            if isinstance(columns, list):
                for col_index, raw_column in enumerate(columns):
                    if not isinstance(raw_column, dict) or not str(raw_column.get("key") or "").strip():
                        errors.append(
                            f"generated UI spec.views[{index}].columns[{col_index}] must include a key"
                        )
        if view_type == "board":
            group_by = raw_view.get("group_by", raw_view.get("groupBy"))
            if group_by is not None and not str(group_by or "").strip():
                errors.append(f"generated UI spec.views[{index}].group_by must be non-empty when provided")
        if view_type == "chart":
            chart_type = str(raw_view.get("chart_type") or raw_view.get("chart") or "bar").strip()
            if chart_type not in GENERATED_UI_CHART_TYPES:
                errors.append(
                    f"generated UI spec.views[{index}].chart_type must be one of: "
                    f"{', '.join(sorted(GENERATED_UI_CHART_TYPES))}"
                )


def _validate_generated_ui_actions(
    spec: Mapping[str, Any],
    errors: list[str],
    *,
    manifest: Mapping[str, Any] | None = None,
) -> None:
    actions = spec.get("actions")
    if actions is None:
        return
    if not isinstance(actions, list):
        errors.append("generated UI spec.actions must be a list when provided")
        return
    declared_action_keys = _declared_action_keys(manifest or {})
    for index, raw_action in enumerate(actions):
        key = ""
        if isinstance(raw_action, str):
            key = raw_action.strip()
        elif isinstance(raw_action, Mapping):
            key = str(raw_action.get("key") or raw_action.get("action_key") or "").strip()
            payload = raw_action.get("payload")
            if payload is not None and not isinstance(payload, Mapping):
                errors.append(f"generated UI spec.actions[{index}].payload must be an object when provided")
            if _forbidden_secret_paths(raw_action):
                errors.append(f"generated UI spec.actions[{index}] must not contain raw credentials or secret values")
        else:
            errors.append(f"generated UI spec.actions[{index}] must be an action key or object")
            continue
        if not key or not re.match(r"^[a-zA-Z][\w.-]*$", key):
            errors.append(f"generated UI spec.actions[{index}].key must be a stable action identifier")
        elif key not in declared_action_keys:
            errors.append(
                f"generated UI spec.actions[{index}].key must reference a manifest.actions declaration"
            )


def _declared_action_keys(manifest: Mapping[str, Any]) -> set[str]:
    actions = manifest.get("actions")
    if actions is None:
        actions = _as_mapping(manifest.get("action_plan")).get("actions")
    if not isinstance(actions, Mapping):
        return set()
    return {str(key or "").strip() for key in actions.keys() if str(key or "").strip()}


class _AppKitHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, set[str], dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {str(key).lower(): (value or "") for key, value in attrs}
        classes = {item for item in attrs_dict.get("class", "").split() if item}
        self.tags.append((tag.lower(), classes, attrs_dict))


def _has_class(tags: list[tuple[str, set[str], dict[str, str]]], class_name: str) -> bool:
    return any(class_name in classes for _, classes, _ in tags)


def _validate_app_kit_html(source: str, errors: list[str]) -> None:
    parser = _AppKitHtmlParser()
    try:
        parser.feed(source)
    except Exception:
        return

    tags = parser.tags
    if not _has_class(tags, "illo-panel"):
        errors.append("source_code must use at least one Illo App Kit panel class 'illo-panel'")

    for tag, classes, _attrs in tags:
        if tag == "button" and "illo-button" not in classes:
            errors.append("button elements must use the Illo App Kit class 'illo-button'")
            break

    for tag, classes, attrs in tags:
        input_type = attrs.get("type", "").strip().lower()
        if tag == "input" and input_type not in {"hidden", "checkbox", "radio"} and "illo-input" not in classes:
            errors.append("text input elements must use the Illo App Kit class 'illo-input'")
            break

    for tag, classes, _attrs in tags:
        if tag == "textarea" and "illo-textarea" not in classes:
            errors.append("textarea elements must use the Illo App Kit class 'illo-textarea'")
            break

    for tag, classes, _attrs in tags:
        if tag == "select" and "illo-select" not in classes:
            errors.append("select elements must use the Illo App Kit class 'illo-select'")
            break

    for tag, classes, _attrs in tags:
        if tag in {"ul", "ol"} and "illo-list" not in classes:
            errors.append("list elements must use the Illo App Kit class 'illo-list'")
            break

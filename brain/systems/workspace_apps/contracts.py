"""Generated workspace app contract validation."""
from __future__ import annotations

import re
import json
from html.parser import HTMLParser
from typing import Any, Mapping


CONTRACT_VERSION = 1
APP_KIT_NAME = "constellation-app-kit"
STRUCTURED_UI_RENDERER_KEY = "generated-ui-app"
STRUCTURED_UI_SOURCE_KIND = "json"
DOMAIN_OPERATIONS = {"schema", "list", "query", "get", "create", "update", "archive"}
APP_LOCAL_SCOPES = {"ui_state", "preferences", "filters", "draft", "ephemeral"}
GENERATED_UI_VIEW_TYPES = {"table", "list", "cards", "chart", "metrics", "detail", "form"}
GENERATED_UI_CHART_TYPES = {"bar", "line", "pie", "scatter"}
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

    _validate_data_plan(manifest_dict.get("data_plan"), errors, initial_state=initial_state)
    _validate_design_contract(manifest_dict.get("design_contract"), errors)
    _validate_thumbnail(visual_spec_dict.get("thumbnail"), errors)

    normalized_renderer = (renderer_key or "").strip() or STRUCTURED_UI_RENDERER_KEY
    normalized_source_kind = (source_kind or "").strip().lower() or STRUCTURED_UI_SOURCE_KIND
    if normalized_renderer == "sandboxed-html-app" and normalized_source_kind == "html":
        _validate_html_source(source_code or "", errors)
    if _is_structured_ui_renderer(normalized_renderer, normalized_source_kind):
        _validate_generated_ui_source(source_code or "", errors)

    return {
        "status": "passed" if not errors else "failed",
        "contract_version": contract_version,
        "errors": errors,
        "warnings": warnings,
    }


def _as_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _validate_data_plan(value: Any, errors: list[str], *, initial_state: Mapping[str, Any] | None) -> None:
    plan = _as_mapping(value)
    if not plan:
        errors.append("manifest.data_plan is required")
        return

    mode = str(plan.get("mode") or "").strip()
    if mode not in {"domain", "app_local"}:
        errors.append("manifest.data_plan.mode must be 'domain' or 'app_local'")
        return

    if mode == "domain":
        bindings = _as_mapping(plan.get("bindings"))
        if not bindings:
            errors.append("manifest.data_plan.bindings is required for Domain-backed apps")
            return
        for alias, raw_binding in bindings.items():
            _validate_domain_binding(str(alias), raw_binding, errors)
        if record_like_state_keys(initial_state):
            errors.append("initial_state must not contain record-like collections; use a Domain binding")
        return

    scope = str(plan.get("scope") or plan.get("app_local_scope") or "").strip()
    if scope not in APP_LOCAL_SCOPES and plan.get("ui_state_only") is not True:
        errors.append(
            "manifest.data_plan for app_local apps must declare a UI-only scope "
            "(ui_state, preferences, filters, draft, or ephemeral)"
        )
    if record_like_state_keys(initial_state):
        errors.append("app_local initial_state must not contain record-like collections; use a Domain binding")


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


def _is_structured_ui_renderer(renderer_key: str, source_kind: str) -> bool:
    return renderer_key == STRUCTURED_UI_RENDERER_KEY or source_kind in {
        STRUCTURED_UI_SOURCE_KIND,
        "generated-ui",
        "generated_ui",
    }


def _validate_generated_ui_source(source: str, errors: list[str]) -> None:
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
        if view_type == "chart":
            chart_type = str(raw_view.get("chart_type") or raw_view.get("chart") or "bar").strip()
            if chart_type not in GENERATED_UI_CHART_TYPES:
                errors.append(
                    f"generated UI spec.views[{index}].chart_type must be one of: "
                    f"{', '.join(sorted(GENERATED_UI_CHART_TYPES))}"
                )


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

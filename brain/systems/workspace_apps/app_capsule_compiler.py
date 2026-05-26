"""Small repairs/defaults for app-capsule HTML source."""
from __future__ import annotations

from typing import Any, Mapping


def compile_app_capsule_source(
    source_code: str | None,
    *,
    app_name: str,
    parsed_source: Mapping[str, Any] | None,
    repairs: list[dict[str, Any]],
) -> str | None:
    if source_code and source_code.strip() and not source_code.strip().startswith("{"):
        return source_code

    spec = dict(parsed_source or {})
    if spec:
        repairs.append({"field": "source_code", "message": "converted structured payload into default app-capsule HTML"})
        return _default_capsule_html(app_name, spec)

    if not source_code or not source_code.strip():
        repairs.append({"field": "source_code", "message": "defaulted empty app-capsule HTML shell"})
        return _default_capsule_html(app_name, {})
    return source_code


def _default_capsule_html(app_name: str, spec: Mapping[str, Any]) -> str:
    title = str(spec.get("title") or app_name or "Workspace App").strip()
    description = str(spec.get("description") or spec.get("summary") or "Ready").strip()
    rows = _row_candidates(spec) or []
    metrics = spec.get("metrics") if isinstance(spec.get("metrics"), list) else []
    body = _default_capsule_rows(rows) if rows else _default_capsule_metrics(metrics) if metrics else _default_capsule_empty(description)
    return (
        '<main class="illo-app">\n'
        '  <section class="illo-panel illo-stack">\n'
        '    <div class="illo-toolbar">\n'
        f'      <h1 class="illo-title">{_escape_html(title)}</h1>\n'
        '      <span class="illo-badge" id="illo-app-status">Ready</span>\n'
        '    </div>\n'
        f"{body}\n"
        "  </section>\n"
        "</main>\n"
        "<script>\n"
        "  window.addEventListener('illo:state', function () {\n"
        "    const status = document.getElementById('illo-app-status');\n"
        "    if (status) status.textContent = 'Synced';\n"
        "  });\n"
        "</script>"
    )


def _default_capsule_rows(rows: list[Any]) -> str:
    record_rows = [row for row in rows if isinstance(row, Mapping)]
    keys: list[str] = []
    for row in record_rows[:5]:
        for key in row.keys():
            text = str(key)
            if text not in keys:
                keys.append(text)
    keys = keys[:6]
    if not keys:
        return _default_capsule_empty("No rows yet")

    head = "".join(f"<th>{_escape_html(_label(key))}</th>" for key in keys)
    body_rows = []
    for row in record_rows[:12]:
        cells = "".join(f"<td>{_escape_html(row.get(key, ''))}</td>" for key in keys)
        body_rows.append(f"<tr>{cells}</tr>")
    return (
        '    <div class="illo-table-wrap">\n'
        "      <table>\n"
        f"        <thead><tr>{head}</tr></thead>\n"
        f"        <tbody>{''.join(body_rows)}</tbody>\n"
        "      </table>\n"
        "    </div>"
    )


def _default_capsule_metrics(metrics: list[Any]) -> str:
    if not metrics:
        return _default_capsule_empty("Ready")
    items = []
    for index, metric in enumerate(metrics[:6]):
        if isinstance(metric, Mapping):
            label = metric.get("label") or metric.get("name") or f"Metric {index + 1}"
            value = metric.get("value", metric.get("status", "Ready"))
        else:
            label = f"Metric {index + 1}"
            value = metric
        items.append(
            '      <li class="illo-row">'
            f"<strong>{_escape_html(label)}</strong>"
            f"<span>{_escape_html(value)}</span>"
            "</li>"
        )
    return '    <ul class="illo-list">\n' + "\n".join(items) + "\n    </ul>"


def _default_capsule_empty(description: str) -> str:
    return (
        '    <div class="illo-empty">\n'
        f"      <p>{_escape_html(description)}</p>\n"
        "    </div>"
    )


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


def _label(value: str) -> str:
    words = value.replace("_", " ").replace("-", " ").strip()
    return words[:1].upper() + words[1:] if words else value


def _escape_html(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )

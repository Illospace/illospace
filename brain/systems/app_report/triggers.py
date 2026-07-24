"""Work-intake shaping for customer reports submitted from the Uwear app."""

from __future__ import annotations

import json
from typing import Any, Mapping

APP_REPORT_ENVELOPE_KIND = "app_report"
APP_REPORT_SOURCE = "app_report"
APP_REPORT_SURFACE = "uwear_app"
CUSTOMER_REQUEST_EVENT_PREFIX = "customer_request"

_REPORT_TYPES = {
    "issue": "Issue",
    "idea": "Idea",
}


class AppReportValidationError(ValueError):
    """Raised when an app-report payload does not satisfy the ingress contract."""


def build_app_report_work_intake_payload(
    *,
    org_id: str,
    authority_user_id: str,
    payload: Mapping[str, Any],
    inbound_event_id: str,
    connection_id: str | None = None,
    idempotency_key: str | None = None,
    origin: str | None = None,
    priority: int = 0,
) -> dict[str, Any]:
    """Build the canonical work-intake trigger for one in-app customer report."""

    report = app_report_payload(payload)
    event_type = app_report_event_type(report)
    target = {
        "kind": APP_REPORT_ENVELOPE_KIND,
        "event_id": str(inbound_event_id),
        "thread_id": f"app-report:{inbound_event_id}",
        "profile_id": report["profileId"],
        **({"generation_ids": report["generation_ids"]} if "generation_ids" in report else {}),
        **({"batch_ids": report["batch_ids"]} if "batch_ids" in report else {}),
    }
    metadata = {
        "origin": str(origin or "uwear.app_report"),
        "signal_kind": "customer_request",
        "originating_surface": APP_REPORT_SURFACE,
        "triggering_surface": APP_REPORT_SURFACE,
        "source_surface": APP_REPORT_SURFACE,
        "final_answer_target_surface": "headless",
        "headless": True,
        "execution_profile": "fast",
        "app_report": report,
        "app_report_connection_id": connection_id,
        "inbound_event": {
            "event_id": str(inbound_event_id),
            "origin": str(origin or "uwear.app_report"),
            "kind": APP_REPORT_ENVELOPE_KIND,
            "connection_id": connection_id,
        },
    }
    return {
        "source": APP_REPORT_SOURCE,
        "event_type": event_type,
        "actor": {
            "id": str(authority_user_id),
            "principal_type": "external_uwear_customer",
            "role": "customer",
            "name": report["email"],
            "org_id": str(org_id),
            "metadata": {
                "auth_source": "app_report_connection_authority",
                "reporter_email": report["email"],
                "reporter_profile_id": report["profileId"],
                **({"connection_id": connection_id} if connection_id else {}),
            },
        },
        "org_id": str(org_id),
        "target": target,
        "payload": {
            "app_report": report,
            "thread_message": report["message"],
            "run_message": app_report_run_message(report),
            "workspace_ref": {
                "source": APP_REPORT_SURFACE,
                "mode": "customer_request_signal",
            },
            "metadata": metadata,
            "priority": int(priority),
            "user_id": str(authority_user_id),
        },
        "idempotency_key": idempotency_key,
        "policy": {
            "route": "run",
            "run_event": event_type.split(".", 1)[-1],
            "priority": int(priority),
            "auth_path": "app_report_connection",
        },
    }


def app_report_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize the uwearaiapp-to-Illo payload contract."""

    data = dict(payload or {})
    email = _required_text(data.get("email"), "email")
    profile_id = _required_text(data.get("profileId"), "profileId")
    report_type = _report_type(data.get("type"))
    message = _required_text(data.get("message"), "message")
    if "attachments" not in data:
        raise AppReportValidationError("attachments is required")
    attachments = data["attachments"]
    if not isinstance(attachments, list):
        raise AppReportValidationError("attachments must be an array")

    report: dict[str, Any] = {
        "email": email,
        "profileId": profile_id,
        "type": report_type,
        "message": message,
        "attachments": list(attachments),
    }
    for field_name in ("generation_ids", "batch_ids"):
        if field_name in data and data[field_name] is not None:
            report[field_name] = _identifier_list(data[field_name], field_name)
    return report


def app_report_event_type(payload: Mapping[str, Any]) -> str:
    report_type = _report_type(payload.get("type"))
    return f"{CUSTOMER_REQUEST_EVENT_PREFIX}.{report_type.lower()}"


def app_report_run_message(payload: Mapping[str, Any]) -> str:
    report = app_report_payload(payload)
    generation_ids = report.get("generation_ids") or []
    batch_ids = report.get("batch_ids") or []
    attachment_preview = _json_preview(report["attachments"], limit=2000)
    return "\n".join(
        [
            f"A Uwear customer submitted an in-app {report['type']} report.",
            "Treat this as a customer-request signal admitted through Illo's shared work-intake lane.",
            "Coordinate any follow-up through durable work surfaces; do not execute product changes directly.",
            "Use only the supplied generation and batch ids for deterministic dossier joins; do not guess a causing generation.",
            "The inbound admission response already contains the reporter acknowledgement.",
            "",
            f"Reporter email: {report['email']}",
            f"Reporter profile id: {report['profileId']}",
            f"Generation ids: {json.dumps(generation_ids, ensure_ascii=False)}",
            f"Batch ids: {json.dumps(batch_ids, ensure_ascii=False)}",
            f"Attachments: {attachment_preview}",
            "",
            f"Customer message: {report['message']}",
        ]
    )


def _report_type(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    report_type = _REPORT_TYPES.get(normalized)
    if report_type is None:
        raise AppReportValidationError("type must be Issue or Idea")
    return report_type


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise AppReportValidationError(f"{field_name} is required")
    return text


def _identifier_list(value: Any, field_name: str) -> list[int | str]:
    if not isinstance(value, list):
        raise AppReportValidationError(f"{field_name} must be an array")
    identifiers: list[int | str] = []
    for identifier in value:
        if isinstance(identifier, bool) or not isinstance(identifier, int | str):
            raise AppReportValidationError(
                f"{field_name} entries must be integer or string identifiers"
            )
        if isinstance(identifier, str):
            identifier = identifier.strip()
            if not identifier:
                raise AppReportValidationError(f"{field_name} entries cannot be empty")
        identifiers.append(identifier)
    return identifiers


def _json_preview(value: Any, *, limit: int) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)].rstrip()}..."


__all__ = [
    "APP_REPORT_ENVELOPE_KIND",
    "APP_REPORT_SOURCE",
    "APP_REPORT_SURFACE",
    "AppReportValidationError",
    "app_report_event_type",
    "app_report_payload",
    "app_report_run_message",
    "build_app_report_work_intake_payload",
]

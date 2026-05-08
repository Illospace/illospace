"""Backend-owned evidence ledger normalization.

Evidence records are compact, JSON-safe summaries of backend-observed tool
calls and execution artifacts. They are intentionally separate from worker
claims: models may state conclusions, but this module records what the backend
actually saw.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any

EVIDENCE_RECORD_TYPE = "evidence_record"
EVIDENCE_SCHEMA_VERSION = 1

_MAX_SUMMARY_CHARS = 360
_MAX_TEXT_CHARS = 1200
_MAX_PREVIEW_CHARS = 600
_MAX_MAPPING_ITEMS = 40
_MAX_SEQUENCE_ITEMS = 40

_FAILED_STATUSES = {"blocked", "error", "failed"}


@dataclass(frozen=True)
class EvidenceRecord:
    """A durable, JSON-safe evidence record."""

    kind: str
    source: str
    status: str
    summary: str
    details: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        details = _json_safe(self.details)
        provenance = _json_safe(self.provenance)
        payload = {
            "type": EVIDENCE_RECORD_TYPE,
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "kind": _clean_text(self.kind, 80) or "observation",
            "source": _clean_text(self.source, 160) or "unknown",
            "status": _clean_text(self.status, 80) or "observed",
            "summary": _clean_text(self.summary, _MAX_SUMMARY_CHARS) or "Evidence observed.",
            "details": details if isinstance(details, dict) else {},
            "provenance": provenance if isinstance(provenance, dict) else {},
        }
        payload = _prune_empty(payload)
        payload.setdefault("details", {})
        payload["dedupe_key"] = evidence_dedupe_key(payload)
        return payload


def normalize_tool_call_evidence(
    tool_name: str,
    args: Mapping[str, Any] | None = None,
    result: Any = None,
    *,
    provenance: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Normalize one backend tool call into ledger evidence records."""

    tool_name = str(tool_name or "").strip()
    args_map = dict(args or {}) if isinstance(args, Mapping) else {}
    result_map = _coerce_result_mapping(result)
    provenance_map = dict(provenance or {}) if isinstance(provenance, Mapping) else {}

    builder = {
        "exec_command": _command_tool_record,
        "run_script": _command_tool_record,
        "read_file": _file_tool_record,
        "write_file": _file_tool_record,
        "edit_file": _file_tool_record,
        "search_files": _search_tool_record,
        "list_files": _search_tool_record,
        "brain_recall": _brain_recall_record,
        "query_workspace_data": _workspace_data_record,
        "read_workspace_overview": _workspace_data_record,
        "read_team_activity": _workspace_data_record,
        "read_project_contexts": _workspace_data_record,
        "read_team_members": _workspace_data_record,
        "read_workspace_records": _workspace_data_record,
        "read_cycles": _workspace_data_record,
        "read_workspace_apps": _workspace_data_record,
        "brain_skills": _brain_skills_record,
        "skill_view": _skill_view_record,
        "semantic_search": _semantic_search_record,
        "file_summary": _file_summary_record,
    }.get(tool_name)

    if builder is None:
        return []
    record = builder(tool_name, args_map, result_map, provenance_map)
    return [record] if record else []


def normalize_tool_call_records_evidence(
    records: Iterable[Mapping[str, Any]] | None,
    *,
    provenance: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Normalize a list of stored tool-call records into evidence records."""

    if not records:
        return []
    normalized: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        tool_name = record.get("tool_name") or record.get("tool") or record.get("name")
        args = record.get("args") or record.get("tool_input") or record.get("input") or {}
        result = (
            record.get("result")
            if "result" in record
            else record.get("result_text", record.get("output", record.get("content")))
        )
        record_provenance = provenance
        if record_provenance is None and isinstance(record.get("provenance"), Mapping):
            record_provenance = record.get("provenance")
        normalized.extend(
            normalize_tool_call_evidence(
                str(tool_name or ""),
                args if isinstance(args, Mapping) else {},
                result,
                provenance=record_provenance,
            )
        )
    return dedupe_evidence_records(normalized)


def normalize_execution_artifact_evidence(
    artifact: Mapping[str, Any],
    *,
    provenance: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Normalize one existing execution artifact into ledger evidence records."""

    if not isinstance(artifact, Mapping):
        return []

    if artifact.get("type") == EVIDENCE_RECORD_TYPE:
        record = coerce_evidence_record(artifact)
        return [record] if record else []

    artifact_type = str(artifact.get("type") or "artifact")
    provenance_map = _artifact_provenance(artifact, provenance)

    if artifact_type in {"file_observation", "file"}:
        path = _first_text(
            artifact.get("path"),
            artifact.get("relative_path"),
            artifact.get("absolute_path"),
        )
        if not path:
            return []
        status = _status_from_error_or_value(artifact, artifact.get("operation") or artifact.get("status") or "observed")
        summary = f"{artifact_type} {status}: {path}"
        details = _pick_details(
            artifact,
            (
                "type",
                "operation",
                "path",
                "relative_path",
                "absolute_path",
                "sha256",
                "mtime",
                "size_bytes",
                "observed_at",
                "partial_read",
                "start_line",
                "end_line",
                "total_lines",
                "pre_sha256",
                "post_sha256",
                "bytes_written",
                "execution_id",
                "worker_id",
                "node_id",
                "skill",
                "session_id",
            ),
        )
        return [_make_record("file", f"artifact:{artifact_type}", status, summary, details, provenance_map)]

    if artifact_type in {"command_run", "test_run"}:
        command = _first_text(artifact.get("command"), artifact.get("summary"))
        if not command:
            return []
        status = _status_from_error_or_value(artifact, artifact.get("status") or "observed")
        summary = f"{artifact_type} {status}: {_clean_text(command, 180)}"
        details = _pick_details(artifact, ("type", "command", "status", "exit_code", "working_dir", "summary"))
        return [_make_record("command", f"artifact:{artifact_type}", status, summary, details, provenance_map)]

    if artifact_type in {
        "branch",
        "commit",
        "push",
        "pr",
        "issue",
        "merge",
        "existing_pr_under_review",
        "worker_activity",
        "worker_assignment",
        "worker_result",
        "coordinator_synthesis_check",
    }:
        status = _status_from_error_or_value(artifact, artifact.get("status") or "observed")
        summary_target = _first_text(
            artifact.get("summary"),
            artifact.get("url"),
            artifact.get("branch"),
            artifact.get("sha"),
            artifact.get("event"),
            artifact.get("worker_id"),
        )
        summary = f"{artifact_type} {status}"
        if summary_target:
            summary += f": {_clean_text(summary_target, 180)}"
        return [
            _make_record(
                "artifact",
                f"artifact:{artifact_type}",
                status,
                summary,
                _pick_details(artifact, tuple(str(key) for key in artifact.keys())),
                provenance_map,
            )
        ]

    return [
        _make_record(
            "artifact",
            f"artifact:{artifact_type}",
            _status_from_error_or_value(artifact, artifact.get("status") or "observed"),
            f"{artifact_type} observed",
            _pick_details(artifact, tuple(str(key) for key in artifact.keys())),
            provenance_map,
        )
    ]


def normalize_execution_artifacts_evidence(
    artifacts: Iterable[Mapping[str, Any]] | None,
    *,
    provenance: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Normalize existing execution artifacts into deduped evidence records."""

    if not artifacts:
        return []
    records: list[dict[str, Any]] = []
    for artifact in artifacts:
        records.extend(normalize_execution_artifact_evidence(artifact, provenance=provenance))
    return dedupe_evidence_records(records)


def coerce_evidence_record(value: Any) -> dict[str, Any] | None:
    """Coerce a record-like value into the canonical JSON-safe shape."""

    if isinstance(value, EvidenceRecord):
        return value.to_dict()
    if not isinstance(value, Mapping):
        return None
    if value.get("type") not in (None, EVIDENCE_RECORD_TYPE):
        return None
    if not all(key in value for key in ("kind", "source", "status", "summary")):
        return None
    return _make_record(
        str(value.get("kind") or "observation"),
        str(value.get("source") or "unknown"),
        str(value.get("status") or "observed"),
        str(value.get("summary") or "Evidence observed."),
        value.get("details") if isinstance(value.get("details"), Mapping) else {},
        value.get("provenance") if isinstance(value.get("provenance"), Mapping) else {},
    )


def dedupe_evidence_records(records: Iterable[Any] | None) -> list[dict[str, Any]]:
    """Return canonical records once, preserving first-seen order."""

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in records or []:
        record = coerce_evidence_record(value)
        if not record:
            continue
        key = str(record.get("dedupe_key") or evidence_dedupe_key(record))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return deduped


def evidence_has_observations(value: Any) -> bool:
    """Return true when evidence contains at least one backend observation."""

    if isinstance(value, Mapping):
        if value.get("files") or value.get("commands") or value.get("artifacts"):
            return True
        record = coerce_evidence_record(value)
        return bool(record and record.get("status") not in _FAILED_STATUSES)

    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            record = coerce_evidence_record(item)
            if record and record.get("status") not in _FAILED_STATUSES:
                return True
    return False


def compact_evidence_previews(records: Iterable[Any] | None, *, limit: int = 8) -> list[dict[str, Any]]:
    """Return compact previews suitable for prompts, logs, and flight recorders."""

    previews: list[dict[str, Any]] = []
    for record in dedupe_evidence_records(records):
        previews.append({
            "kind": record.get("kind"),
            "source": record.get("source"),
            "status": record.get("status"),
            "summary": record.get("summary"),
        })
        if len(previews) >= max(0, int(limit)):
            break
    return previews


def evidence_dedupe_key(record: Mapping[str, Any]) -> str:
    """Return a stable hash key for a canonical evidence record."""

    payload = {
        "kind": record.get("kind"),
        "source": record.get("source"),
        "status": record.get("status"),
        "summary": record.get("summary"),
        "details": record.get("details") or {},
        "provenance": record.get("provenance") or {},
    }
    encoded = json.dumps(_json_safe(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _command_tool_record(
    tool_name: str,
    args: Mapping[str, Any],
    result: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    exit_code = result.get("exit_code")
    status = _status_from_error_or_value(result, "passed" if exit_code == 0 else "failed" if exit_code is not None else "observed")
    if tool_name == "run_script":
        description = _first_text(args.get("description"), "run_script")
        script = str(args.get("script") or "")
        command = f"run_script: {description}"
        details = {
            "tool": tool_name,
            "description": description,
            "script_sha256": sha256(script.encode("utf-8", errors="replace")).hexdigest() if script else None,
            "script_bytes": len(script.encode("utf-8", errors="replace")) if script else None,
            "timeout": args.get("timeout"),
            "exit_code": exit_code,
            "stdout_preview": _clean_text(result.get("stdout") or result.get("output"), _MAX_PREVIEW_CHARS),
            "stderr_preview": _clean_text(result.get("stderr") or result.get("error"), _MAX_PREVIEW_CHARS),
        }
    else:
        command = str(args.get("command") or "").strip()
        details = {
            "tool": tool_name,
            "command": command,
            "working_dir": args.get("working_dir") or args.get("workspace"),
            "timeout": args.get("timeout"),
            "exit_code": exit_code,
            "stdout_preview": _clean_text(result.get("stdout") or result.get("output"), _MAX_PREVIEW_CHARS),
            "stderr_preview": _clean_text(result.get("stderr") or result.get("error"), _MAX_PREVIEW_CHARS),
        }
    summary = f"{tool_name} {status}"
    if command:
        summary += f": {_clean_text(command, 180)}"
    return _make_record("command", f"tool:{tool_name}", status, summary, details, provenance)


def _file_tool_record(
    tool_name: str,
    args: Mapping[str, Any],
    result: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    path = _first_text(result.get("path"), args.get("path"))
    default_status = {
        "read_file": "observed",
        "write_file": "written",
        "edit_file": "edited",
    }.get(tool_name, "observed")
    status = _status_from_error_or_value(result, default_status)

    details: dict[str, Any] = {
        "tool": tool_name,
        "path": path,
        "requested_path": args.get("path"),
        "start_line": args.get("start_line"),
        "end_line": args.get("end_line"),
        "total_lines": result.get("total_lines"),
        "bytes": result.get("bytes"),
        "error": result.get("error"),
    }
    if tool_name == "read_file":
        details["content_preview"] = _clean_text(result.get("content"), _MAX_PREVIEW_CHARS)
    elif tool_name == "write_file":
        content = str(args.get("content") or "")
        encoded = content.encode("utf-8", errors="replace")
        details["content_sha256"] = sha256(encoded).hexdigest() if content else None
        details["content_bytes"] = len(encoded)
    elif tool_name == "edit_file":
        old_text = str(args.get("old_text") or "")
        new_text = str(args.get("new_text") or "")
        details["old_text_sha256"] = sha256(old_text.encode("utf-8", errors="replace")).hexdigest() if old_text else None
        details["new_text_sha256"] = sha256(new_text.encode("utf-8", errors="replace")).hexdigest() if new_text else None
        details["old_text_bytes"] = len(old_text.encode("utf-8", errors="replace")) if old_text else None
        details["new_text_bytes"] = len(new_text.encode("utf-8", errors="replace")) if new_text else None

    summary = f"{tool_name} {status}"
    if path:
        summary += f": {_clean_text(path, 180)}"
    return _make_record("file", f"tool:{tool_name}", status, summary, details, provenance)


def _search_tool_record(
    tool_name: str,
    args: Mapping[str, Any],
    result: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    if tool_name == "search_files":
        count = _int_or_none(result.get("count"))
        status = _status_from_error_or_value(result, "empty" if count == 0 else "observed")
        pattern = _first_text(args.get("pattern"))
        summary = f"search_files {status}: {count if count is not None else 0} matches"
        if pattern:
            summary += f" for {_clean_text(pattern, 120)}"
        details = {
            "tool": tool_name,
            "pattern": pattern,
            "path": args.get("path"),
            "glob": args.get("glob"),
            "count": count,
            "matches_preview": _clean_text(result.get("matches"), _MAX_PREVIEW_CHARS),
            "error": result.get("error"),
        }
        return _make_record("search", "tool:search_files", status, summary, details, provenance)

    total = _int_or_none(result.get("total"))
    files = result.get("files") if isinstance(result.get("files"), list) else []
    count = total if total is not None else len(files)
    status = _status_from_error_or_value(result, "empty" if count == 0 else "observed")
    summary = f"list_files {status}: {count} files"
    details = {
        "tool": tool_name,
        "pattern": args.get("pattern"),
        "path": args.get("path"),
        "total": total,
        "truncated": result.get("truncated"),
        "files_preview": files[:20],
        "error": result.get("error"),
    }
    return _make_record("search", "tool:list_files", status, summary, details, provenance)


def _brain_recall_record(
    tool_name: str,
    args: Mapping[str, Any],
    result: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    memories = result.get("memories") if isinstance(result.get("memories"), list) else []
    count = _int_or_none(result.get("count"))
    count = len(memories) if count is None else count
    status = _status_from_error_or_value(result, "empty" if count == 0 else "observed")
    query = _first_text(args.get("query"))
    details = {
        "tool": tool_name,
        "query": query,
        "limit": args.get("limit"),
        "count": count,
        "candidate_count": result.get("candidate_count"),
        "memory_ids": [_json_safe(item.get("id")) for item in memories[:10] if isinstance(item, Mapping)],
        "memory_types": [_clean_text(item.get("type"), 80) for item in memories[:10] if isinstance(item, Mapping)],
        "attention_decision": result.get("attention_decision") if isinstance(result.get("attention_decision"), Mapping) else None,
        "error": result.get("error"),
    }
    summary = f"brain_recall {status}: {count} memories"
    if query:
        summary += f" for {_clean_text(query, 120)}"
    return _make_record("memory", "tool:brain_recall", status, summary, details, provenance)


def _workspace_data_record(
    tool_name: str,
    args: Mapping[str, Any],
    result: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    counts = result.get("counts") if isinstance(result.get("counts"), Mapping) else {}
    total = _int_or_none(result.get("total_count"))
    if total is None:
        total = sum(_int_or_none(value) or 0 for value in counts.values())
    checked_sources = result.get("checked_sources") if isinstance(result.get("checked_sources"), list) else []
    warnings = result.get("warnings") if isinstance(result.get("warnings"), list) else []
    status = _status_from_error_or_value(result, "empty" if total == 0 else "observed")
    if warnings and status == "observed":
        status = "degraded"
    details = {
        "tool": tool_name,
        "query": _first_text(args.get("query")),
        "search": _first_text(args.get("search")),
        "person": _first_text(args.get("person")),
        "time_window": result.get("time_window") if isinstance(result.get("time_window"), Mapping) else None,
        "checked_sources": [_clean_text(source, 80) for source in checked_sources[:20]],
        "counts": {str(key): _int_or_none(value) for key, value in counts.items()},
        "warnings": warnings[:5],
        "scope": result.get("scope") if isinstance(result.get("scope"), Mapping) else None,
    }
    summary = f"{tool_name} {status}: {total or 0} records across {len(checked_sources)} sources"
    person = _first_text(args.get("person"))
    if person:
        summary += f" for {_clean_text(person, 80)}"
    return _make_record("workspace_data", f"tool:{tool_name}", status, summary, details, provenance)


def _brain_skills_record(
    tool_name: str,
    args: Mapping[str, Any],
    result: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    recommended = result.get("recommended_skills") if isinstance(result.get("recommended_skills"), list) else []
    count = len(recommended)
    status = _status_from_error_or_value(result, "empty" if count == 0 else "observed")
    if result.get("degraded") and status == "observed":
        status = "degraded"
    task = _first_text(args.get("task"), result.get("task"))
    details = {
        "tool": tool_name,
        "task": task,
        "strategy": result.get("strategy"),
        "recommended_skill_names": [
            _clean_text(item.get("name"), 120)
            for item in recommended[:12]
            if isinstance(item, Mapping)
        ],
        "recommended_count": count,
        "guardrail_count": len(result.get("guardrails") or []) if isinstance(result.get("guardrails"), list) else None,
        "degraded": result.get("degraded"),
        "degraded_reason": result.get("degraded_reason"),
        "skill_gap": result.get("skill_gap"),
        "error": result.get("error"),
    }
    summary = f"brain_skills {status}: {count} recommendations"
    if task:
        summary += f" for {_clean_text(task, 120)}"
    return _make_record("skill", "tool:brain_skills", status, summary, details, provenance)


def _skill_view_record(
    tool_name: str,
    args: Mapping[str, Any],
    result: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    name = _first_text(result.get("name"), args.get("name"))
    section = _first_text(result.get("section"), args.get("section"), "procedure")
    status = _status_from_error_or_value(result, "observed")
    content = _first_text(result.get("content"))
    items = result.get("items") if isinstance(result.get("items"), list) else []
    details = {
        "tool": tool_name,
        "name": name,
        "section": section,
        "loaded_sections": result.get("loaded_sections"),
        "content_type": result.get("content_type"),
        "content_sha256": sha256(content.encode("utf-8", errors="replace")).hexdigest() if content else None,
        "content_preview": _clean_text(content, _MAX_PREVIEW_CHARS),
        "items_preview": items[:12],
        "truncated": result.get("truncated"),
        "effective_digest": result.get("effective_digest"),
        "bundle_digest": result.get("bundle_digest"),
        "error": result.get("error"),
    }
    summary = f"skill_view {status}"
    if name:
        summary += f": {name}"
    if section:
        summary += f"#{section}"
    return _make_record("skill", "tool:skill_view", status, summary, details, provenance)


def _semantic_search_record(
    tool_name: str,
    args: Mapping[str, Any],
    result: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    results = result.get("results") if isinstance(result.get("results"), list) else []
    count = _int_or_none(result.get("count"))
    count = len(results) if count is None else count
    status = _status_from_error_or_value(result, "empty" if count == 0 else "observed")
    query = _first_text(args.get("query"), result.get("query"))
    details = {
        "tool": tool_name,
        "query": query,
        "scope": args.get("scope"),
        "limit": args.get("limit"),
        "count": count,
        "results_preview": [_semantic_result_preview(item) for item in results[:10] if isinstance(item, Mapping)],
        "error": result.get("error"),
    }
    summary = f"semantic_search {status}: {count} results"
    if query:
        summary += f" for {_clean_text(query, 120)}"
    return _make_record("search", "tool:semantic_search", status, summary, details, provenance)


def _file_summary_record(
    tool_name: str,
    args: Mapping[str, Any],
    result: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    path = _first_text(result.get("path"), args.get("path"))
    status = _status_from_error_or_value(result, "observed")
    details = {
        "tool": tool_name,
        "path": path,
        "size_bytes": result.get("size_bytes"),
        "extension": result.get("extension"),
        "line_count": result.get("line_count"),
        "imports_preview": result.get("imports")[:20] if isinstance(result.get("imports"), list) else None,
        "classes_preview": result.get("classes")[:12] if isinstance(result.get("classes"), list) else None,
        "functions_preview": result.get("functions")[:20] if isinstance(result.get("functions"), list) else None,
        "docstring_preview": _clean_text(result.get("docstring"), _MAX_PREVIEW_CHARS),
        "preview": _clean_text(result.get("preview"), _MAX_PREVIEW_CHARS),
        "parse_error": result.get("parse_error"),
        "error": result.get("error"),
    }
    summary = f"file_summary {status}"
    if path:
        summary += f": {_clean_text(path, 180)}"
    return _make_record("file", "tool:file_summary", status, summary, details, provenance)


def _semantic_result_preview(item: Mapping[str, Any]) -> dict[str, Any]:
    return _prune_empty({
        "source": item.get("source"),
        "path": item.get("path") or item.get("file"),
        "type": item.get("type"),
        "similarity": item.get("similarity"),
        "content_preview": _clean_text(item.get("content") or item.get("summary"), 240),
    })


def _make_record(
    kind: str,
    source: str,
    status: str,
    summary: str,
    details: Mapping[str, Any] | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return EvidenceRecord(
        kind=kind,
        source=source,
        status=status,
        summary=summary,
        details=details or {},
        provenance=provenance or {},
    ).to_dict()


def _status_from_error_or_value(payload: Mapping[str, Any], default: Any) -> str:
    if payload.get("blocked"):
        return "blocked"
    if payload.get("error") and not payload.get("ok"):
        return "failed"
    if payload.get("ok") is False:
        return "failed"
    value = _first_text(default, "observed").lower()
    return value or "observed"


def _coerce_result_mapping(result: Any) -> dict[str, Any]:
    if isinstance(result, Mapping):
        return dict(result)
    if isinstance(result, str):
        text = result.strip()
        if not text:
            return {}
        try:
            decoded, _ = json.JSONDecoder().raw_decode(text)
            if isinstance(decoded, Mapping):
                return dict(decoded)
        except Exception:
            pass
        if text.startswith("{") and text.endswith("}"):
            try:
                decoded = ast.literal_eval(text)
                if isinstance(decoded, Mapping):
                    return dict(decoded)
            except Exception:
                pass
    return {}


def _artifact_provenance(
    artifact: Mapping[str, Any],
    explicit: Mapping[str, Any] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if isinstance(artifact.get("provenance"), Mapping):
        payload.update(artifact["provenance"])
    for key in ("run_id", "execution_id", "worker_id", "node_id", "skill", "session_id"):
        if not _is_empty_value(artifact.get(key)):
            payload[key] = artifact.get(key)
    if explicit:
        payload.update({str(key): value for key, value in explicit.items() if not _is_empty_value(value)})
    return _json_safe(payload)


def _pick_details(payload: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return _prune_empty({key: payload.get(key) for key in keys})


def _json_safe(value: Any, *, depth: int = 4) -> Any:
    if depth <= 0:
        return _clean_text(value, _MAX_TEXT_CHARS)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _clean_text(value, _MAX_TEXT_CHARS)
    if isinstance(value, bytes):
        return {
            "bytes": len(value),
            "sha256": sha256(value).hexdigest(),
        }
    if isinstance(value, (datetime, Path, Enum)):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value), depth=depth - 1)
    if isinstance(value, Mapping):
        items = list(value.items())[:_MAX_MAPPING_ITEMS]
        result = {
            str(key): _json_safe(val, depth=depth - 1)
            for key, val in items
            if not _is_empty_value(val)
        }
        if len(value) > _MAX_MAPPING_ITEMS:
            result["_truncated_keys"] = len(value) - _MAX_MAPPING_ITEMS
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
        result = [_json_safe(item, depth=depth - 1) for item in values[:_MAX_SEQUENCE_ITEMS]]
        if len(values) > _MAX_SEQUENCE_ITEMS:
            result.append({"_truncated_items": len(values) - _MAX_SEQUENCE_ITEMS})
        return result
    return _clean_text(value, _MAX_TEXT_CHARS)


def _prune_empty(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): _json_safe(value)
        for key, value in payload.items()
        if not _is_empty_value(value)
    }


def _clean_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, str) and value == "":
        return ""
    text = str(value).replace("\x00", "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 24)].rstrip() + f" ... ({len(text)} chars)"


def _first_text(*values: Any) -> str:
    for value in values:
        text = _clean_text(value, _MAX_TEXT_CHARS)
        if text:
            return text
    return ""


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value == ""
    if isinstance(value, Mapping):
        return len(value) == 0
    if isinstance(value, (list, tuple, set, frozenset)):
        return len(value) == 0
    return False


__all__ = [
    "EVIDENCE_RECORD_TYPE",
    "EVIDENCE_SCHEMA_VERSION",
    "EvidenceRecord",
    "coerce_evidence_record",
    "compact_evidence_previews",
    "dedupe_evidence_records",
    "evidence_dedupe_key",
    "evidence_has_observations",
    "normalize_execution_artifact_evidence",
    "normalize_execution_artifacts_evidence",
    "normalize_tool_call_evidence",
    "normalize_tool_call_records_evidence",
]

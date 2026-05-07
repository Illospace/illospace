"""Deterministic context-pack usefulness signals for after-run learning.

This module is intentionally local and low-cost: no model calls, no repository
scans, and no raw selected-memory text in the emitted learning payload.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
import hashlib
import json
import re
from typing import Any

from brain.kernel.common.coercion import as_mapping as _shared_as_mapping
from brain.kernel.common.coercion import drop_none as _shared_drop_none
from brain.kernel.common.coercion import int_or_none as _shared_int_or_none
from brain.kernel.common.coercion import optional_text as _shared_optional_text

CONTEXT_USEFULNESS_SCHEMA_VERSION = 1
CONTEXT_USEFULNESS_SIGNAL_SOURCE = "context_signals.v1"
CONTEXT_USEFULNESS_LABELS = {"useful", "unused", "missed", "over_budget", "unknown"}

_LABEL_PRIORITY = {
    "unknown": 0,
    "unused": 1,
    "useful": 2,
    "missed": 3,
    "over_budget": 4,
}
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_.:/-]{2,}", re.IGNORECASE)
_STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "because",
    "before",
    "being",
    "between",
    "could",
    "from",
    "have",
    "into",
    "just",
    "like",
    "more",
    "must",
    "once",
    "only",
    "over",
    "previously",
    "should",
    "that",
    "their",
    "there",
    "these",
    "this",
    "through",
    "were",
    "when",
    "where",
    "with",
    "would",
}


def stable_context_item_digest(
    kind: str,
    payload: Mapping[str, Any],
    *,
    length: int = 24,
) -> str:
    """Return a stable, compact digest for a context item identity."""
    basis = {
        "schema_version": CONTEXT_USEFULNESS_SCHEMA_VERSION,
        "kind": str(kind or "context_item"),
        "payload": _jsonable(payload),
    }
    return _stable_digest(basis)[:length]


def stable_text_digest(value: Any, *, length: int = 64) -> str | None:
    """Digest text without returning the text itself."""
    text = _text(value)
    if not text:
        return None
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def memory_item_reference(memory: Mapping[str, Any], *, index: int | None = None) -> dict[str, Any]:
    """Build a privacy-safe reference for a selected memory item."""
    memory = _mapping(memory)
    memory_id = _text(memory.get("id")) or _text(memory.get("memory_id"))
    content_digest = (
        _text(memory.get("content_digest"))
        or _text(memory.get("claim_digest"))
        or _text(memory.get("source_digest"))
        or stable_text_digest(memory.get("content"))
    )
    identity = {
        "id": memory_id,
        "type": _text(memory.get("type")) or _text(memory.get("memory_type")),
        "tier": _text(memory.get("tier")) or _text(memory.get("memory_tier")),
        "source": _text(memory.get("source")),
        "source_ref": _text(memory.get("source_ref")),
        "content_digest": content_digest,
    }
    item_digest = _text(memory.get("item_digest")) or stable_context_item_digest("memory", identity)
    ref: dict[str, Any] = {
        "item_id": _text(memory.get("item_id")) or f"memory:{memory_id or item_digest}",
        "item_digest": item_digest,
        "content_digest": content_digest,
        "rank": _int_or_none(memory.get("rank")) or index,
        "memory_id": memory_id,
        "memory_type": identity["type"],
        "tier": identity["tier"],
    }
    return _drop_none(ref)


def skill_item_reference(
    skill: Mapping[str, Any],
    *,
    role: str = "selected",
    index: int | None = None,
) -> dict[str, Any]:
    """Build a stable reference for a selected or recommended skill item."""
    skill = _mapping(skill)
    record = _mapping(skill.get("skill_record")) or skill
    name = _text(skill.get("name")) or _text(record.get("name"))
    effective_digest = (
        _text(skill.get("effective_digest"))
        or _text(record.get("effective_digest"))
        or _text(skill.get("bundle_digest"))
        or _text(record.get("bundle_digest"))
    )
    loaded_sections = _text_list(skill.get("loaded_sections") or record.get("loaded_sections"))
    identity = {
        "name": name,
        "role": role,
        "skill_version": _text(skill.get("skill_version")) or _text(record.get("skill_version")),
        "bundle_version_id": _text(skill.get("bundle_version_id")) or _text(record.get("bundle_version_id")),
        "bundle_digest": _text(skill.get("bundle_digest")) or _text(record.get("bundle_digest")),
        "effective_digest": effective_digest,
        "loaded_sections": loaded_sections,
    }
    item_digest = _text(skill.get("item_digest")) or stable_context_item_digest("skill", identity)
    ref: dict[str, Any] = {
        "item_id": _text(skill.get("item_id")) or f"skill:{name or item_digest}@{effective_digest or item_digest}",
        "item_digest": item_digest,
        "effective_digest": effective_digest,
        "name": name,
        "rank": _int_or_none(skill.get("rank")) or index,
        "role": role,
        "loaded_sections": loaded_sections,
        "asset_refs": skill_asset_references_from_skill(skill),
    }
    return _drop_none(ref)


def skill_asset_reference(
    *,
    skill_name: str | None = None,
    path: str | None = None,
    content_digest: str | None = None,
    effective_digest: str | None = None,
    bundle_version_id: Any = None,
) -> dict[str, Any]:
    """Build a stable, raw-content-free reference for a skill asset."""
    identity = {
        "skill_name": _text(skill_name),
        "path": _text(path),
        "content_digest": _text(content_digest),
        "effective_digest": _text(effective_digest),
        "bundle_version_id": _text(bundle_version_id),
    }
    item_digest = stable_context_item_digest("skill_asset", identity)
    path_text = identity["path"] or item_digest
    ref = {
        "item_id": f"skill_asset:{identity['skill_name'] or 'unknown'}:{path_text}",
        "item_digest": item_digest,
        "skill_name": identity["skill_name"],
        "path": identity["path"],
        "content_digest": identity["content_digest"],
        "effective_digest": identity["effective_digest"],
    }
    return _drop_none(ref)


def skill_asset_references_from_skill(skill: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract stable asset refs from a skill payload without raw asset content."""
    skill = _mapping(skill)
    record = _mapping(skill.get("skill_record")) or skill
    skill_name = _text(skill.get("name")) or _text(record.get("name"))
    effective_digest = _text(record.get("effective_digest")) or _text(skill.get("effective_digest"))
    bundle_version_id = record.get("bundle_version_id") or skill.get("bundle_version_id")
    refs: list[dict[str, Any]] = []

    for item in _iter_asset_like_values(skill):
        item = _mapping(item)
        path = _text(item.get("path")) or _text(item.get("asset_path"))
        if not path:
            continue
        refs.append(skill_asset_reference(
            skill_name=skill_name,
            path=path,
            content_digest=_text(item.get("content_digest")) or _text(item.get("digest")),
            effective_digest=effective_digest,
            bundle_version_id=bundle_version_id,
        ))

    for section in _text_list(skill.get("loaded_sections") or record.get("loaded_sections")):
        if not section.startswith("asset:"):
            continue
        path = section.split(":", 1)[1].strip()
        if path:
            refs.append(skill_asset_reference(
                skill_name=skill_name,
                path=path,
                effective_digest=effective_digest,
                bundle_version_id=bundle_version_id,
            ))

    return _dedupe_refs(refs)


def build_context_usefulness_payload(
    *,
    trajectory: Mapping[str, Any] | None,
    eval_case: Mapping[str, Any] | None = None,
    runtime_metadata: Mapping[str, Any] | None = None,
    run_id: int | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Build deterministic context usefulness labels from completed-run facts."""
    trajectory = _mapping(trajectory)
    eval_case = _mapping(eval_case)
    runtime_metadata = _mapping(runtime_metadata)
    context_pack = _mapping(trajectory.get("context_pack") or runtime_metadata.get("context_pack"))
    context = {
        **_mapping(runtime_metadata.get("context")),
        **_mapping(trajectory.get("context")),
    }
    context_pack_digest = _context_pack_digest(trajectory, eval_case, runtime_metadata)
    if not context_pack and not context and not context_pack_digest:
        return {}

    final_strings = _collect_strings(_mapping(trajectory.get("final_output")))
    tool_calls = [item for item in trajectory.get("tool_calls") or [] if isinstance(item, Mapping)]
    tool_arg_strings = _tool_arg_strings(tool_calls)
    tool_result_strings = _tool_result_strings(tool_calls)
    tool_names = {_text(call.get("tool_name")) for call in tool_calls}
    tool_names.discard(None)
    cognitive_misses = _text_list(context.get("cognitive_misses"))

    labels = _LabelAccumulator()
    sections = _mapping(context_pack.get("sections"))
    render_order = [
        name for name in (context_pack.get("render_order") or sections.keys())
        if isinstance(name, str) and name in sections
    ]

    for name in render_order:
        section = _mapping(sections.get(name))
        budget = _mapping(section.get("token_budget"))
        ref = _section_ref(name)
        estimated_tokens = _int_or_none(budget.get("estimated_tokens"))
        budget_tokens = _int_or_none(budget.get("budget_tokens"))
        is_over_budget = bool(budget.get("over_budget")) or (
            estimated_tokens is not None
            and budget_tokens is not None
            and estimated_tokens > budget_tokens
        )
        if is_over_budget:
            labels.add(
                ref,
                "over_budget",
                "section_estimated_tokens_exceed_budget",
                0.98,
                evidence=_budget_evidence(budget),
            )
        else:
            labels.add(
                ref,
                "unknown",
                "section_rendered_no_specific_use_signal",
                0.25,
                evidence=_budget_evidence(budget),
            )

    memory_items = _selected_memory_items(sections)
    useful_memory_count = 0
    for index, memory in enumerate(memory_items, start=1):
        ref = memory_item_reference(memory, index=index)
        target = _memory_label_ref(ref)
        content = _text(memory.get("content"))
        match = _memory_reference_match(
            content,
            final_strings=final_strings,
            tool_arg_strings=tool_arg_strings,
        )
        if match:
            useful_memory_count += 1
            labels.add(
                target,
                "useful",
                match["reason"],
                match["confidence"],
                evidence={
                    "rank": ref.get("rank"),
                    "matched_final_output": match["matched_final_output"],
                    "matched_tool_args": match["matched_tool_args"],
                    "match_strategy": match["match_strategy"],
                },
            )
        elif final_strings or tool_arg_strings:
            labels.add(
                target,
                "unused",
                "selected_memory_not_referenced_in_final_output_or_tool_args",
                0.54,
                evidence={"rank": ref.get("rank")},
            )
        else:
            labels.add(
                target,
                "unknown",
                "no_final_output_or_tool_args_available_for_memory_match",
                0.30,
                evidence={"rank": ref.get("rank")},
            )

    selected_skill = _selected_skill(sections)
    selected_skill_ref = skill_item_reference(selected_skill, role="selected") if selected_skill else {}
    if selected_skill_ref:
        skill_reasons = _selected_skill_use_reasons(
            selected_skill_ref,
            trajectory=trajectory,
            tool_calls=tool_calls,
            plan_texts=[*tool_arg_strings, *tool_result_strings],
            brain_skills_used=bool(context.get("brain_skills_used")),
        )
        skill_target = _skill_label_ref(selected_skill_ref)
        if skill_reasons:
            labels.add(
                skill_target,
                "useful",
                skill_reasons[0],
                0.82,
                evidence={"reason_count": len(skill_reasons)},
            )
            for reason in skill_reasons[1:]:
                labels.add(skill_target, "useful", reason, 0.82)
        elif tool_calls or trajectory.get("worker_evidence"):
            labels.add(skill_target, "unused", "selected_skill_not_used_by_plan_or_worker", 0.55)
        else:
            labels.add(skill_target, "unknown", "no_plan_or_worker_evidence_available_for_skill", 0.30)

        for asset_ref in selected_skill_ref.get("asset_refs") or []:
            asset_target = _skill_asset_label_ref(asset_ref)
            if _asset_requested(asset_ref, tool_calls):
                labels.add(
                    asset_target,
                    "useful",
                    "selected_skill_asset_requested_by_tool",
                    0.78,
                    evidence={"path_digest": stable_text_digest(asset_ref.get("path"))},
                )
            else:
                labels.add(asset_target, "unknown", "selected_skill_asset_no_use_signal", 0.30)

    _apply_section_usefulness_labels(
        labels,
        memory_items=memory_items,
        useful_memory_count=useful_memory_count,
        selected_skill_ref=selected_skill_ref,
        context=context,
    )
    _apply_missing_context_labels(
        labels,
        context=context,
        sections=sections,
        memory_items=memory_items,
        selected_skill_ref=selected_skill_ref,
        tool_calls=tool_calls,
        cognitive_misses=cognitive_misses,
    )

    labels_list = labels.to_list()
    counts = Counter(item["label"] for item in labels_list)
    payload = {
        "schema_version": CONTEXT_USEFULNESS_SCHEMA_VERSION,
        "source": CONTEXT_USEFULNESS_SIGNAL_SOURCE,
        "run_id": run_id or _int_or_none(trajectory.get("run_id")),
        "trace_id": trace_id or _text(trajectory.get("trace_id")) or _text(eval_case.get("trace_id")),
        "trajectory_digest": _text(eval_case.get("trajectory_digest")) or _text(trajectory.get("digest")),
        "context_pack_digest": context_pack_digest,
        "context": _context_summary(context, rendered_sections=render_order),
        "labels": labels_list,
        "summary": {
            "label_count": len(labels_list),
            "counts": dict(sorted(counts.items())),
            "memory_item_count": len(memory_items),
            "selected_skill_count": 1 if selected_skill_ref else 0,
            "tool_call_count": len(tool_calls),
            "cognitive_miss_count": len(cognitive_misses),
            "raw_private_memory_exported": False,
        },
    }
    payload["digest"] = _stable_digest(payload)[:24]
    return payload


class _LabelAccumulator:
    def __init__(self) -> None:
        self._labels: dict[tuple[str, str], dict[str, Any]] = {}

    def add(
        self,
        ref: Mapping[str, Any],
        label: str,
        reason: str,
        confidence: float,
        *,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        if label not in CONTEXT_USEFULNESS_LABELS:
            label = "unknown"
        target_type = _text(ref.get("target_type")) or "context"
        target_id = (
            _text(ref.get("target_id"))
            or _text(ref.get("item_digest"))
            or _text(ref.get("section"))
            or target_type
        )
        key = (target_type, target_id)
        existing = self._labels.get(key)
        if existing is None:
            item = {
                "target_type": target_type,
                "target_id": target_id,
                "section": _text(ref.get("section")),
                "item_id": _text(ref.get("item_id")),
                "item_digest": _text(ref.get("item_digest")),
                "label": label,
                "reasons": [reason] if reason else [],
                "confidence": _confidence(confidence),
                "evidence": _jsonable(dict(evidence or {})),
            }
            self._labels[key] = _drop_none(item)
            return

        if reason and reason not in existing.setdefault("reasons", []):
            existing["reasons"].append(reason)
        if _LABEL_PRIORITY[label] > _LABEL_PRIORITY.get(existing.get("label"), 0):
            existing["label"] = label
            existing["confidence"] = _confidence(confidence)
        elif label == existing.get("label"):
            existing["confidence"] = max(existing.get("confidence") or 0.0, _confidence(confidence))
        if evidence:
            existing_evidence = _mapping(existing.get("evidence"))
            existing_evidence.update(_jsonable(dict(evidence)))
            existing["evidence"] = existing_evidence

    def to_list(self) -> list[dict[str, Any]]:
        return [
            self._labels[key]
            for key in sorted(self._labels)
        ]


def _context_pack_digest(
    trajectory: Mapping[str, Any],
    eval_case: Mapping[str, Any],
    runtime_metadata: Mapping[str, Any],
) -> str | None:
    context = _mapping(trajectory.get("context"))
    context_pack = _mapping(trajectory.get("context_pack"))
    return (
        _text(eval_case.get("context_digest"))
        or _text(context.get("context_pack_digest"))
        or _text(context_pack.get("digest"))
        or _text(runtime_metadata.get("context_pack_digest"))
    )


def _context_summary(context: Mapping[str, Any], *, rendered_sections: list[str]) -> dict[str, Any]:
    cognitive_misses = context.get("cognitive_misses") or []
    if not isinstance(cognitive_misses, list):
        cognitive_misses = []
    return {
        "brain_context_loaded": bool(context.get("brain_context_loaded")),
        "brain_recall_used": bool(context.get("brain_recall_used")),
        "brain_skills_used": bool(context.get("brain_skills_used")),
        "preloaded_memory_count": _int_or_none(context.get("preloaded_memory_count")) or 0,
        "cognitive_miss_count": len(cognitive_misses),
        "rendered_sections": list(rendered_sections),
    }


def _selected_memory_items(sections: Mapping[str, Any]) -> list[dict[str, Any]]:
    section = _mapping(sections.get("selected_memories"))
    content = _mapping(section.get("content"))
    return [dict(item) for item in content.get("items") or [] if isinstance(item, Mapping)]


def _selected_skill(sections: Mapping[str, Any]) -> dict[str, Any]:
    section = _mapping(sections.get("selected_skills"))
    content = _mapping(section.get("content"))
    selected = content.get("selected")
    return dict(selected) if isinstance(selected, Mapping) else {}


def _apply_section_usefulness_labels(
    labels: _LabelAccumulator,
    *,
    memory_items: list[dict[str, Any]],
    useful_memory_count: int,
    selected_skill_ref: Mapping[str, Any],
    context: Mapping[str, Any],
) -> None:
    memory_section = _section_ref("selected_memories")
    if useful_memory_count:
        labels.add(
            memory_section,
            "useful",
            "selected_memory_referenced_after_context_pack",
            0.72,
            evidence={"useful_memory_count": useful_memory_count},
        )
    elif memory_items and (context.get("brain_recall_used") or context.get("brain_context_loaded")):
        labels.add(memory_section, "useful", "memory_context_loaded_or_recall_used", 0.60)
    elif memory_items:
        labels.add(memory_section, "unused", "selected_memories_had_no_observed_use_signal", 0.50)

    skill_section = _section_ref("selected_skills")
    if selected_skill_ref and context.get("brain_skills_used"):
        labels.add(skill_section, "useful", "brain_skills_used", 0.70)
    elif selected_skill_ref:
        labels.add(skill_section, "unknown", "selected_skill_present_without_section_use_signal", 0.35)


def _apply_missing_context_labels(
    labels: _LabelAccumulator,
    *,
    context: Mapping[str, Any],
    sections: Mapping[str, Any],
    memory_items: list[dict[str, Any]],
    selected_skill_ref: Mapping[str, Any],
    tool_calls: list[Mapping[str, Any]],
    cognitive_misses: list[str],
) -> None:
    tool_names = {_text(call.get("tool_name")) for call in tool_calls}
    tool_names.discard(None)
    if context.get("brain_recall_used") or "brain_recall" in tool_names:
        reason = (
            "brain_recall_requested_after_empty_preload"
            if not memory_items
            else "additional_memory_context_requested_after_pack"
        )
        labels.add(_section_ref("selected_memories"), "missed", reason, 0.74)

    if "brain_skills" in tool_names and not selected_skill_ref:
        labels.add(_section_ref("selected_skills"), "missed", "brain_skills_requested_after_no_selected_skill", 0.74)

    loaded_sections = set(_text_list(selected_skill_ref.get("loaded_sections"))) if selected_skill_ref else set()
    for call in tool_calls:
        name = _text(call.get("tool_name"))
        args = _tool_args(call)
        if name == "skill_view":
            requested_section = _text(args.get("section")) or "metadata"
            if requested_section and requested_section not in loaded_sections:
                labels.add(
                    _section_ref("selected_skills"),
                    "missed",
                    "skill_section_requested_after_context_pack",
                    0.72,
                    evidence={"requested_section": requested_section},
                )
        elif name == "skill_asset":
            asset_ref = skill_asset_reference(
                skill_name=_text(args.get("name")),
                path=_text(args.get("path")),
            )
            known_assets = {
                _text(asset.get("path"))
                for asset in selected_skill_ref.get("asset_refs") or []
                if isinstance(asset, Mapping)
            }
            if asset_ref.get("path") and asset_ref.get("path") not in known_assets:
                labels.add(
                    _skill_asset_label_ref(asset_ref),
                    "missed",
                    "skill_asset_requested_after_context_pack",
                    0.76,
                    evidence={"path_digest": stable_text_digest(asset_ref.get("path"))},
                )

    for miss in cognitive_misses:
        miss_key = _normalized_miss_key(miss)
        labels.add(
            {
                "target_type": "run",
                "target_id": "context_pack:cognitive_misses",
            },
            "missed",
            f"cognitive_miss:{miss_key}",
            0.90,
        )
        if any(marker in miss_key for marker in ("recall", "memory", "context")):
            labels.add(_section_ref("selected_memories"), "missed", f"cognitive_miss:{miss_key}", 0.86)
        if any(marker in miss_key for marker in ("skill", "plan", "planning")):
            labels.add(_section_ref("selected_skills"), "missed", f"cognitive_miss:{miss_key}", 0.86)

    if not sections and (tool_names or cognitive_misses):
        labels.add(
            {"target_type": "run", "target_id": "context_pack:missing"},
            "missed",
            "context_pack_missing_but_context_requested_later",
            0.78,
        )


def _selected_skill_use_reasons(
    selected_skill_ref: Mapping[str, Any],
    *,
    trajectory: Mapping[str, Any],
    tool_calls: list[Mapping[str, Any]],
    plan_texts: list[str],
    brain_skills_used: bool,
) -> list[str]:
    reasons: list[str] = []
    skill_name = (_text(selected_skill_ref.get("name")) or "").lower()
    effective_digest = (_text(selected_skill_ref.get("effective_digest")) or "").lower()
    if skill_name and _worker_skill_names(trajectory).intersection({skill_name}):
        reasons.append("selected_skill_used_by_worker")
    if skill_name and any(skill_name in text.lower() for text in plan_texts if text):
        reasons.append("selected_skill_referenced_by_plan_or_tool_args")
    if effective_digest and any(effective_digest in text.lower() for text in plan_texts if text):
        reasons.append("selected_skill_digest_referenced_by_plan_or_tool_args")
    if brain_skills_used:
        reasons.append("brain_skills_used")
    return _dedupe_text(reasons)


def _worker_skill_names(trajectory: Mapping[str, Any]) -> set[str]:
    evidence = _mapping(trajectory.get("worker_evidence"))
    candidates: list[Any] = []
    candidates.extend(evidence.get("workers") or [])
    candidates.extend(evidence.get("results") or [])
    candidates.extend(trajectory.get("worker_assignments") or [])
    names: set[str] = set()
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        for key in ("skill", "skill_name"):
            text = _text(item.get(key))
            if text:
                names.add(text.lower())
    return names


def _memory_reference_match(
    content: str | None,
    *,
    final_strings: list[str],
    tool_arg_strings: list[str],
) -> dict[str, Any] | None:
    tokens = _distinctive_tokens(content)
    if not tokens:
        return None
    final_text = " ".join(final_strings).lower()
    args_text = " ".join(tool_arg_strings).lower()
    final_hits = tokens & set(_WORD_RE.findall(final_text))
    arg_hits = tokens & set(_WORD_RE.findall(args_text))
    phrase_hit_final = _phrase_hit(content, final_text)
    phrase_hit_args = _phrase_hit(content, args_text)
    matched_final = bool(final_hits) or phrase_hit_final
    matched_args = bool(arg_hits) or phrase_hit_args
    if not matched_final and not matched_args:
        return None
    hit_count = len(final_hits | arg_hits)
    if phrase_hit_final or phrase_hit_args:
        return {
            "reason": "memory_referenced_in_final_output" if phrase_hit_final else "memory_referenced_in_tool_args",
            "confidence": 0.86,
            "matched_final_output": matched_final,
            "matched_tool_args": matched_args,
            "match_strategy": "phrase",
        }
    if hit_count >= 2:
        return {
            "reason": "memory_referenced_in_final_output" if final_hits else "memory_referenced_in_tool_args",
            "confidence": 0.74,
            "matched_final_output": matched_final,
            "matched_tool_args": matched_args,
            "match_strategy": "distinctive_token_overlap",
        }
    return None


def _asset_requested(asset_ref: Mapping[str, Any], tool_calls: list[Mapping[str, Any]]) -> bool:
    skill_name = (_text(asset_ref.get("skill_name")) or "").lower()
    path = (_text(asset_ref.get("path")) or "").lower()
    for call in tool_calls:
        if _text(call.get("tool_name")) != "skill_asset":
            continue
        args = _tool_args(call)
        if path and path == (_text(args.get("path")) or "").lower():
            if not skill_name or skill_name == (_text(args.get("name")) or "").lower():
                return True
    return False


def _tool_arg_strings(tool_calls: Iterable[Mapping[str, Any]]) -> list[str]:
    strings: list[str] = []
    for call in tool_calls:
        if isinstance(call.get("args"), Mapping):
            strings.extend(_collect_strings(call.get("args")))
        if isinstance(call.get("input"), Mapping):
            strings.extend(_collect_strings(call.get("input")))
        snippet = _text(call.get("args_snippet"))
        if snippet:
            strings.append(snippet)
    return strings


def _tool_result_strings(tool_calls: Iterable[Mapping[str, Any]]) -> list[str]:
    strings: list[str] = []
    for call in tool_calls:
        snippet = _text(call.get("result_snippet"))
        if snippet:
            strings.append(snippet)
    return strings


def _tool_args(call: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(call.get("args"), Mapping):
        return dict(call["args"])
    if isinstance(call.get("input"), Mapping):
        return dict(call["input"])
    snippet = _text(call.get("args_snippet"))
    if not snippet:
        return {}
    try:
        parsed = json.loads(snippet)
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _collect_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        strings: list[str] = []
        for item in value.values():
            strings.extend(_collect_strings(item))
        return strings
    if isinstance(value, (list, tuple)):
        strings: list[str] = []
        for item in value:
            strings.extend(_collect_strings(item))
        return strings
    if isinstance(value, (int, float, bool)):
        return [str(value)]
    return []


def _distinctive_tokens(value: Any) -> set[str]:
    text = _text(value)
    if not text:
        return set()
    return {
        token.lower()
        for token in _WORD_RE.findall(text)
        if len(token) >= 5 and token.lower() not in _STOPWORDS
    }


def _phrase_hit(content: Any, corpus: str) -> bool:
    text = _text(content)
    if not text or not corpus:
        return False
    words = [token for token in _WORD_RE.findall(text.lower()) if token not in _STOPWORDS]
    if len(words) < 3:
        return False
    for index in range(0, max(1, len(words) - 2)):
        phrase = " ".join(words[index:index + 3])
        if len(phrase) >= 18 and phrase in corpus:
            return True
    return False


def _section_ref(name: str) -> dict[str, Any]:
    return {
        "target_type": "section",
        "target_id": f"section:{name}",
        "section": name,
    }


def _memory_label_ref(ref: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "target_type": "memory",
        "target_id": _text(ref.get("item_id")) or _text(ref.get("item_digest")),
        "section": "selected_memories",
        "item_id": _text(ref.get("item_id")),
        "item_digest": _text(ref.get("item_digest")),
    }


def _skill_label_ref(ref: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "target_type": "skill",
        "target_id": _text(ref.get("item_id")) or _text(ref.get("item_digest")),
        "section": "selected_skills",
        "item_id": _text(ref.get("item_id")),
        "item_digest": _text(ref.get("item_digest")),
    }


def _skill_asset_label_ref(ref: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "target_type": "skill_asset",
        "target_id": _text(ref.get("item_id")) or _text(ref.get("item_digest")),
        "section": "selected_skills",
        "item_id": _text(ref.get("item_id")),
        "item_digest": _text(ref.get("item_digest")),
    }


def _budget_evidence(budget: Mapping[str, Any]) -> dict[str, Any]:
    return _drop_none({
        "estimated_tokens": _int_or_none(budget.get("estimated_tokens")),
        "budget_tokens": _int_or_none(budget.get("budget_tokens")),
        "remaining_tokens": _int_or_none(budget.get("remaining_tokens")),
    })


def _iter_asset_like_values(skill: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    for key in ("asset_refs", "assets", "loaded_assets"):
        value = skill.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    yield item


def _dedupe_refs(refs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for ref in refs:
        key = _text(ref.get("item_digest")) or _text(ref.get("item_id"))
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(dict(ref))
    return result


def _dedupe_text(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _normalized_miss_key(value: str) -> str:
    text = re.sub(r"[^a-z0-9_]+", "_", str(value or "").lower()).strip("_")
    return text[:80] or "unknown"


def _jsonable(value: Any) -> Any:
    if value.__class__.__module__ == "unittest.mock":
        return None
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _mapping(value: Any) -> dict[str, Any]:
    return _shared_as_mapping(value)


def _text(value: Any) -> str | None:
    return _shared_optional_text(value)


def _text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _int_or_none(value: Any) -> int | None:
    return _shared_int_or_none(value)


def _confidence(value: float) -> float:
    return round(max(0.0, min(1.0, float(value))), 2)


def _drop_none(payload: Mapping[str, Any]) -> dict[str, Any]:
    return _shared_drop_none(payload)


def _stable_digest(payload: Any) -> str:
    raw = json.dumps(_jsonable(payload), sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


__all__ = [
    "CONTEXT_USEFULNESS_LABELS",
    "CONTEXT_USEFULNESS_SCHEMA_VERSION",
    "build_context_usefulness_payload",
    "memory_item_reference",
    "skill_asset_reference",
    "skill_asset_references_from_skill",
    "skill_item_reference",
    "stable_context_item_digest",
    "stable_text_digest",
]

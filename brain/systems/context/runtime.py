"""ContextRuntime wraps durable context packs for model-call rendering."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from brain.systems.context.schema import validate_context_pack as validate_typed_context_pack
from brain.systems.context.sections import (
    COORDINATOR_SECTION_ORDER,
    SECTION_ORDER,
    WORKER_SECTION_ORDER,
    filter_context_pack_sections,
    validate_context_pack,
)

DEFAULT_SECTION_BUDGETS = {
    "thread_summary": 2500,
    "handoffs": 1100,
    "user_team_facts": 1800,
    "selected_memories": 900,
    "selected_skills": 1200,
    "policy_constraints": 2600,
    "approvals": 350,
    "budget": 350,
    "output_contract": 650,
    "tool_permissions": 10000,
    "uncertainty": 500,
}


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return str(value)


def _estimate_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(_jsonable(value), sort_keys=True, default=str)
    return 0 if not text else max(1, (len(str(text)) + 3) // 4)


def _digest_payload(payload: dict[str, Any], *, length: int = 24) -> str:
    raw = json.dumps(_jsonable(payload), sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def _section(name: str, title: str, content: Any, *, source: str) -> dict[str, Any]:
    content = _jsonable(content)
    estimate = _estimate_tokens(content)
    budget = int(DEFAULT_SECTION_BUDGETS.get(name, max(estimate, 1)))
    return {
        "name": name,
        "title": title,
        "source": source,
        "included": bool(content),
        "content": content,
        "token_budget": {
            "estimated_tokens": estimate,
            "budget_tokens": budget,
            "remaining_tokens": budget - estimate,
            "over_budget": estimate > budget,
        },
        "notes": [],
    }


def compile_context_pack(**kwargs: Any) -> dict[str, Any]:
    source_sections = kwargs.get("sections") if isinstance(kwargs.get("sections"), dict) else {}
    task = str(kwargs.get("task") or kwargs.get("message") or "").strip()
    compiled_at = kwargs.get("compiled_at") or datetime.now(timezone.utc).isoformat()
    sections = {
        "thread_summary": _section(
            "thread_summary",
            "Thread Summary",
            {
                "task": task,
                "thread_context": kwargs.get("thread_context"),
                "working_memory": kwargs.get("working_memory"),
            },
            source="ContextRuntime.compile_run_pack",
        ),
        "handoffs": _section(
            "handoffs",
            "Handoffs",
            kwargs.get("handoffs") or kwargs.get("handoff") or [],
            source="ContextRuntime.compile_run_pack",
        ),
        "user_team_facts": _section(
            "user_team_facts",
            "User And Team Facts",
            {
                "identity": kwargs.get("identity"),
                "user_context": kwargs.get("user_context") or {},
            },
            source="ContextRuntime.compile_run_pack",
        ),
        "selected_memories": _section(
            "selected_memories",
            "Selected Memories",
            kwargs.get("memories") or [],
            source="ContextRuntime.compile_run_pack",
        ),
        "selected_skills": _section(
            "selected_skills",
            "Selected Skills",
            {
                "selected_skill": kwargs.get("selected_skill"),
                "skill": kwargs.get("skill") or {},
            },
            source="ContextRuntime.compile_run_pack",
        ),
        "policy_constraints": _section(
            "policy_constraints",
            "Policy Constraints",
            {
                "guardrails": kwargs.get("guardrails") or [],
                "coordinator_instructions": kwargs.get("coordinator_instructions"),
            },
            source="ContextRuntime.compile_run_pack",
        ),
        "approvals": _section(
            "approvals",
            "Approvals",
            kwargs.get("approvals") or {},
            source="ContextRuntime.compile_run_pack",
        ),
        "budget": _section(
            "budget",
            "Budget",
            {
                "model": kwargs.get("model"),
                "thinking": kwargs.get("thinking"),
                "run_id": kwargs.get("run_id"),
            },
            source="ContextRuntime.compile_run_pack",
        ),
        "output_contract": _section(
            "output_contract",
            "Output Contract",
            kwargs.get("output_contract") or {},
            source="ContextRuntime.compile_run_pack",
        ),
        "tool_permissions": _section(
            "tool_permissions",
            "Tool Permissions",
            kwargs.get("tool_permissions") or {},
            source="ContextRuntime.compile_run_pack",
        ),
        "uncertainty": _section(
            "uncertainty",
            "Uncertainty",
            kwargs.get("uncertainty") or {},
            source="ContextRuntime.compile_run_pack",
        ),
    }
    for name, section in source_sections.items():
        if isinstance(section, dict):
            content = section.get("content", section)
            sections[str(name)] = _section(
                str(name),
                str(section.get("title") or str(name).replace("_", " ").title()),
                content,
                source=str(section.get("source") or "caller"),
            )

    render_order = [name for name in SECTION_ORDER if name in sections]
    section_token_budget = {
        name: dict(sections[name]["token_budget"])
        for name in render_order
    }
    total_estimated_tokens = sum(item["estimated_tokens"] for item in section_token_budget.values())
    pack = {
        "schema_version": 1,
        "compiler_version": "agent-run-context-v1",
        "compiled_at": str(compiled_at) if compiled_at is not None else None,
        "run_id": kwargs.get("run_id"),
        "idea_id": kwargs.get("idea_id"),
        "task": task,
        "render_order": render_order,
        "sections": {name: sections[name] for name in render_order},
        "section_token_budget": section_token_budget,
        "total_estimated_tokens": total_estimated_tokens,
    }
    pack["digest"] = _digest_payload({
        "schema_version": pack["schema_version"],
        "compiler_version": pack["compiler_version"],
        "run_id": pack["run_id"],
        "idea_id": pack["idea_id"],
        "task": task,
        "sections": pack["sections"],
    })
    return validate_typed_context_pack(pack)


def render_context_pack_for_prompt(context_pack: dict[str, Any]) -> str:
    sections = context_pack.get("sections") if isinstance(context_pack, dict) else {}
    lines: list[str] = ["## Context Pack"]
    if isinstance(context_pack, dict) and context_pack.get("digest"):
        lines.append(f"Digest: {context_pack['digest']}")
    if isinstance(sections, dict):
        for name in context_pack.get("render_order") or sections.keys():
            section = sections.get(name)
            if not isinstance(section, dict):
                continue
            title = section.get("title") or str(name).replace("_", " ").title()
            content = section.get("content", section)
            rendered = content if isinstance(content, str) else json.dumps(_jsonable(content), sort_keys=True, indent=2, default=str)
            lines.append(f"### {title}\n{rendered}")
    return "\n\n".join(lines)


@dataclass(frozen=True)
class ContextRender:
    """Rendered context plus audit metadata."""

    prompt: str
    context_pack: dict[str, Any] | None
    role: str
    context_pack_digest: str | None = None
    source_context_pack_digest: str | None = None
    rendered_sections: tuple[str, ...] = ()
    omitted_sections: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "context_pack_digest": self.context_pack_digest,
            "source_context_pack_digest": self.source_context_pack_digest,
            "rendered_sections": list(self.rendered_sections),
            "omitted_sections": list(self.omitted_sections),
            **dict(self.metadata or {}),
        }


class ContextRuntime:
    """Runtime facade for compiling and rendering model context."""

    compiler_version = "context-runtime-v1"

    def compile_run_pack(self, **kwargs: Any) -> dict[str, Any]:
        pack = compile_context_pack(**kwargs)
        pack.setdefault("runtime", {})
        if isinstance(pack["runtime"], dict):
            pack["runtime"].setdefault("compiler", self.compiler_version)
            pack["runtime"].setdefault("source", "ContextRuntime.compile_run_pack")
        return validate_context_pack(pack)

    def render_prompt(
        self,
        context_pack: dict[str, Any] | None,
        *,
        role: str = "coordinator",
        section_order: tuple[str, ...] | None = None,
    ) -> ContextRender:
        if not context_pack:
            return ContextRender(prompt="", context_pack=None, role=role)

        if role == "coordinator":
            if section_order is None or tuple(section_order) == COORDINATOR_SECTION_ORDER:
                render_pack = validate_context_pack(context_pack)
            else:
                render_pack = filter_context_pack_sections(
                    context_pack,
                    include_sections=section_order,
                    role=role,
                )
        elif role == "worker":
            render_pack = filter_context_pack_sections(
                context_pack,
                include_sections=section_order or WORKER_SECTION_ORDER,
                role=role,
            )
        else:
            render_pack = filter_context_pack_sections(
                context_pack,
                include_sections=section_order or tuple(context_pack.get("render_order") or ()),
                role=role,
            )

        prompt = render_context_pack_for_prompt(render_pack or context_pack)
        rendered_sections = tuple((render_pack or context_pack).get("render_order") or ())
        omitted_sections = tuple((render_pack or {}).get("omitted_sections") or ())
        return ContextRender(
            prompt=prompt,
            context_pack=render_pack or context_pack,
            role=role,
            context_pack_digest=(render_pack or context_pack).get("digest"),
            source_context_pack_digest=(render_pack or context_pack).get("source_digest")
            or context_pack.get("digest"),
            rendered_sections=rendered_sections,
            omitted_sections=omitted_sections,
            metadata={
                "compiler": self.compiler_version,
                "render_source": "ContextRuntime.render_prompt",
            },
        )

    def render_worker_context(
        self,
        context_pack: dict[str, Any] | None,
        *,
        worker_id: str | None = None,
        node_id: str | None = None,
        skill_name: str | None = None,
    ) -> ContextRender:
        render = self.render_prompt(context_pack, role="worker")
        if not render.context_pack:
            return render
        metadata = {
            **render.metadata,
            "worker_id": worker_id,
            "node_id": node_id,
            "skill_name": skill_name,
        }
        return ContextRender(
            prompt=render.prompt,
            context_pack=render.context_pack,
            role=render.role,
            context_pack_digest=render.context_pack_digest,
            source_context_pack_digest=render.source_context_pack_digest,
            rendered_sections=render.rendered_sections,
            omitted_sections=render.omitted_sections,
            metadata=metadata,
        )

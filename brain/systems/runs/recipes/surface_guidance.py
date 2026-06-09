"""Shared response-surface guidance for agent recipes."""

from __future__ import annotations

from typing import Any, Mapping


def response_surface_guidance(
    *,
    target_ref: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None,
) -> str:
    """Build generic guidance for prompt-time triage and delegated work."""

    target = dict(target_ref or {})
    meta = dict(metadata or {})
    originating_surface = _first_text(
        (target, meta),
        ("originating_surface", "triggering_surface", "source_surface", "surface"),
    )
    response_tool = _first_text((target, meta), ("required_response_tool",))
    final_surface = _first_text((target, meta), ("final_answer_target_surface",))

    lines = ["## Response Surface and Delegation"]
    if originating_surface:
        lines.append(f"- Originating surface: {originating_surface}.")
    if response_tool:
        lines.append(f"- User-visible response tool for that surface: {response_tool}.")
    if final_surface:
        lines.append(f"- Final answer target surface: {final_surface}.")
    lines.extend(
        [
            "- Triage before deep work: decide whether the request can be answered directly or needs durable/parallel follow-up.",
            "- For simple requests, answer naturally on the originating surface when a response tool is available; otherwise finish with a concise final answer.",
            "- For long, uncertain, or parallelizable work, first make the delegation durable with spawn_worker or a Cortex Thread/run via manage_idea, then promptly send a model-authored update on the originating surface when a response tool is available.",
            "- Do not use canned acknowledgements; describe the actual decision, work started, child runs, or thread created.",
            "- Do not wait for long-running work just to produce the first visible user update after durable dispatch.",
            "- Use headless=true only for internal diagnostics or blocker reports that should not create visible user-facing content.",
            "- When sharing Cortex Thread links, use a thread_url returned by a tool. Never construct a Thread URL from a Slack id, run id, or other synthetic thread key.",
            "- When a visible delegated run completes, make its final answer suitable as a concise update back to the originating surface.",
        ]
    )
    return "\n".join(lines)


def _first_text(containers: tuple[Mapping[str, Any], ...], keys: tuple[str, ...]) -> str:
    for container in containers:
        if not isinstance(container, Mapping):
            continue
        for key in keys:
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


__all__ = ["response_surface_guidance"]

"""Worker orchestration tool schemas."""

from __future__ import annotations

from brain.platform.effort import EFFORT_TIERS


WORKER_SPAWN_TOOLS = [
    {
        "name": "spawn_worker",
        "description": (
            "Queue a scoped worker AgentRun and return immediately. Use this when an independent "
            "slice can progress in parallel while the current run continues, or when Illo should "
            "file/report an internal bug or blocker in the background. Set headless=true for "
            "background reporting or investigation that should not create visible thread content; "
            "leave headless=false when the delegated run should be able to report visible progress "
            "or a final update back through the inherited originating surface. Two lanes. "
            "Reasoning/judgment/review/long-context/chatty tool loops: gpt-5.6-sol, routed by effort "
            "(xhigh judgment, high standard). Bulk/mechanical/single-shot/small-context execution: "
            "openai/gpt-5.6-luna at xhigh. Luna caveats: quality collapses above ~200K context; "
            "xhigh pays a long first-token pause per turn — never use Luna xhigh for many-short-turn "
            "loops. Reserve non-OpenAI models for a cross-provider verifier. Free local lane: "
            "`ollama/qwen3.6-27b` — zero cost, unlimited volume, ≤64k context, quality well below "
            "Luna; use for heartbeat-class, high-volume, low-stakes single-shot work; never for "
            "judgment or anything user-facing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "objective": {
                    "type": "string",
                    "description": "Specific outcome the worker owns. Keep it narrow and independently completable.",
                },
                "role": {
                    "type": "string",
                    "default": "worker",
                    "description": "Short worker role label, such as investigate, report_bug, verify, or implement.",
                },
                "message": {
                    "type": "string",
                    "description": "Optional full instruction to run. Defaults to the objective.",
                },
                "effort": {
                    "type": "string",
                    "enum": list(EFFORT_TIERS),
                    "description": "Optional canonical effort override for this child. Inherits the parent's effective effort when omitted.",
                },
                "model": {
                    "type": "string",
                    "description": "Optional lane or cross-provider override: a provider-prefixed catalog id or a bare provider name selecting that provider's default model.",
                },
                "headless": {
                    "type": "boolean",
                    "default": False,
                    "description": "When true, hide the worker from visible thread history and block visible reply tools.",
                },
                "join_parent": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "When true, queue one continuation on the parent thread after the parent "
                        "and all of its spawn_worker children reach terminal state. Has no effect "
                        "on existing fire-and-forget calls when omitted."
                    ),
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Optional stable key to avoid duplicate workers for the same background task.",
                },
                "allowed_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional file patterns the worker may mutate/read within its scoped assignment.",
                },
                "forbidden_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional file patterns the worker must not touch.",
                },
                "allowed_resources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional resource identifiers the worker may use.",
                },
                "forbidden_resources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional resource identifiers the worker must not use.",
                },
                "expected_artifacts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional artifact types the worker should produce, such as worker_result or pr_link.",
                },
                "evidence_requirements": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Optional evidence requirements using WorkerAssignment evidence requirement shape.",
                },
                "acceptance_criteria": {
                    "type": "object",
                    "description": "Optional WorkerAssignment acceptance criteria.",
                },
                "risk_level": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "default": "medium",
                    "description": "Risk level for the scoped worker assignment.",
                },
                "tool_policy": {
                    "type": "object",
                    "description": "Optional additional tool policy for the worker. headless=true adds visible-tool blocks.",
                },
                "metadata": {
                    "type": "object",
                    "description": "Optional extra provenance metadata to attach to the worker run.",
                },
            },
            "required": ["objective"],
        },
    },
]

"""Worker orchestration tool schemas."""

from __future__ import annotations


WORKER_SPAWN_TOOLS = [
    {
        "name": "spawn_worker",
        "description": (
            "Queue a scoped worker AgentRun and return immediately. Use this when an independent "
            "slice can progress in parallel while the current run continues, or when Illo should "
            "file/report an internal bug or blocker in the background. Set headless=true for "
            "background reporting or investigation that should not create visible thread content; "
            "leave headless=false when the delegated run should be able to report visible progress "
            "or a final update back through the inherited originating surface."
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
                "headless": {
                    "type": "boolean",
                    "default": False,
                    "description": "When true, hide the worker from visible thread history and block visible reply tools.",
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

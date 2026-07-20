"""Registry for agent tool schemas, permissions, and audit metadata."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import re
from typing import Any, Literal

from brain.systems.runs.tool_definitions import (
    BRAIN_TOOLS,
    BROWSER_TOOL,
    CHAT_TOOLS,
    CORTEX_REPLY_TOOL,
    CORTEX_VISUAL_REPLY_TOOL,
    DEPLOYMENT_TOOLS,
    CORTEX_IDEA_TOOLS,
    DOMAIN_TOOLS,
    EXEC_TOOLS,
    GITHUB_TOOLS,
    INBOUND_TOOLS,
    LAUNCH_HANDOFF_TOOLS,
    LIFECYCLE_TOOLS,
    MY_ACTIVITY_TOOL,
    PROJECT_TOOLS,
    SOUL_TOOLS,
    SESSION_TOOLS,
    WORKSPACE_OVERVIEW_SPARSE_GUIDANCE,
    WORKSPACE_APP_TOOLS,
    WORKSPACE_TOOL_TOOLS,
    WORKER_SPAWN_TOOLS,
)
from brain.systems.runs.tool_catalog.metadata import (
    ActionPolicyResult,
    ToolAvailability,
    ToolParallelSafety,
    ToolPermission,
    ToolRegistration,
    ToolReversibility,
    ToolRiskClass,
    ToolSideEffectClass,
)

_DEFAULT_OUTPUT_BUDGET_CHARS = 10_000

_BROWSER_TOOLS = [
    BROWSER_TOOL,
]

_STATIC_METADATA: dict[str, dict[str, Any]] = {
    "brain_recall": {
        "permission": "read_memory",
        "output_budget_chars": 8_000,
        "context_route": {
            "description": "Search long-term semantic memories. Use for remembered lessons, facts, patterns, and episodes; use read_workspace_* tools for source-of-truth workspace records and current team activity.",
            "domains": ["memory", "remembered context", "prior lessons", "broad memory recap"],
            "scopes": ["narrow", "broad"],
            "empty_result_policy": "fallback_full_pipeline_when_broad",
        },
    },
    "brain_guardrails": {"permission": "read_memory", "output_budget_chars": 6_000},
    "brain_skills": {"permission": "read_skills", "output_budget_chars": 6_000},
    "skill_view": {"permission": "read_skills", "output_budget_chars": 12_000},
    "skill_asset": {"permission": "read_skills", "output_budget_chars": 12_000},
    "manage_skill": {
        "permission": "write_skill",
        "risk_class": "high",
        "side_effect_class": "skill_write",
        "reversibility": "variable",
        "action_manifest": True,
        "expected_effect": "read or mutate installed Illo skills and skill bundle assets",
        "output_budget_chars": 14_000,
    },
    "manage_deployment": {
        "permission": "manage_runtime",
        "risk_class": "high",
        "side_effect_class": "deployment_management",
        "reversibility": "variable",
        "action_manifest": True,
        "expected_effect": "start or inspect the Illospace self-update deployment flow",
        "output_budget_chars": 8_000,
    },
    "manage_runtime_services": {
        "permission": "manage_runtime",
        "risk_class": "high",
        "side_effect_class": "deployment_management",
        "reversibility": "variable",
        "action_manifest": True,
        "expected_effect": "list, inspect, or restart known Illospace runtime services",
        "output_budget_chars": 8_000,
    },
    "manage_workspace_tools": {
        "permission": "manage_runtime",
        "risk_class": "high",
        "side_effect_class": "workspace_tool_management",
        "reversibility": "variable",
        "action_manifest": True,
        "expected_effect": "inspect or mutate persisted workspace tool bundle installations",
        "output_budget_chars": 12_000,
    },
    "brain_encode": {
        "permission": "write_memory",
        "risk_class": "low",
        "side_effect_class": "append_only",
        "reversibility": "append_only",
        "action_manifest": True,
        "expected_effect": "ingest source-backed reconstructive memory",
    },
    "memory_ingest_source": {
        "permission": "write_memory",
        "risk_class": "low",
        "side_effect_class": "append_only",
        "reversibility": "append_only",
        "action_manifest": True,
        "expected_effect": "ingest source-backed reconstructive memory",
    },
    "memory_reconstruct": {
        "permission": "read_memory",
        "risk_class": "low",
        "side_effect_class": "read_only",
        "reversibility": "none",
        "action_manifest": False,
        "expected_effect": "reconstruct source-backed memory evidence",
    },
    "memory_link": {
        "permission": "write_memory",
        "risk_class": "low",
        "side_effect_class": "memory_curation",
        "reversibility": "variable",
        "action_manifest": True,
        "expected_effect": "create or reinforce a deliberate memory relationship",
        "output_budget_chars": 4_000,
    },
    "memory_supersede": {
        "permission": "write_memory",
        "risk_class": "medium",
        "side_effect_class": "memory_curation",
        "reversibility": "variable",
        "action_manifest": True,
        "expected_effect": "mark stale memory superseded and link its replacement",
        "output_budget_chars": 4_000,
    },
    "memory_archive": {
        "permission": "write_memory",
        "risk_class": "medium",
        "side_effect_class": "memory_curation",
        "reversibility": "reversible_by_archive",
        "action_manifest": True,
        "expected_effect": "archive obsolete or redundant memory nodes",
        "output_budget_chars": 4_000,
    },
    "brain_vault": {
        "permission": "read_secret",
        "risk_class": "high",
        "side_effect_class": "read_only",
        "reversibility": "none",
        "action_manifest": True,
        "expected_effect": "request or consume a one-use vault grant for an exact secret key",
    },
    "vault_inventory": {
        "permission": "read_secret",
        "risk_class": "low",
        "side_effect_class": "read_only",
        "reversibility": "read_mostly",
        "action_manifest": True,
        "expected_effect": "list safe vault secret metadata without values",
    },
    "vault_secret_prompt": {
        "permission": "write_session",
        "risk_class": "medium",
        "side_effect_class": "run_annotation",
        "reversibility": "reversible",
        "action_manifest": True,
        "expected_effect": "open a guided vault secret prompt in the current Cortex thread",
    },
    "runtime_settings": {
        "permission": "read_runtime",
        "context_route": {
            "description": "Inspect active provider, credentials/auth status, runtime model routing, and provider/model mappings for the current user/workspace.",
            "domains": ["runtime settings", "provider", "auth status", "model routing", "credentials"],
            "scopes": ["narrow"],
        },
    },
    "read_self_context": {
        "permission": "read_runtime",
        "output_budget_chars": 12_000,
        "evidence_emitter": True,
        "context_route": {
            "description": "Read verified Illo/Illospace identity, source-root, open-source repository, git, install, and source-inspection facts for the current run.",
            "domains": ["self context", "identity", "source code", "installation", "open source repo", "where Illo runs"],
            "scopes": ["narrow"],
        },
    },
    "read_capabilities": {
        "permission": "read_runtime",
        "output_budget_chars": 16_000,
        "evidence_emitter": True,
        "context_route": {
            "description": "Read machine-readable capability manifests: what Illo can inspect, do, or guide; status tools; setup modes; and setup guide references.",
            "domains": ["capabilities", "integrations", "connectors", "plugins", "tools", "setup", "what Illo can do"],
            "scopes": ["narrow", "broad"],
        },
    },
    "transcribe_audio_attachment": {
        "permission": "network_read",
        "risk_class": "low",
        "side_effect_class": "read_only_external",
        "reversibility": "read_only_external",
        "output_budget_chars": 24_000,
        "evidence_emitter": True,
        "action_manifest": True,
        "expected_effect": "stream an uploaded audio attachment to the configured voice transcription provider and return the transcript",
    },
    "manage_soul": {
        "permission": "manage_soul",
        "risk_class": "medium",
        "side_effect_class": "soul_management",
        "reversibility": "reversible",
        "action_manifest": True,
        "expected_effect": "read or mutate Illo's private SOUL.md personality file",
        "output_budget_chars": 8_000,
    },
    "read_thread_messages": {
        "permission": "read_session",
        "parallel_safety": "safe",
        "output_budget_chars": 12_000,
        "context_route": {
            "description": "Read raw stored messages from this agent run's persistent LLM session when the durable handoff summary lacks an exact older detail. This is not the user's Cortex workspace thread.",
            "domains": ["agent session transcript", "thread transcript", "raw LLM conversation", "prior messages", "handoff provenance"],
            "scopes": ["narrow"],
        },
    },
    "query_workspace_data": {
        "permission": "read_activity",
        "output_budget_chars": 18_000,
        "evidence_emitter": True,
        "context_route": {
            "description": (
                "Low-level read-only query over Illospace DB-backed workspace truth: team members, "
                "Cortex ideas/thread messages, runs, tool calls, Project Contexts, Domains/records, "
                "workspace apps/state, and Cycles. Prefer specific read_workspace_* tools for normal answers."
            ),
            "domains": [
                "workspace records",
                "team activity",
                "teammate activity",
                "runs",
                "thread messages",
                "ideas",
                "tool calls",
                "team members",
                "project contexts",
                "domains",
                "domain records",
                "workspace apps",
                "artifacts",
                "app state",
                "cycles",
            ],
            "scopes": ["narrow"],
        },
    },
    "read_workspace_overview": {
        "permission": "read_activity",
        "output_budget_chars": 22_000,
        "evidence_emitter": True,
        "context_route": {
            "description": (
                "Read a curated overview of the workspace: team members, active thoughts, recent runs/messages, "
                "Project Context, Domains, apps, Cycles, and setup gaps. Use first for onboarding, setup, and broad "
                "'what can you see?' questions. "
                + WORKSPACE_OVERVIEW_SPARSE_GUIDANCE
            ),
            "domains": ["workspace overview", "workspace setup", "onboarding", "available context", "workspace awareness"],
            "scopes": ["broad"],
        },
    },
    "read_team_activity": {
        "permission": "read_activity",
        "output_budget_chars": 18_000,
        "evidence_emitter": True,
        "context_route": {
            "description": "Read recent human and Illo activity across Cortex messages, ideas, runs, tool calls, Domain events, Project Context attachments, apps, and Cycle runs.",
            "domains": ["team activity", "teammate activity", "workspace activity", "recent activity", "what changed", "what Illo did"],
            "scopes": ["narrow", "broad"],
        },
    },
    "read_project_contexts": {
        "permission": "read_activity",
        "output_budget_chars": 18_000,
        "evidence_emitter": True,
        "context_route": {
            "description": "Read Project Context profiles and thread attachments: resources, repos, files, docs, validation, permission scope, and attached thoughts.",
            "domains": ["project context", "projects", "connected repos", "connected docs", "workspace resources"],
            "scopes": ["narrow", "broad"],
        },
    },
    "read_team_members": {
        "permission": "read_activity",
        "output_budget_chars": 14_000,
        "evidence_emitter": True,
        "context_route": {
            "description": "Read the workspace roster and optional nearby activity for people. Use for who is here, roles, ownership, named teammate activity, and teammate coordination that needs exact user ids.",
            "domains": ["team members", "workspace roster", "people", "roles", "ownership", "who is working on what"],
            "scopes": ["narrow", "broad"],
        },
    },
    "read_workspace_records": {
        "permission": "read_activity",
        "output_budget_chars": 18_000,
        "evidence_emitter": True,
        "context_route": {
            "description": "Read user-created structured workspace data: Domain schemas, typed records, and Domain audit events.",
            "domains": ["workspace records", "domains", "domain records", "structured data", "trackers", "team database"],
            "scopes": ["narrow", "broad"],
        },
    },
    "read_cycles": {
        "permission": "read_activity",
        "output_budget_chars": 14_000,
        "evidence_emitter": True,
        "context_route": {
            "description": "Read workspace Cycles and Cycle runs: recurring prompts, schedules, enabled state, last/next run, linked thoughts, and status.",
            "domains": ["cycles", "recurring work", "scheduled work", "check-ins", "automations", "reports"],
            "scopes": ["narrow", "broad"],
        },
    },
    "read_workspace_apps": {
        "permission": "read_activity",
        "output_budget_chars": 18_000,
        "evidence_emitter": True,
        "context_route": {
            "description": "Read generated workspace apps, dashboards, metadata, and app-local state.",
            "domains": ["workspace apps", "dashboards", "generated apps", "app state", "workspace UI"],
            "scopes": ["narrow", "broad"],
        },
    },
    "read_github_source": {
        "permission": "read_workspace",
        "risk_class": "low",
        "side_effect_class": "read_only",
        "reversibility": "none",
        "expected_effect": (
            "read bounded GitHub repository metadata, work items, CI checks, or pinned source evidence"
        ),
        "output_budget_chars": 18_000,
        "evidence_emitter": True,
    },
    "check_fix_deploy_state": {
        "permission": "read_workspace",
        "risk_class": "low",
        "side_effect_class": "read_only",
        "reversibility": "none",
        "expected_effect": "read GitHub ancestry to classify a fix's deploy state",
        "output_budget_chars": 8_000,
        "evidence_emitter": True,
    },
    "create_github_issue": {
        "permission": "write_workspace",
        "risk_class": "high",
        "side_effect_class": "append_only",
        "reversibility": "append_only",
        "action_manifest": True,
        "expected_effect": "open a real GitHub issue in the target repository",
        "output_budget_chars": 8_000,
    },
    "update_github_issue": {
        "permission": "write_workspace",
        "risk_class": "high",
        "side_effect_class": "project_context_management",
        "reversibility": "reversible",
        "action_manifest": True,
        "expected_effect": "update fields on a real GitHub issue and read the issue back",
        "output_budget_chars": 10_000,
    },
    "add_github_issue_comment": {
        "permission": "write_workspace",
        "risk_class": "high",
        "side_effect_class": "append_only",
        "reversibility": "append_only",
        "action_manifest": True,
        "expected_effect": "append a real comment to an existing GitHub issue",
        "output_budget_chars": 8_000,
    },
    "add_github_sub_issue": {
        "permission": "write_workspace",
        "risk_class": "high",
        "side_effect_class": "project_context_management",
        "reversibility": "reversible",
        "action_manifest": True,
        "expected_effect": "link a real GitHub issue under a native parent issue",
        "output_budget_chars": 8_000,
    },
    "remove_github_sub_issue": {
        "permission": "write_workspace",
        "risk_class": "high",
        "side_effect_class": "project_context_management",
        "reversibility": "reversible",
        "action_manifest": True,
        "expected_effect": "remove a real GitHub issue from a native parent issue",
        "output_budget_chars": 8_000,
    },
    "list_github_sub_issues": {
        "permission": "read_workspace",
        "risk_class": "low",
        "side_effect_class": "read_only",
        "reversibility": "none",
        "expected_effect": "list native GitHub sub-issues or look up an issue's parent",
        "output_budget_chars": 18_000,
        "evidence_emitter": True,
    },
    "manage_cycle": {
        "permission": "manage_cycles",
        "risk_class": "high",
        "side_effect_class": "cycle_management",
        "reversibility": "reversible",
        "action_manifest": True,
        "expected_effect": "mutate a scheduled cycle",
    },
    "manage_domain": {
        "permission": "write_domain",
        "risk_class": "medium",
        "side_effect_class": "domain_management",
        "reversibility": "variable",
        "action_manifest": True,
        "expected_effect": "read or mutate org-wide domain schema and records",
        # Must carry get_record reads of doc_page operating documents. The
        # coordinator playbook (record 1155) was split into a ~31K core plus
        # on-demand mode records (~2.5K each, fetched per its On-demand Run
        # Modes section), so the core reads ~33K JSON-wrapped. 40K stays: the
        # headroom is what stops budget-chasing — future growth belongs in the
        # on-demand records, not in a bigger core. Listings are protected
        # separately by format="compact" + returned/total_matching counts; an
        # overflow here is loud (degraded-evidence note) rather than silent.
        "output_budget_chars": 40_000,
    },
    "merge_chantier": {
        "permission": "write_domain",
        "risk_class": "medium",
        "side_effect_class": "domain_management",
        "reversibility": "variable",
        "action_manifest": True,
        "expected_effect": "merge and retire a duplicate chantier record",
        "output_budget_chars": 12_000,
    },
    "manage_inbound": {
        "permission": "manage_inbound",
        "risk_class": "high",
        "side_effect_class": "inbound_configuration",
        "reversibility": "variable",
        "action_manifest": True,
        "expected_effect": "read or mutate inbound source connections, tokens, policies, and Domain Projections",
        "output_budget_chars": 18_000,
    },
    "manage_idea": {
        "permission": "write_idea",
        "risk_class": "medium",
        "side_effect_class": "idea_management",
        "reversibility": "reversible_by_archive",
        "action_manifest": True,
        "expected_effect": "read or mutate Cortex thoughts, threads, ideas, and Illo-authored teammate handoffs",
        "output_budget_chars": 14_000,
    },
    "post_chat_message": {
        "permission": "write_chat",
        "risk_class": "medium",
        "side_effect_class": "chat_message",
        "reversibility": "append_only",
        "action_manifest": True,
        "expected_effect": "post an Illo-authored message to the native team room",
        "output_budget_chars": 8_000,
    },
    "post_slack_reply": {
        "permission": "write_chat",
        "risk_class": "medium",
        "side_effect_class": "chat_message",
        "reversibility": "append_only",
        "action_manifest": True,
        "expected_effect": "post an Illo-authored reply to Slack",
        "output_budget_chars": 8_000,
    },
    "react_to_slack_message": {
        "permission": "write_chat",
        "risk_class": "low",
        "side_effect_class": "chat_message",
        "reversibility": "reversible",
        "action_manifest": True,
        "expected_effect": "add one Illo-authored emoji reaction to a Slack message",
        "output_budget_chars": 4_000,
    },
    "post_thread_discussion_reply": {
        "permission": "write_chat",
        "risk_class": "medium",
        "side_effect_class": "chat_message",
        "reversibility": "append_only",
        "action_manifest": True,
        "expected_effect": "post an Illo-authored reply to Thread Discussion",
        "output_budget_chars": 8_000,
    },
    "publish_thread_asset": {
        "permission": "write_workspace",
        "risk_class": "medium",
        "side_effect_class": "append_only",
        "reversibility": "append_only",
        "action_manifest": True,
        "expected_effect": "publish a generated local artifact as a visible Thread upload asset",
        "output_budget_chars": 8_000,
    },
    "publish_thread_artifact": {
        "permission": "write_workspace_app",
        "risk_class": "medium",
        "side_effect_class": "workspace_app_management",
        "reversibility": "reversible_by_archive",
        "action_manifest": True,
        "expected_effect": "publish a versioned interactive HTML artifact scoped to a Thread",
        "output_budget_chars": 12_000,
    },
    "post_ai_timeline_message": {
        "permission": "write_chat",
        "risk_class": "medium",
        "side_effect_class": "chat_message",
        "reversibility": "append_only",
        "action_manifest": True,
        "expected_effect": "post an Illo-authored message to the linked Thread AI Timeline",
        "output_budget_chars": 8_000,
    },
    "create_launch_handoff": {
        "permission": "write_workspace",
        "risk_class": "medium",
        "side_effect_class": "append_only",
        "reversibility": "append_only",
        "action_manifest": True,
        "expected_effect": "create a durable launch handoff link for a local coding agent",
        "output_budget_chars": 8_000,
    },
    "read_thread_discussion": {
        "permission": "read_workspace",
        "risk_class": "low",
        "side_effect_class": "read_only",
        "reversibility": "none",
        "expected_effect": "read Discussion comments attached to the current Thread",
        "output_budget_chars": 12_000,
    },
    "read_slack_conversation": {
        "permission": "read_workspace",
        "risk_class": "low",
        "side_effect_class": "read_only",
        "reversibility": "none",
        "expected_effect": "read bounded Slack context for the current Slack-triggered run",
        "output_budget_chars": 12_000,
    },
    "manage_slack": {
        "permission": "manage_inbound",
        "risk_class": "medium",
        "side_effect_class": "inbound_configuration",
        "reversibility": "variable",
        "action_manifest": True,
        "expected_effect": "inspect Slack connection health, list bot-visible or observed Slack conversations, or update Slack-to-Illospace identity mappings",
        "output_budget_chars": 12_000,
    },
    "manage_project": {
        "permission": "write_project",
        "risk_class": "medium",
        "side_effect_class": "project_context_management",
        "reversibility": "reversible_by_archive",
        "action_manifest": True,
        "expected_effect": "read or mutate durable Cortex Project Context profiles",
        "output_budget_chars": 14_000,
    },
    "manage_workspace_app": {
        "permission": "write_workspace_app",
        "risk_class": "medium",
        "side_effect_class": "workspace_app_management",
        "reversibility": "reversible_by_archive",
        "action_manifest": True,
        "expected_effect": "read or mutate a generated workspace app",
        "output_budget_chars": 18_000,
    },
    "exec_command": {
        "permission": "execute_shell",
        "risk_class": "medium",
        "side_effect_class": "shell",
        "reversibility": "variable",
        "action_manifest": True,
        "expected_effect": "execute shell command",
    },
    "read_file": {"permission": "read_workspace", "parallel_safety": "safe", "evidence_emitter": True},
    "write_file": {
        "permission": "write_workspace",
        "risk_class": "medium",
        "side_effect_class": "file_write",
        "reversibility": "reversible_with_version_control",
        "action_manifest": True,
        "expected_effect": "write file contents",
    },
    "edit_file": {
        "permission": "write_workspace",
        "risk_class": "medium",
        "side_effect_class": "file_edit",
        "reversibility": "reversible_with_version_control",
        "action_manifest": True,
        "expected_effect": "edit file contents",
    },
    "search_files": {"permission": "read_workspace", "parallel_safety": "safe", "evidence_emitter": True},
    "list_files": {"permission": "read_workspace", "parallel_safety": "safe", "evidence_emitter": True},
    "run_script": {
        "permission": "execute_python",
        "risk_class": "medium",
        "side_effect_class": "shell",
        "reversibility": "variable",
        "action_manifest": True,
        "expected_effect": "execute Python script",
    },
    "parallel_tool_batch": {
        "permission": "parallel_read",
        "side_effect_class": "read_only",
        "parallel_safety": "serial",
        "expected_effect": "execute safe read tools concurrently",
    },
    "spawn_worker": {
        "permission": "spawn_worker",
        "risk_class": "medium",
        "side_effect_class": "run_spawn",
        "reversibility": "reversible",
        "action_manifest": True,
        "expected_effect": "queue a scoped child AgentRun worker",
    },
    "web_search": {
        "permission": "network_read",
        "risk_class": "low",
        "side_effect_class": "read_only_external",
        "reversibility": "read_only_external",
        "parallel_safety": "safe",
        "action_manifest": True,
        "expected_effect": "call external web search provider",
    },
    "web_fetch": {
        "permission": "network_read",
        "risk_class": "low",
        "side_effect_class": "read_only_external",
        "reversibility": "read_only_external",
        "parallel_safety": "safe",
        "action_manifest": True,
        "expected_effect": "fetch external URL",
    },
    "semantic_search": {"permission": "read_workspace", "parallel_safety": "safe", "evidence_emitter": True},
    "file_summary": {"permission": "read_workspace", "parallel_safety": "safe", "evidence_emitter": True},
    "test_runner": {
        "permission": "execute_tests",
        "risk_class": "medium",
        "side_effect_class": "shell",
        "reversibility": "variable",
        "action_manifest": True,
        "expected_effect": "execute test command",
    },
    "project_context": {"permission": "read_workspace", "parallel_safety": "safe", "evidence_emitter": True},
    "summarize_file_for_task": {
        "permission": "read_workspace",
        "parallel_safety": "agent_safe",
        "evidence_emitter": True,
    },
    "summarize_files_for_task": {
        "permission": "read_workspace",
        "parallel_safety": "agent_safe",
        "evidence_emitter": True,
    },
    "trace_symbol": {"permission": "read_workspace", "parallel_safety": "safe", "evidence_emitter": True},
    "build_implementation_map": {"permission": "read_workspace", "parallel_safety": "safe", "evidence_emitter": True},
    "session_write": {
        "permission": "write_session",
        "side_effect_class": "scratchpad",
        "reversibility": "append_only",
        "action_manifest": True,
        "expected_effect": "write session scratchpad entry",
    },
    "session_read": {"permission": "read_session"},
    "session_append": {
        "permission": "write_session",
        "side_effect_class": "scratchpad",
        "reversibility": "append_only",
        "action_manifest": True,
        "expected_effect": "append session scratchpad entry",
    },
    "session_list": {"permission": "read_session"},
    "session_promote": {
        "permission": "promote_session",
        "side_effect_class": "scratchpad_lifecycle",
        "reversibility": "read_mostly",
        "action_manifest": True,
        "expected_effect": "promote session scratchpad entries for review",
    },
    "session_close": {
        "permission": "close_session",
        "side_effect_class": "scratchpad_lifecycle",
        "reversibility": "reversible",
        "action_manifest": True,
        "expected_effect": "close session scratchpad entries",
    },
    "cortex_reply": {
        "permission": "post_reply",
        "risk_class": "medium",
        "side_effect_class": "append_only",
        "reversibility": "append_only",
        "action_manifest": True,
        "expected_effect": "post a Cortex thread reply",
    },
    "cortex_visual_reply": {
        "permission": "post_visual_reply",
        "risk_class": "medium",
        "side_effect_class": "append_only",
        "reversibility": "append_only",
        "action_manifest": True,
        "expected_effect": "post a Cortex visual reply",
    },
    "my_activity": {
        "permission": "read_activity",
        "context_route": {
            "description": "Inspect the current agent run's own execution artifacts and provenance when the answer is about this live run/session, not historical workspace activity.",
            "domains": ["current run provenance", "current branch", "current commit", "current PR", "files edited in this run"],
            "scopes": ["narrow"],
        },
    },
    "browser": {
        "permission": "automate_browser",
        "risk_class": "high",
        "side_effect_class": "browser_interaction",
        "reversibility": "variable",
        "action_manifest": True,
        "expected_effect": "control or inspect the live browser session",
        "output_budget_chars": 14_000,
    },
}

def _definition_sources() -> list[tuple[str, tuple[str, ...], list[Mapping[str, Any]]]]:
    sources: list[tuple[str, tuple[str, ...], list[Mapping[str, Any]]]] = [
        ("brain", ("coordinator", "worker"), BRAIN_TOOLS),
        ("soul", ("coordinator",), SOUL_TOOLS),
        ("domains", ("coordinator", "worker"), DOMAIN_TOOLS),
        ("inbound", ("coordinator", "worker"), INBOUND_TOOLS),
        ("ideas", ("coordinator", "worker"), CORTEX_IDEA_TOOLS),
        ("chat", ("coordinator", "worker"), CHAT_TOOLS),
        ("launch_handoffs", ("coordinator", "worker"), LAUNCH_HANDOFF_TOOLS),
        ("projects", ("coordinator", "worker"), PROJECT_TOOLS),
        ("workspace_apps", ("coordinator", "worker"), WORKSPACE_APP_TOOLS),
        ("github", ("coordinator", "worker"), GITHUB_TOOLS),
        ("execution", ("coordinator", "worker"), EXEC_TOOLS),
        ("session", ("coordinator", "worker"), SESSION_TOOLS),
        ("lifecycle", ("coordinator",), LIFECYCLE_TOOLS),
        ("deployment", ("coordinator",), DEPLOYMENT_TOOLS),
        ("workspace_tools", ("coordinator",), WORKSPACE_TOOL_TOOLS),
        ("worker_spawn", ("coordinator",), WORKER_SPAWN_TOOLS),
        ("reply", ("coordinator",), [CORTEX_REPLY_TOOL]),
        ("visual_reply", ("coordinator", "worker"), [CORTEX_VISUAL_REPLY_TOOL]),
        ("introspection", ("coordinator",), [MY_ACTIVITY_TOOL]),
        ("browser", ("coordinator", "worker"), _BROWSER_TOOLS),
    ]
    try:
        from brain.systems.tools.handlers import EXTENDED_TOOLS

        sources.append(("extended", ("coordinator", "worker"), EXTENDED_TOOLS))
    except Exception:
        pass
    return sources


def _default_registration(
    *,
    definition: Mapping[str, Any],
    toolset: str,
    availability: tuple[str, ...],
) -> ToolRegistration:
    name = str(definition["name"])
    metadata = dict(_STATIC_METADATA.get(name, {}))
    return ToolRegistration(
        name=name,
        description=str(definition.get("description") or ""),
        schema=definition.get("input_schema") or {},
        toolset=toolset,
        availability=availability,
        permission=str(metadata.get("permission") or "read"),
        risk_class=str(metadata.get("risk_class") or "low"),
        side_effect_class=str(metadata.get("side_effect_class") or "read_only"),
        reversibility=str(metadata.get("reversibility") or "none"),
        output_budget_chars=int(metadata.get("output_budget_chars") or _DEFAULT_OUTPUT_BUDGET_CHARS),
        parallel_safety=str(metadata.get("parallel_safety") or "serial"),
        evidence_emitter=bool(metadata.get("evidence_emitter", False)),
        action_manifest=bool(metadata.get("action_manifest", False)),
        expected_effect=metadata.get("expected_effect"),
        context_route=metadata.get("context_route"),
    )


def _build_registry() -> dict[str, ToolRegistration]:
    registrations: dict[str, ToolRegistration] = {}
    for toolset, availability, definitions in _definition_sources():
        for definition in definitions:
            name = str(definition.get("name") or "")
            if not name:
                raise ValueError(f"Tool definition in {toolset} is missing a name")
            if name in registrations:
                previous = registrations[name]
                if previous.schema != (definition.get("input_schema") or {}):
                    raise ValueError(f"Conflicting tool schema registration for {name}")
                continue
            registrations[name] = _default_registration(
                definition=definition,
                toolset=toolset,
                availability=availability,
            )
    return registrations


def _validate_registry(registrations: Mapping[str, ToolRegistration]) -> None:
    """Fail fast if registry policy metadata is not normalized and complete."""
    for name, registration in registrations.items():
        if not registration.availability or any(
            not isinstance(role, ToolAvailability) for role in registration.availability
        ):
            raise ValueError(f"Tool {name!r} has untyped availability metadata")
        if not isinstance(registration.permission, ToolPermission):
            raise ValueError(f"Tool {name!r} has untyped permission metadata")
        if not isinstance(registration.risk_class, ToolRiskClass):
            raise ValueError(f"Tool {name!r} has untyped risk_class metadata")
        if not isinstance(registration.side_effect_class, ToolSideEffectClass):
            raise ValueError(f"Tool {name!r} has untyped side_effect_class metadata")
        if not isinstance(registration.reversibility, ToolReversibility):
            raise ValueError(f"Tool {name!r} has untyped reversibility metadata")
        if not isinstance(registration.parallel_safety, ToolParallelSafety):
            raise ValueError(f"Tool {name!r} has untyped parallel_safety metadata")
        if (
            registration.side_effect_class != ToolSideEffectClass.READ_ONLY
            and not registration.expected_effect
        ):
            raise ValueError(f"Side-effecting tool {name!r} must declare expected_effect")
        if registration.context_route is not None:
            if registration.side_effect_class != ToolSideEffectClass.READ_ONLY:
                raise ValueError(f"Context route tool {name!r} must be read-only")
            if ToolAvailability.COORDINATOR not in registration.availability:
                raise ValueError(f"Context route tool {name!r} must be coordinator-available")


_REGISTRY = _build_registry()
_validate_registry(_REGISTRY)


def _ensure_dynamic_registrations() -> None:
    """Refresh optional tool registrations that can be hidden during circular imports."""
    global _REGISTRY
    try:
        from brain.systems.tools.handlers import EXTENDED_TOOLS
    except Exception:
        return
    expected = {str(tool.get("name") or "") for tool in EXTENDED_TOOLS}
    if expected and not expected <= set(_REGISTRY):
        _REGISTRY = _build_registry()
        _validate_registry(_REGISTRY)


def all_tool_registrations() -> dict[str, ToolRegistration]:
    """Return all known tool registrations keyed by tool name."""
    _ensure_dynamic_registrations()
    return dict(_REGISTRY)


def get_tool_registration(name: str) -> ToolRegistration | None:
    """Return a tool registration by name."""
    _ensure_dynamic_registrations()
    return _REGISTRY.get(name)


def context_route_registrations() -> dict[str, ToolRegistration]:
    """Return read-only tools that advertise lightweight context-reply routing."""
    _ensure_dynamic_registrations()
    return {
        name: registration
        for name, registration in _REGISTRY.items()
        if registration.context_route is not None
    }


def context_route_tool_names() -> frozenset[str]:
    """Return names the scout LLM may select as context_tool."""
    return frozenset(context_route_registrations())


def context_route_payload() -> list[dict[str, Any]]:
    """Return compact context-route metadata for router prompts and traces."""
    routes: list[dict[str, Any]] = []
    for name, registration in sorted(context_route_registrations().items()):
        route = registration.context_route
        if route is None:
            continue
        routes.append({
            "name": name,
            **route.to_payload(),
        })
    return routes


def output_budget_chars_for_tool(name: str) -> int:
    """Return the model-visible output budget for a tool result."""
    registration = get_tool_registration(name)
    if registration is None:
        return _DEFAULT_OUTPUT_BUDGET_CHARS
    return int(registration.output_budget_chars or _DEFAULT_OUTPUT_BUDGET_CHARS)


def parallel_safe_tool_names(*, scope: Literal["batch", "agent"] = "batch") -> frozenset[str]:
    """Return tool names that may run concurrently in the requested runtime scope."""
    _ensure_dynamic_registrations()
    allowed = (
        {ToolParallelSafety.SAFE}
        if scope == "batch"
        else {ToolParallelSafety.SAFE, ToolParallelSafety.AGENT_SAFE}
    )
    return frozenset(
        name for name, registration in _REGISTRY.items()
        if registration.parallel_safety in allowed
    )


def action_manifest_tool_names() -> frozenset[str]:
    """Return tool names whose side effects should be action-manifest audited."""
    _ensure_dynamic_registrations()
    return frozenset(
        name for name, registration in _REGISTRY.items()
        if registration.action_manifest
    )


def _arg_at(args: tuple, kwargs: dict, name: str, index: int, default=None):
    if name in kwargs:
        return kwargs[name]
    if len(args) > index:
        return args[index]
    return default


def _exec_risk(command: str) -> ToolRiskClass:
    lowered = (command or "").lower()
    if re.search(
        r"\b(git\s+push|git\s+reset|git\s+checkout\s+--|gh\s+pr\s+merge|gh\s+release|deploy|kubectl|terraform\s+apply)\b",
        lowered,
    ):
        return ToolRiskClass.HIGH
    if re.search(
        r"\b(git\s+commit|git\s+cherry-pick|git\s+merge|gh\s+pr\s+create|gh\s+issue\s+create|npm\s+publish)\b",
        lowered,
    ):
        return ToolRiskClass.MEDIUM
    return ToolRiskClass.MEDIUM


def _exec_deny_reason(command: str) -> str | None:
    lowered = (command or "").lower()
    if re.search(
        r"(\bgit\s+reset\s+--hard\b|\bgit\s+checkout\s+--(?:\s|$)|\brm\s+-rf\s+/|\bkubectl\s+delete\b|\bterraform\s+destroy\b)",
        lowered,
    ):
        return "destructive shell command is denied by action policy"
    return None


def action_policy_for_tool(
    tool_name: str,
    *,
    args: Iterable[Any] | tuple = (),
    kwargs: Mapping[str, Any] | None = None,
) -> dict[str, str] | None:
    """Return action policy metadata for a concrete invocation."""
    args_tuple = tuple(args)
    kwargs_dict = dict(kwargs or {})
    if tool_name in {"manage_cycle", "manage_cron_job"} and _arg_at(args_tuple, kwargs_dict, "action", 0) in {"help", "schema", "list"}:
        return None
    if tool_name == "manage_domain" and _arg_at(args_tuple, kwargs_dict, "action", 0) in {"help", "schema", "list", "query_records", "get_record", "events"}:
        return None
    if tool_name == "manage_inbound" and _arg_at(args_tuple, kwargs_dict, "action", 0) in {
        "help",
        "schema",
        "list_connections",
        "get_connection",
        "list_tokens",
        "get_token",
        "list_policies",
        "get_policy",
        "list_projections",
        "get_projection",
        "list_events",
        "list_attention_events",
        "get_event",
        "list_receipts",
        "dry_run_match",
        "replay_events",
        "get_source_card",
    }:
        return None
    if tool_name == "manage_inbound":
        inbound_action = str(_arg_at(args_tuple, kwargs_dict, "action", 0, "") or "").strip().lower()
        effect_by_action = {
            "create_connection": "create an external source connection for inbound signals",
            "update_connection": "update an external source connection",
            "mint_token": "mint a scoped source token for inbound signal submission",
            "list_tokens": "read source token metadata",
            "get_token": "read source token metadata",
            "revoke_token": "revoke an inbound source token",
            "create_policy": "create an inbound source policy",
            "update_policy": "update an inbound source policy",
            "create_projection": "create an inbound Domain Projection",
            "update_projection": "update an inbound Domain Projection",
            "refresh_source_card": "refresh persisted inbound source-card metadata",
        }
        return {
            "risk": "high",
            "reversibility": "variable",
            "expected_effect": effect_by_action.get(inbound_action, "mutate inbound coordination configuration"),
        }
    if tool_name == "manage_skill" and _arg_at(args_tuple, kwargs_dict, "action", 0) in {"help", "schema", "list", "get", "list_assets", "get_asset"}:
        return None
    if tool_name == "manage_workspace_tools" and _arg_at(args_tuple, kwargs_dict, "action", 0) in {"help", "schema", "catalog", "list", "status", "check"}:
        return None
    if tool_name == "manage_workspace_tools":
        workspace_tool_action = str(_arg_at(args_tuple, kwargs_dict, "action", 0, "") or "").strip().lower()
        effect_by_action = {
            "install": "queue a persisted workspace tool bundle installation",
        }
        return {
            "risk": "high",
            "reversibility": "variable",
            "expected_effect": effect_by_action.get(
                workspace_tool_action,
                "mutate persisted workspace tool bundle installations",
            ),
        }
    if tool_name == "manage_skill":
        skill_action = str(_arg_at(args_tuple, kwargs_dict, "action", 0, "") or "").strip().lower()
        effect_by_action = {
            "create": "create a durable slash-routable skill",
            "create_many": "create multiple durable slash-routable skills",
            "update": "update an installed skill",
            "edit": "update an installed skill",
            "archive": "archive an installed skill",
            "delete": "archive an installed skill",
            "convert_to_bundle": "convert a skill to a local bundle-backed skill",
            "upsert_asset": "add or replace a skill bundle asset",
            "delete_asset": "delete a skill bundle asset",
        }
        return {
            "risk": "high",
            "reversibility": "variable",
            "expected_effect": effect_by_action.get(skill_action, "mutate an installed Illo skill"),
        }
    if tool_name == "manage_idea" and _arg_at(args_tuple, kwargs_dict, "action", 0) in {"help", "schema", "list", "get"}:
        return None
    if tool_name == "manage_project" and _arg_at(args_tuple, kwargs_dict, "action", 0) in {"help", "schema", "list", "get", "search_files", "mount_reference"}:
        return None
    if tool_name == "manage_workspace_app" and _arg_at(args_tuple, kwargs_dict, "action", 0) in {
        "help",
        "schema",
        "list",
        "get",
        "get_state",
        "get_collaboration",
        "list_events",
    }:
        return None
    if tool_name == "manage_soul" and _arg_at(args_tuple, kwargs_dict, "action", 0) == "read":
        return None
    if tool_name == "browser":
        browser_action = str(_arg_at(args_tuple, kwargs_dict, "action", 0, "") or "").strip().lower()
        if browser_action in {"help", "list_tabs", "wait", "extract", "discover"}:
            return None
        if browser_action == "snapshot" and not bool(_arg_at(args_tuple, kwargs_dict, "persist", 21, False)):
            return None
    if tool_name == "exec_command":
        command = str(_arg_at(args_tuple, kwargs_dict, "command", 0, ""))
        policy = {
            "risk": _exec_risk(command).value,
            "reversibility": "variable",
            "expected_effect": "execute shell command",
        }
        deny_reason = _exec_deny_reason(command)
        if deny_reason:
            policy["default_result"] = ActionPolicyResult.DENY.value
            policy["deny_reason"] = deny_reason
        return policy
    _ensure_dynamic_registrations()
    registration = _REGISTRY.get(tool_name)
    if registration is not None:
        return registration.to_action_policy()
    if tool_name == "manage_cron_job":
        return {
            "risk": "high",
            "reversibility": "reversible",
            "expected_effect": "mutate a scheduled job",
        }
    return None

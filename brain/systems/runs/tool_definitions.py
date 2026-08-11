"""Tool schema definitions for the Illo agent loop.

Contains all tool definitions (BRAIN_TOOLS, EXEC_TOOLS, etc.) and
tier constants. These are pure data — no handlers, no side effects.
"""

from __future__ import annotations

from brain.systems.runs.tool_catalog.definitions.brain import (
    BRAIN_TOOLS,
    WORKSPACE_OVERVIEW_SPARSE_GUIDANCE,
)
from brain.systems.runs.tool_catalog.definitions.browser import (
    BROWSER_BACK_TOOL,
    BROWSER_CLICK_TOOL,
    BROWSER_CLOSE_TAB_TOOL,
    BROWSER_CLOSE_TOOL,
    BROWSER_DISCOVER_TOOL,
    BROWSER_EXTRACT_TOOL,
    BROWSER_FORWARD_TOOL,
    BROWSER_KEY_TOOL,
    BROWSER_LIST_TABS_TOOL,
    BROWSER_NAVIGATE_TOOL,
    BROWSER_NEW_TAB_TOOL,
    BROWSER_PRINT_PDF_TOOL,
    BROWSER_SAVE_SCREENSHOT_TOOL,
    BROWSER_SESSION_OPEN_TOOL,
    BROWSER_SNAPSHOT_TOOL,
    BROWSER_SWITCH_TAB_TOOL,
    BROWSER_TOOL,
    BROWSER_TYPE_TOOL,
    BROWSER_UPLOAD_ATTACHMENT_TOOL,
    BROWSER_WAIT_TOOL,
)
from brain.systems.runs.tool_catalog.definitions.cortex_thread import (
    CHAT_TOOLS,
    CORTEX_IDEA_TOOLS,
    LAUNCH_HANDOFF_TOOLS,
)
from brain.systems.runs.tool_catalog.definitions.domain_inbound import (
    DOMAIN_TOOLS,
    INBOUND_TOOLS,
)
from brain.systems.runs.tool_catalog.definitions.execution import EXEC_TOOLS
from brain.systems.runs.tool_catalog.definitions.github import GITHUB_TOOLS
from brain.systems.runs.tool_catalog.definitions.knowledge import KNOWLEDGE_TOOLS
from brain.systems.runs.tool_catalog.definitions.meetings import MEETING_TOOLS
from brain.systems.runs.tool_catalog.definitions.run_support import (
    CORTEX_REPLY_TOOL,
    CORTEX_VISUAL_REPLY_TOOL,
    LIFECYCLE_TOOLS,
    MY_ACTIVITY_TOOL,
    SESSION_TOOLS,
    SOUL_TOOLS,
)
from brain.systems.runs.tool_catalog.definitions.runtime_management import (
    DEPLOYMENT_TOOLS,
    HOST_CAPACITY_TOOLS,
    RUNTIME_PREFERENCE_TOOLS,
    WORKSPACE_TOOL_TOOLS,
)
from brain.systems.runs.tool_catalog.definitions.workspace_surfaces import (
    PROJECT_TOOLS,
    WORKSPACE_APP_TOOLS,
)
from brain.systems.runs.tool_catalog.definitions.workers import WORKER_SPAWN_TOOLS


# ── Composite Tool Lists ─────────────────────────────────────

# Worker tools = normal workspace/product capabilities. Harness orchestration
# is AgentRun-owned, not model-visible.
WORKER_TOOLS = (
    BRAIN_TOOLS
    + DOMAIN_TOOLS
    + INBOUND_TOOLS
    + CORTEX_IDEA_TOOLS
    + CHAT_TOOLS
    + LAUNCH_HANDOFF_TOOLS
    + PROJECT_TOOLS
    + WORKSPACE_APP_TOOLS
    + GITHUB_TOOLS
    + KNOWLEDGE_TOOLS
    + MEETING_TOOLS
    + HOST_CAPACITY_TOOLS
    + EXEC_TOOLS
    + SESSION_TOOLS
    + [
        CORTEX_VISUAL_REPLY_TOOL,
        MY_ACTIVITY_TOOL,
        BROWSER_TOOL,
    ]
)

# Coordinator tools = normal workspace/product capabilities plus reply and
# introspection tools. Deep planning remains recipe-owned; worker spawning is a
# coordinator action so Fast can delegate independent slices without switching recipes.
COORDINATOR_TOOLS = (
    BRAIN_TOOLS
    + SOUL_TOOLS
    + DOMAIN_TOOLS
    + INBOUND_TOOLS
    + CORTEX_IDEA_TOOLS
    + CHAT_TOOLS
    + LAUNCH_HANDOFF_TOOLS
    + PROJECT_TOOLS
    + WORKSPACE_APP_TOOLS
    + GITHUB_TOOLS
    + KNOWLEDGE_TOOLS
    + MEETING_TOOLS
    + HOST_CAPACITY_TOOLS
    + EXEC_TOOLS
    + SESSION_TOOLS
    + LIFECYCLE_TOOLS
    + DEPLOYMENT_TOOLS
    + RUNTIME_PREFERENCE_TOOLS
    + WORKSPACE_TOOL_TOOLS
    + WORKER_SPAWN_TOOLS
    + [
        CORTEX_REPLY_TOOL,
        CORTEX_VISUAL_REPLY_TOOL,
        MY_ACTIVITY_TOOL,
        BROWSER_TOOL,
    ]
)

# ── Gate Constants ────────────────────────────────────────────

# Brain gate: these tool names satisfy the "brain context accessed" requirement
_BRAIN_TOOL_NAMES = frozenset({
    "brain_recall", "brain_guardrails", "brain_skills", "skill_view", "skill_asset",
    "memory_reconstruct", "memory_ingest_source", "memory_link", "memory_supersede", "memory_archive",
    "brain_encode", "runtime_settings", "read_self_context", "read_capabilities", "query_workspace_data", "read_workspace_overview",
    "read_team_activity", "read_project_contexts", "read_team_members", "read_workspace_records",
    "read_cycles", "read_workspace_apps",
})

# Brain gate: these tools require brain context before first use
# Destructive tools need guardrails; cortex_reply needs recall so the agent
# checks its memory before asking users questions it should already know.
_GATED_TOOL_NAMES = frozenset({
    "write_file", "edit_file", "exec_command", "run_script", "cortex_reply", "cortex_visual_reply",
})

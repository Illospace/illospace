"""Facade for AgentRun tool handlers.

Domain implementations live under :mod:`brain.systems.runs.tool_catalog.handlers`.
`_get_tool_handlers()` remains the public composition function used by the
agent loop.
"""

from __future__ import annotations

from brain.systems.runs.tool_catalog.handlers import composition as _composition
from brain.systems.runs.tool_catalog.handlers.browser import *  # noqa: F401,F403
from brain.systems.runs.tool_catalog.handlers.common import *  # noqa: F401,F403
from brain.systems.runs.tool_catalog.handlers.activity import *  # noqa: F401,F403
from brain.systems.runs.tool_catalog.handlers.capabilities import *  # noqa: F401,F403
from brain.systems.runs.tool_catalog.handlers.chat import *  # noqa: F401,F403
from brain.systems.runs.tool_catalog.handlers.cortex_reply import *  # noqa: F401,F403
from brain.systems.runs.tool_catalog.handlers.cycles import *  # noqa: F401,F403
from brain.systems.runs.tool_catalog.handlers.ideas import *  # noqa: F401,F403
from brain.systems.runs.tool_catalog.handlers.files import *  # noqa: F401,F403
from brain.systems.runs.tool_catalog.handlers.inbound import *  # noqa: F401,F403
from brain.systems.runs.tool_catalog.handlers.projects import *  # noqa: F401,F403
from brain.systems.runs.tool_catalog.handlers.session_tools import *  # noqa: F401,F403
from brain.systems.runs.tool_catalog.handlers.skills import *  # noqa: F401,F403
from brain.systems.runs.tool_catalog.handlers.voice import *  # noqa: F401,F403
from brain.systems.runs.tool_catalog.handlers.web import *  # noqa: F401,F403
from brain.systems.runs.tool_catalog.handlers.workers import *  # noqa: F401,F403
from brain.systems.runs.tool_catalog.handlers.workspace_data import *  # noqa: F401,F403
from brain.systems.runs.tool_catalog.handlers.workspace_apps import *  # noqa: F401,F403
from brain.systems.runs.tool_catalog.handlers.composition import get_tools_with_extended

_COMPOSITION_PATCH_NAMES = (
    "_handle_browser",
    "_handle_browser_back",
    "_handle_browser_click",
    "_handle_browser_close",
    "_handle_browser_close_tab",
    "_handle_browser_discover",
    "_handle_browser_extract",
    "_handle_browser_forward",
    "_handle_browser_key",
    "_handle_browser_list_tabs",
    "_handle_browser_navigate",
    "_handle_browser_new_tab",
    "_handle_browser_print_pdf",
    "_handle_browser_save_screenshot",
    "_handle_browser_session_open",
    "_handle_browser_snapshot",
    "_handle_browser_switch_tab",
    "_handle_browser_type",
    "_handle_browser_upload_attachment",
    "_handle_browser_wait",
    "_handle_read_capabilities",
    "_handle_cortex_reply",
    "_handle_cortex_visual_reply",
    "_handle_post_ai_timeline_message",
    "_handle_post_chat_message",
    "_handle_post_thread_discussion_reply",
    "_handle_read_thread_discussion",
    "_handle_read_thread_messages",
    "_handle_manage_cycle",
    "_handle_manage_domain",
    "_handle_manage_inbound",
    "_handle_manage_idea",
    "_handle_manage_project",
    "_handle_manage_skill",
    "_handle_manage_workspace_app",
    "_handle_edit_file",
    "_handle_exec_command",
    "_handle_list_files",
    "_handle_read_file",
    "_handle_run_script",
    "_handle_search_files",
    "_handle_write_file",
    "_handle_session_append",
    "_handle_session_close",
    "_handle_session_list",
    "_handle_session_promote",
    "_handle_session_read",
    "_handle_session_write",
    "_handle_my_activity",
    "_handle_transcribe_audio_attachment",
    "_handle_spawn_worker",
    "_handle_query_workspace_data",
    "_handle_read_cycles",
    "_handle_read_project_contexts",
    "_handle_read_team_activity",
    "_handle_read_team_members",
    "_handle_read_workspace_apps",
    "_handle_read_workspace_overview",
    "_handle_read_workspace_records",
    "_handle_web_fetch",
    "_handle_web_search",
)


def _sync_composition_patch_names() -> None:
    current = globals()
    for name in _COMPOSITION_PATCH_NAMES:
        if name in current:
            setattr(_composition, name, current[name])


def _get_tool_handlers(
    workspace_root: str | None = None,
    allowed_workspaces: list[str | dict] | None = None,
    reader_policy: dict | None = None,
) -> dict:
    _sync_composition_patch_names()
    return _composition._get_tool_handlers(
        workspace_root=workspace_root,
        allowed_workspaces=allowed_workspaces,
        reader_policy=reader_policy,
    )

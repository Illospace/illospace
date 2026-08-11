"""Compatibility composition for orchestration tool handlers."""

from __future__ import annotations

from brain.systems.runs.tool_catalog.handlers.browser import (
    _handle_browser,
    _handle_browser_back,
    _handle_browser_click,
    _handle_browser_close,
    _handle_browser_close_tab,
    _handle_browser_discover,
    _handle_browser_extract,
    _handle_browser_forward,
    _handle_browser_key,
    _handle_browser_list_tabs,
    _handle_browser_navigate,
    _handle_browser_new_tab,
    _handle_browser_print_pdf,
    _handle_browser_save_screenshot,
    _handle_browser_session_open,
    _handle_browser_snapshot,
    _handle_browser_switch_tab,
    _handle_browser_type,
    _handle_browser_upload_attachment,
    _handle_browser_wait,
)
from brain.systems.runs.tool_catalog.handlers.capabilities import _handle_read_capabilities
from brain.systems.runs.tool_catalog.handlers.common import *
from brain.systems.runs.tool_catalog.handlers.cortex_reply import (
    _handle_cortex_reply,
    _handle_cortex_visual_reply,
)
from brain.systems.runs.tool_catalog.handlers.chat import (
    _handle_post_ai_timeline_message,
    _handle_post_chat_message,
    _handle_post_thread_discussion_reply,
    _handle_publish_thread_asset,
    _handle_read_thread_discussion,
)
from brain.systems.runs.tool_catalog.handlers.cycles import _handle_manage_cycle
from brain.systems.runs.tool_catalog.handlers.domains import (
    _handle_manage_domain,
    _handle_merge_chantier,
)
from brain.systems.runs.tool_catalog.handlers.inbound import _handle_manage_inbound
from brain.systems.runs.tool_catalog.handlers.github import (
    _handle_add_github_issue_comment,
    _handle_add_github_sub_issue,
    _handle_check_fix_deploy_state,
    _handle_create_github_issue,
    _handle_create_github_pull_request,
    _handle_list_github_sub_issues,
    _handle_read_github_source,
    _handle_remove_github_sub_issue,
    _handle_update_github_issue,
)
from brain.systems.runs.tool_catalog.handlers.knowledge import _handle_search_knowledge
from brain.systems.runs.tool_catalog.handlers.meetings import (
    _handle_join_meeting,
    _handle_leave_meeting,
    _handle_meeting_status,
    _handle_send_meeting_chat,
)
from brain.systems.runs.tool_catalog.handlers.ideas import _handle_manage_idea
from brain.systems.runs.tool_catalog.handlers.launch_handoffs import _handle_create_launch_handoff
from brain.systems.runs.tool_catalog.handlers.projects import _handle_manage_project
from brain.systems.runs.tool_catalog.handlers.skills import _handle_manage_skill
from brain.systems.runs.tool_catalog.handlers.files import (
    _handle_edit_file,
    _handle_exec_command,
    _handle_list_files,
    _handle_read_file,
    _handle_run_script,
    _handle_search_files,
    _handle_write_file,
)
from brain.systems.runs.tool_catalog.handlers.session_tools import (
    _handle_read_thread_messages,
    _handle_session_append,
    _handle_session_close,
    _handle_session_list,
    _handle_session_promote,
    _handle_session_read,
    _handle_session_write,
)
from brain.systems.runs.tool_catalog.handlers.self_context import _handle_read_self_context
from brain.systems.runs.tool_catalog.handlers.slack import (
    _handle_manage_slack,
    _handle_post_slack_reply,
    _handle_react_to_slack_message,
    _handle_read_slack_conversation,
)
from brain.systems.runs.tool_catalog.handlers.thread_artifacts import _handle_publish_thread_artifact
from brain.systems.runs.tool_catalog.handlers.activity import _handle_my_activity
from brain.systems.runs.tool_catalog.handlers.voice import _handle_transcribe_audio_attachment
from brain.systems.runs.tool_catalog.handlers.web import _handle_web_fetch, _handle_web_search
from brain.systems.runs.tool_catalog.handlers.workers import _handle_spawn_worker
from brain.systems.runs.tool_catalog.handlers.workspace_data import (
    _handle_query_workspace_data,
    _handle_read_cycles,
    _handle_read_project_contexts,
    _handle_read_team_activity,
    _handle_read_team_members,
    _handle_read_workspace_apps,
    _handle_read_workspace_overview,
    _handle_read_workspace_records,
)
from brain.systems.runs.tool_catalog.handlers.workspace_apps import _handle_manage_workspace_app
from brain.systems.runs.tool_catalog.handlers.workspace_tools import _handle_manage_workspace_tools


def _current_agent_value(name: str):
    value = getattr(_agent_context, name, None)
    if value:
        return value
    run = getattr(_agent_context, "run", None)
    value = getattr(run, name, None)
    if value:
        return value
    execution_metadata = getattr(_agent_context, "execution_metadata", None)
    if isinstance(execution_metadata, dict):
        value = execution_metadata.get(name)
        if value:
            return value
    return None


def _current_run_id():
    run = getattr(_agent_context, "run", None)
    return getattr(run, "run_id", None) or _current_agent_value("run_id")


def _current_idea_id():
    idea_id = _current_agent_value("idea_id")
    if idea_id:
        return str(idea_id)
    run = getattr(_agent_context, "run", None)
    thread_id = getattr(run, "thread_id", None)
    if thread_id:
        return str(thread_id)
    metadata = getattr(_agent_context, "execution_metadata", None)
    if isinstance(metadata, dict):
        target_ref = metadata.get("target_ref")
        if isinstance(target_ref, dict):
            candidate = target_ref.get("idea_id") or target_ref.get("thread_id")
            if candidate:
                return str(candidate)
    return None


def _current_requested_by():
    return getattr(_agent_context, "worker_name", None) or "agent"


def _resolved_secret_env_arg(secret_env=None, _resolved_secret_env=None):
    if secret_env not in (None, {}, []):
        raise ValueError("secret_env mount specs must be resolved by the runtime before invoking tool handlers")
    return _resolved_secret_env


def _resolved_workspace_tool_runtime_args(
    workspace_tool_auth=None,
    _resolved_workspace_tool_env=None,
    _resolved_workspace_tool_sensitive_values=None,
):
    if workspace_tool_auth not in (None, {}, []):
        raise ValueError("workspace_tool_auth specs must be resolved by the runtime before invoking tool handlers")
    return _resolved_workspace_tool_env, _resolved_workspace_tool_sensitive_values


def _run_on_event_loop_adapter(handler):
    """Mark a sync compatibility adapter whose returned awaitable does the work."""
    handler._illo_run_on_event_loop = True
    return handler


def _private_async_adapter(name: str, default):
    return _run_on_event_loop_adapter(
        lambda **kwargs: _patched_private(name, default)(**kwargs)
    )


def _get_tool_handlers(
    workspace_root: str | None = None,
    allowed_workspaces: list[str | dict] | None = None,
    reader_policy: dict | None = None,
) -> dict:
    """Build the tool handler map. Lazy import to avoid circular deps.

    Args:
        workspace_root: Override workspace root for all file/exec tools.
                       Used by run-level worktrees to isolate agents.
                       When set, all file and exec operations are confined
                       to this directory instead of the default WORKSPACE_ROOT.
    """
    from brain.app.mcp.server import (
        async_tool_brain_recall,
        async_tool_memory_archive,
        async_tool_memory_link,
        async_tool_memory_reconstruct,
        async_tool_memory_supersede,
        async_tool_memory_ingest_source,
        async_tool_brain_guardrails,
        async_tool_brain_skills,
        async_tool_skill_view,
        async_tool_skill_asset,
        async_tool_brain_encode,
        tool_brain_vault,
        tool_vault_inventory,
        tool_vault_secret_prompt,
        tool_runtime_settings,
        tool_manage_deployment,
        tool_manage_runtime_services,
    )
    from brain.systems.personality import manage_agent_soul

    def _manage_deployment(action="status", build_no_cache=False, worker_drain_timeout_seconds=None):
        return tool_manage_deployment(
            action=action,
            build_no_cache=build_no_cache,
            worker_drain_timeout_seconds=worker_drain_timeout_seconds,
            user_id=getattr(_agent_context, "user_id", None),
            org_id=getattr(_agent_context, "org_id", None),
        )

    def _manage_runtime_services(action="list", services=None):
        return tool_manage_runtime_services(
            action=action,
            services=services,
            user_id=getattr(_agent_context, "user_id", None),
            org_id=getattr(_agent_context, "org_id", None),
        )

    async def _manage_runtime_preferences(
        action="get",
        setting=None,
        value=None,
    ):
        from brain.platform.db.repositories.unit_of_work import UnitOfWork
        from brain.systems.runtime_settings.preferences import (
            RuntimePreferenceAccessError,
            async_manage_runtime_preferences,
            authenticate_runtime_preference_principal,
            denied_runtime_preference_result,
        )

        async with UnitOfWork() as uow:
            try:
                principal = await authenticate_runtime_preference_principal(
                    uow.session,
                    user_id=_current_agent_value("user_id"),
                    org_id=_current_agent_value("org_id"),
                )
            except RuntimePreferenceAccessError as exc:
                result = denied_runtime_preference_result(exc)
            else:
                result = await async_manage_runtime_preferences(
                    uow.session,
                    principal=principal,
                    run_id=_coerce_agent_run_id(_current_run_id()),
                    action=action,
                    setting=setting,
                    value=value,
                )
        return result

    async def _manage_storage_policy(
        action="get",
        policy_id=None,
        rationale=None,
        limit=50,
        **storage_values,
    ):
        from brain.platform.db.repositories.unit_of_work import UnitOfWork
        from brain.systems.storage_policy import (
            StoragePolicyPatch,
            async_manage_storage_policy,
        )

        actor_id = _current_run_id() or _current_agent_value("user_id")
        patch = StoragePolicyPatch.from_storage_fields(storage_values)
        async with UnitOfWork() as uow:
            return await async_manage_storage_policy(
                uow.session,
                action=action,
                policy_id=policy_id,
                rationale=rationale,
                source_type="agent",
                source_id=str(actor_id) if actor_id is not None else None,
                limit=limit,
                patch=patch,
            )

    async def _read_host_capacity(limit=24, refresh_inventory=False):
        from brain.kernel import config as brain_config
        from brain.platform.db.repositories.unit_of_work import UnitOfWork
        from brain.systems.host_capacity import async_read_host_capacity

        async with UnitOfWork() as uow:
            return await async_read_host_capacity(
                uow.session,
                workspace_root=brain_config.resolve_workspace_root(),
                limit=limit,
                refresh_inventory=refresh_inventory,
            )

    async def _manage_workspace_reclamation(
        action="inventory",
        limit=100,
        max_reclaims=100,
    ):
        from brain.systems.workspace_reclamation import (
            manage_headless_worker_workspaces,
        )
        from brain.kernel import config as brain_config
        from brain.platform.db.repositories.unit_of_work import UnitOfWork

        async with UnitOfWork() as uow:
            return await manage_headless_worker_workspaces(
                uow.session,
                action=action,
                workspace_root=brain_config.resolve_workspace_root(),
                max_reclaims=max_reclaims,
                report_limit=limit,
            )

    _manage_deployment._illo_run_on_event_loop = True
    _manage_runtime_services._illo_run_on_event_loop = True
    _manage_runtime_preferences._illo_run_on_event_loop = True
    _manage_storage_policy._illo_run_on_event_loop = True
    _read_host_capacity._illo_run_on_event_loop = True
    _manage_workspace_reclamation._illo_run_on_event_loop = True

    handlers = {
        # Brain tools (workspace-independent — always hit shared DB)
        "brain_recall": _wrap_brain_recall(async_tool_brain_recall),
        "memory_reconstruct": _wrap_memory_reconstruct(async_tool_memory_reconstruct),
        "memory_link": _wrap_memory_curation(async_tool_memory_link),
        "memory_supersede": _wrap_memory_curation(async_tool_memory_supersede),
        "memory_archive": _wrap_memory_curation(async_tool_memory_archive),
        "memory_ingest_source": _wrap_memory_ingest_source(async_tool_memory_ingest_source),
        "brain_guardrails": async_tool_brain_guardrails,
        "brain_skills": _wrap_tool_evidence("brain_skills", async_tool_brain_skills),
        "skill_view": _wrap_tool_evidence("skill_view", async_tool_skill_view),
        "skill_asset": async_tool_skill_asset,
        "brain_encode": _wrap_brain_encode(async_tool_brain_encode),
        "brain_vault": _run_on_event_loop_adapter(
            lambda key, reason=None: tool_brain_vault(
                key,
                reason=reason,
                user_id=_current_agent_value("user_id"),
                org_id=_current_agent_value("org_id"),
                run_id=_current_run_id(),
                idea_id=_current_idea_id(),
                requested_by=_current_requested_by(),
                **_current_project_token_context(),
            )
        ),
        "vault_inventory": _run_on_event_loop_adapter(
            lambda category=None, access_level=None: tool_vault_inventory(
                category=category,
                access_level=access_level,
                user_id=_current_agent_value("user_id"),
                org_id=_current_agent_value("org_id"),
            )
        ),
        "vault_secret_prompt": _run_on_event_loop_adapter(
            lambda key_name, description=None, category="api", reason=None: tool_vault_secret_prompt(
                key_name,
                description=description,
                category=category,
                reason=reason,
                user_id=_current_agent_value("user_id"),
                org_id=_current_agent_value("org_id"),
                run_id=_current_run_id(),
                idea_id=_current_idea_id(),
                requested_by=_current_requested_by(),
            )
        ),
        "runtime_settings": _run_on_event_loop_adapter(
            lambda provider=None: tool_runtime_settings(
                provider=provider,
                user_id=getattr(_agent_context, "user_id", None),
                org_id=getattr(_agent_context, "org_id", None),
            )
        ),
        "manage_runtime_preferences": _manage_runtime_preferences,
        "manage_storage_policy": _manage_storage_policy,
        "read_host_capacity": _read_host_capacity,
        "manage_workspace_reclamation": _manage_workspace_reclamation,
        "read_self_context": _handle_read_self_context,
        "read_capabilities": _handle_read_capabilities,
        "manage_deployment": _manage_deployment,
        "manage_runtime_services": _manage_runtime_services,
        "transcribe_audio_attachment": _private_async_adapter(
            "_handle_transcribe_audio_attachment",
            _handle_transcribe_audio_attachment,
        ),
        "manage_soul": lambda action, content=None, reason=None: manage_agent_soul(
            action,
            content=content,
            reason=reason,
            actor_user_id=getattr(_agent_context, "user_id", None),
        ),
        "query_workspace_data": _handle_query_workspace_data,
        "read_workspace_overview": _handle_read_workspace_overview,
        "read_team_activity": _handle_read_team_activity,
        "read_project_contexts": _handle_read_project_contexts,
        "read_team_members": _handle_read_team_members,
        "read_workspace_records": _handle_read_workspace_records,
        "read_cycles": _handle_read_cycles,
        "read_workspace_apps": _handle_read_workspace_apps,
        "read_github_source": _private_async_adapter(
            "_handle_read_github_source",
            _handle_read_github_source,
        ),
        "search_knowledge": _private_async_adapter(
            "_handle_search_knowledge",
            _handle_search_knowledge,
        ),
        "create_github_issue": _private_async_adapter(
            "_handle_create_github_issue",
            _handle_create_github_issue,
        ),
        "create_github_pull_request": _private_async_adapter(
            "_handle_create_github_pull_request",
            _handle_create_github_pull_request,
        ),
        "update_github_issue": _private_async_adapter(
            "_handle_update_github_issue",
            _handle_update_github_issue,
        ),
        "add_github_issue_comment": _private_async_adapter(
            "_handle_add_github_issue_comment",
            _handle_add_github_issue_comment,
        ),
        "add_github_sub_issue": _private_async_adapter(
            "_handle_add_github_sub_issue",
            _handle_add_github_sub_issue,
        ),
        "remove_github_sub_issue": _private_async_adapter(
            "_handle_remove_github_sub_issue",
            _handle_remove_github_sub_issue,
        ),
        "list_github_sub_issues": _private_async_adapter(
            "_handle_list_github_sub_issues",
            _handle_list_github_sub_issues,
        ),
        "join_meeting": _private_async_adapter(
            "_handle_join_meeting",
            _handle_join_meeting,
        ),
        "meeting_status": _private_async_adapter(
            "_handle_meeting_status",
            _handle_meeting_status,
        ),
        "leave_meeting": _private_async_adapter(
            "_handle_leave_meeting",
            _handle_leave_meeting,
        ),
        "send_meeting_chat": _private_async_adapter(
            "_handle_send_meeting_chat",
            _handle_send_meeting_chat,
        ),
        "check_fix_deploy_state": _private_async_adapter(
            "_handle_check_fix_deploy_state",
            _handle_check_fix_deploy_state,
        ),
        "read_thread_messages": _handle_read_thread_messages,
        "post_chat_message": _private_async_adapter(
            "_handle_post_chat_message",
            _handle_post_chat_message,
        ),
        "post_slack_reply": _private_async_adapter(
            "_handle_post_slack_reply",
            _handle_post_slack_reply,
        ),
        "react_to_slack_message": _private_async_adapter(
            "_handle_react_to_slack_message",
            _handle_react_to_slack_message,
        ),
        "post_thread_discussion_reply": _private_async_adapter(
            "_handle_post_thread_discussion_reply",
            _handle_post_thread_discussion_reply,
        ),
        "publish_thread_asset": _private_async_adapter(
            "_handle_publish_thread_asset",
            _handle_publish_thread_asset,
        ),
        "publish_thread_artifact": _private_async_adapter(
            "_handle_publish_thread_artifact",
            _handle_publish_thread_artifact,
        ),
        "post_ai_timeline_message": _private_async_adapter(
            "_handle_post_ai_timeline_message",
            _handle_post_ai_timeline_message,
        ),
        "create_launch_handoff": _private_async_adapter(
            "_handle_create_launch_handoff",
            _handle_create_launch_handoff,
        ),
        "read_thread_discussion": _private_async_adapter(
            "_handle_read_thread_discussion",
            _handle_read_thread_discussion,
        ),
        "read_slack_conversation": _private_async_adapter(
            "_handle_read_slack_conversation",
            _handle_read_slack_conversation,
        ),
        "manage_slack": _private_async_adapter(
            "_handle_manage_slack",
            _handle_manage_slack,
        ),
        "manage_cycle": _private_async_adapter("_handle_manage_cycle", _handle_manage_cycle),
        "manage_domain": _private_async_adapter("_handle_manage_domain", _handle_manage_domain),
        "merge_chantier": _private_async_adapter(
            "_handle_merge_chantier",
            _handle_merge_chantier,
        ),
        "manage_inbound": _private_async_adapter("_handle_manage_inbound", _handle_manage_inbound),
        "manage_idea": _private_async_adapter("_handle_manage_idea", _handle_manage_idea),
        "manage_project": _private_async_adapter("_handle_manage_project", _handle_manage_project),
        "manage_skill": _private_async_adapter("_handle_manage_skill", _handle_manage_skill),
        "manage_workspace_app": _private_async_adapter(
            "_handle_manage_workspace_app",
            _handle_manage_workspace_app,
        ),
        "manage_workspace_tools": _private_async_adapter(
            "_handle_manage_workspace_tools",
            _handle_manage_workspace_tools,
        ),
        # Session scratchpad tools
        "session_write": _handle_session_write,
        "session_read": _handle_session_read,
        "session_append": _handle_session_append,
        "session_list": _handle_session_list,
        # Memory lifecycle tools (coordinator only)
        "session_promote": _handle_session_promote,
        "session_close": _handle_session_close,
        "cortex_reply": _handle_cortex_reply,
        "cortex_visual_reply": _run_on_event_loop_adapter(
            lambda **kwargs: _handle_cortex_visual_reply(**kwargs)
        ),
        "my_activity": _handle_my_activity,
        "spawn_worker": _private_async_adapter("_handle_spawn_worker", _handle_spawn_worker),
        "browser": _private_async_adapter("_handle_browser", _handle_browser),
        "web_search": _handle_web_search,
        "web_fetch": _handle_web_fetch,
    }

    # Execution tools — bind to worktree workspace if provided
    def _predictive_read(
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        workspace: str | None = None,
        _workspace: str | None = None,
    ) -> dict:
        selected_workspace = _workspace
        if workspace is not None:
            selected_workspace = _select_workspace(workspace, ws, allowed_workspaces)
        result = _handle_read_file(path, start_line, end_line, _workspace=selected_workspace)
        if (
            not reader_policy
            or not reader_policy.get("enabled")
            or start_line is not None
            or end_line is not None
            or "error" in result
        ):
            return result

        total_lines = int(result.get("total_lines") or 0)
        content = result.get("content") or ""
        line_threshold = int(reader_policy.get("large_file_lines", 220))
        char_threshold = int(reader_policy.get("large_file_chars", 8000))
        if total_lines < line_threshold and len(content) < char_threshold:
            return result

        summary_handler = handlers.get("summarize_file_for_task")
        if not callable(summary_handler):
            return result

        task_hint = (reader_policy.get("task_hint") or "").strip()
        focus = reader_policy.get("focus")
        question = (
            f"For task '{task_hint[:220]}', summarize the file's relevant responsibilities, "
            "key symbols, likely edit areas, and risks."
            if task_hint
            else "Summarize the file's responsibilities, key symbols, likely edit areas, and risks."
        )
        try:
            summary = summary_handler(
                path=path,
                question=question,
                focus=focus,
                allow_llm=False,
            )
        except Exception as exc:
            logger.debug("Predictive read redirect failed for %s: %s", path, exc)
            return result

        return {
            "redirected": True,
            "reason": (
                f"Large unbounded read auto-routed through predictive reader policy "
                f"({total_lines} lines). Use read_file with line ranges for exact raw code."
            ),
            "path": result.get("path"),
            "total_lines": total_lines,
            "suggested_next_step": "Use read_file with specific line ranges before editing.",
            "summary": summary,
        }

    ws = workspace_root  # capture for closures

    def _exec_command_handler(
        command,
        working_dir=None,
        timeout=60,
        workspace=None,
        secret_env=None,
        workspace_tool_auth=None,
        _resolved_secret_env=None,
        _resolved_workspace_tool_env=None,
        _resolved_workspace_tool_sensitive_values=None,
        **_unexpected_kwargs,
    ):
        if _unexpected_kwargs:
            # Tolerate undocumented/legacy kwargs the model may pass instead of
            # raising a raw TypeError — the schema is the source of truth.
            logger.debug("exec_command ignoring unexpected kwargs: %s", sorted(_unexpected_kwargs))
        workspace_tool_env, workspace_tool_sensitive_values = _resolved_workspace_tool_runtime_args(
            workspace_tool_auth,
            _resolved_workspace_tool_env,
            _resolved_workspace_tool_sensitive_values,
        )
        return _handle_exec_command(
            command,
            working_dir=working_dir,
            timeout=timeout,
            _workspace=_select_workspace(workspace, ws, allowed_workspaces),
            _resolved_secret_env=_resolved_secret_env_arg(secret_env, _resolved_secret_env),
            _resolved_workspace_tool_env=workspace_tool_env,
            _resolved_workspace_tool_sensitive_values=workspace_tool_sensitive_values,
        )

    def _run_script_handler(
        script,
        description=None,
        timeout=60,
        workspace=None,
        secret_env=None,
        workspace_tool_auth=None,
        _resolved_secret_env=None,
        _resolved_workspace_tool_env=None,
        _resolved_workspace_tool_sensitive_values=None,
    ):
        workspace_tool_env, workspace_tool_sensitive_values = _resolved_workspace_tool_runtime_args(
            workspace_tool_auth,
            _resolved_workspace_tool_env,
            _resolved_workspace_tool_sensitive_values,
        )
        return _handle_run_script(
            script,
            description,
            timeout,
            _workspace=_select_workspace(workspace, ws, allowed_workspaces),
            _resolved_secret_env=_resolved_secret_env_arg(secret_env, _resolved_secret_env),
            _resolved_workspace_tool_env=workspace_tool_env,
            _resolved_workspace_tool_sensitive_values=workspace_tool_sensitive_values,
        )

    handlers.update({
        "exec_command": _exec_command_handler,
        "read_file": lambda path, workspace=None, start_line=None, end_line=None: (
            _predictive_read(
                path,
                start_line,
                end_line,
                _workspace=_select_workspace(workspace, ws, allowed_workspaces),
            )
        ),
        "write_file": lambda path, content, workspace=None: (
            _handle_write_file(
                path,
                content,
                _workspace=_select_workspace(workspace, ws, allowed_workspaces),
            )
        ),
        "edit_file": lambda path, old_text, new_text, workspace=None: (
            _handle_edit_file(
                path,
                old_text,
                new_text,
                _workspace=_select_workspace(workspace, ws, allowed_workspaces),
            )
        ),
        "search_files": lambda pattern, path=None, glob=None, workspace=None: (
            _handle_search_files(
                pattern,
                path,
                glob,
                _workspace=_select_workspace(workspace, ws, allowed_workspaces),
            )
        ),
        "list_files": lambda pattern, path=None, workspace=None: (
            _handle_list_files(
                pattern,
                path,
                _workspace=_select_workspace(workspace, ws, allowed_workspaces),
            )
        ),
        "run_script": _run_script_handler,
    })

    # Extended tools (semantic search, file summary, test runner, etc.)
    try:
        from brain.systems.tools.handlers import get_extended_handlers

        extended_handlers = get_extended_handlers()
        handlers.update(extended_handlers)

        def _workspace_hint() -> str | None:
            return _current_workspace_root_hint() or workspace_root

        def _semantic_search_with_workspace(query, scope="both", limit=5):
            return (
                extended_handlers["semantic_search"](
                    query,
                    scope=scope,
                    limit=limit,
                    workspace_root=_workspace_hint(),
                )
            )

        def _file_summary_with_workspace(path):
            return (
                extended_handlers["file_summary"](
                    path,
                    workspace_root=_workspace_hint(),
                )
            )

        handlers["semantic_search"] = _wrap_tool_evidence(
            "semantic_search",
            _semantic_search_with_workspace,
        )
        handlers["file_summary"] = _wrap_tool_evidence(
            "file_summary",
            _file_summary_with_workspace,
        )
        def _test_runner_handler(
            target,
            pattern=None,
            verbose=False,
            secret_env=None,
            workspace_tool_auth=None,
            _resolved_secret_env=None,
            _resolved_workspace_tool_env=None,
            _resolved_workspace_tool_sensitive_values=None,
        ):
            workspace_tool_env, workspace_tool_sensitive_values = _resolved_workspace_tool_runtime_args(
                workspace_tool_auth,
                _resolved_workspace_tool_env,
                _resolved_workspace_tool_sensitive_values,
            )
            return extended_handlers["test_runner"](
                target,
                pattern=pattern,
                verbose=verbose,
                workspace_root=_workspace_hint(),
                _resolved_secret_env=_resolved_secret_env_arg(secret_env, _resolved_secret_env),
                _resolved_workspace_tool_env=workspace_tool_env,
                _resolved_workspace_tool_sensitive_values=workspace_tool_sensitive_values,
            )

        handlers["test_runner"] = _test_runner_handler
        handlers["project_context"] = lambda path=None: (
            extended_handlers["project_context"](
                path=path,
                workspace_root=_workspace_hint(),
            )
        )

        def _current_identity(
            user_id: str | None = None,
            org_id: str | None = None,
        ) -> tuple[str | None, str | None]:
            execution_metadata = getattr(_agent_context, "execution_metadata", None)
            if user_id is None:
                user_id = getattr(_agent_context, "user_id", None)
                if user_id is None and isinstance(execution_metadata, dict):
                    user_id = execution_metadata.get("user_id")
            if org_id is None:
                org_id = getattr(_agent_context, "org_id", None)
                if org_id is None and isinstance(execution_metadata, dict):
                    org_id = execution_metadata.get("org_id")
            return user_id, org_id

        @_run_on_event_loop_adapter
        def _summarize_file_for_task_with_context(
            path,
            question,
            focus=None,
            user_id=None,
            org_id=None,
            allow_llm=True,
        ):
            effective_user_id, effective_org_id = _current_identity(user_id, org_id)
            return extended_handlers["summarize_file_for_task"](
                path,
                question,
                focus=focus,
                user_id=effective_user_id,
                org_id=effective_org_id,
                allow_llm=allow_llm,
                workspace_root=_workspace_hint(),
            )

        @_run_on_event_loop_adapter
        def _summarize_files_for_task_with_context(
            paths,
            question,
            max_files=8,
            output_mode="ranked_evidence",
            user_id=None,
            org_id=None,
            allow_llm=True,
        ):
            effective_user_id, effective_org_id = _current_identity(user_id, org_id)
            return extended_handlers["summarize_files_for_task"](
                paths,
                question,
                max_files=max_files,
                output_mode=output_mode,
                user_id=effective_user_id,
                org_id=effective_org_id,
                allow_llm=allow_llm,
                workspace_root=_workspace_hint(),
            )

        handlers["summarize_file_for_task"] = _summarize_file_for_task_with_context
        handlers["summarize_files_for_task"] = _summarize_files_for_task_with_context
        handlers["trace_symbol"] = lambda symbol, path=None, max_results=20: (
            extended_handlers["trace_symbol"](
                symbol,
                path=path,
                max_results=max_results,
                workspace_root=_workspace_hint(),
            )
        )
        handlers["build_implementation_map"] = lambda question, paths=None, max_files=10: (
            extended_handlers["build_implementation_map"](
                question,
                paths=paths,
                max_files=max_files,
                workspace_root=_workspace_hint(),
            )
        )
    except Exception as e:
        logger.debug(f"Extended tools unavailable: {e}")

    def _parallel_tool_batch(operations: list[dict], max_parallel: int | None = None) -> dict:
        if not isinstance(operations, list) or not operations:
            return {"error": "operations must be a non-empty list"}
        if len(operations) > _MAX_PARALLEL_BATCH_OPERATIONS:
            return {
                "error": (
                    f"parallel_tool_batch supports at most {_MAX_PARALLEL_BATCH_OPERATIONS} operations "
                    f"per call (received {len(operations)})"
                )
            }

        normalized_ops: list[tuple[int, str, dict]] = []
        for idx, op in enumerate(operations):
            if not isinstance(op, dict):
                return {"error": f"operations[{idx}] must be an object"}
            tool_name = str(op.get("tool_name") or "").strip()
            if not tool_name:
                return {"error": f"operations[{idx}].tool_name is required"}
            if tool_name not in _PARALLEL_BATCH_SAFE_TOOL_NAMES:
                allowed = ", ".join(sorted(_PARALLEL_BATCH_SAFE_TOOL_NAMES))
                return {
                    "error": (
                        f"Tool '{tool_name}' is not allowed in parallel_tool_batch. "
                        f"Allowed tools: {allowed}"
                    )
                }
            handler = handlers.get(tool_name)
            if not callable(handler):
                return {"error": f"Tool '{tool_name}' is unavailable in this runtime"}
            args = op.get("args") or {}
            if not isinstance(args, dict):
                return {"error": f"operations[{idx}].args must be an object"}
            normalized_ops.append((idx, tool_name, args))

        threadlocal_context = snapshot_agent_context()
        parallelism = max(1, min(
            max_parallel or len(normalized_ops),
            len(normalized_ops),
            _MAX_PARALLEL_BATCH_WORKERS,
        ))
        results: list[dict | None] = [None] * len(normalized_ops)

        def _run_batch_op(idx: int, tool_name: str, args: dict) -> tuple[int, dict]:
            started_at = time.time()
            try:
                result = _invoke_with_threadlocal_context(handlers[tool_name], args, threadlocal_context)
                payload = {
                    "tool_name": tool_name,
                    "ok": True,
                    "result": result,
                    "duration_ms": int((time.time() - started_at) * 1000),
                }
            except Exception as exc:
                payload = {
                    "tool_name": tool_name,
                    "ok": False,
                    "error": str(exc),
                    "duration_ms": int((time.time() - started_at) * 1000),
                }
            return idx, payload

        with ThreadPoolExecutor(max_workers=parallelism, thread_name_prefix="tool-batch") as executor:
            futures = [
                executor.submit(_run_batch_op, idx, tool_name, args)
                for idx, tool_name, args in normalized_ops
            ]
            for future in futures:
                idx, payload = future.result()
                results[idx] = payload

        completed = [result for result in results if result and result.get("ok")]
        failed = [result for result in results if result and not result.get("ok")]
        return {
            "max_parallel": parallelism,
            "completed": len(completed),
            "failed": len(failed),
            "results": [result for result in results if result is not None],
        }

    handlers["parallel_tool_batch"] = _parallel_tool_batch

    for tool_name in tuple(handlers):
        if tool_name in _ACTION_MANIFEST_TOOL_NAMES:
            handlers[tool_name] = _wrap_action_manifest_audit(tool_name, handlers[tool_name])

    return handlers


def get_tools_with_extended(base_tools: list[dict]) -> list[dict]:
    """Add extended tool definitions to a base tool list.

    Usage:
        tools = get_tools_with_extended(COORDINATOR_TOOLS)
    """
    try:
        from brain.systems.tools.handlers import EXTENDED_TOOLS
        return base_tools + EXTENDED_TOOLS
    except Exception:
        return base_tools


__all__ = [name for name in globals() if not name.startswith("__")]

"""Compatibility composition for orchestration tool handlers."""

from __future__ import annotations

from brain.systems.runs.tool_catalog.handlers.common import *
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
from brain.systems.runs.tool_catalog.handlers.cortex_reply import (
    _build_final_reply_check_context,
    _handle_cortex_reply,
    _handle_cortex_visual_reply,
)
from brain.systems.runs.tool_catalog.handlers.chat import (
    _handle_post_chat_message,
    _handle_post_thread_discussion_reply,
    _handle_read_thread_discussion,
)
from brain.systems.runs.tool_catalog.handlers.cycles import _handle_manage_cycle
from brain.systems.runs.tool_catalog.handlers.domains import _handle_manage_domain
from brain.systems.runs.tool_catalog.handlers.inbound import _handle_manage_inbound
from brain.systems.runs.tool_catalog.handlers.ideas import _handle_manage_idea
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
from brain.systems.runs.tool_catalog.handlers.activity import _handle_my_activity
from brain.systems.runs.tool_catalog.handlers.web import _handle_web_fetch, _handle_web_search
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
        async_tool_brain_guardrails,
        async_tool_brain_skills,
        async_tool_skill_view,
        async_tool_skill_asset,
        async_tool_brain_encode,
        tool_brain_vault,
        tool_vault_inventory,
        tool_vault_secret_prompt,
        tool_runtime_settings,
    )
    from brain.systems.personality import manage_agent_soul

    handlers = {
        # Brain tools (workspace-independent — always hit shared DB)
        "brain_recall": _wrap_brain_recall(async_tool_brain_recall),
        "brain_guardrails": async_tool_brain_guardrails,
        "brain_skills": _wrap_tool_evidence("brain_skills", async_tool_brain_skills),
        "skill_view": _wrap_tool_evidence("skill_view", async_tool_skill_view),
        "skill_asset": async_tool_skill_asset,
        "brain_encode": _wrap_brain_encode(async_tool_brain_encode),
        "brain_vault": lambda key, reason=None: tool_brain_vault(
            key,
            reason=reason,
            user_id=_current_agent_value("user_id"),
            org_id=_current_agent_value("org_id"),
            run_id=_current_run_id(),
            idea_id=_current_idea_id(),
            requested_by=_current_requested_by(),
            **_current_project_token_context(),
        ),
        "vault_inventory": lambda category=None, access_level=None: tool_vault_inventory(
            category=category,
            access_level=access_level,
            user_id=_current_agent_value("user_id"),
            org_id=_current_agent_value("org_id"),
        ),
        "vault_secret_prompt": (
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
        "runtime_settings": lambda provider=None: tool_runtime_settings(
            provider=provider,
            user_id=getattr(_agent_context, "user_id", None),
            org_id=getattr(_agent_context, "org_id", None),
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
        "read_thread_messages": _handle_read_thread_messages,
        "post_chat_message": lambda **kw: _patched_private(
            "_handle_post_chat_message",
            _handle_post_chat_message,
        )(**kw),
        "post_thread_discussion_reply": lambda **kw: _patched_private(
            "_handle_post_thread_discussion_reply",
            _handle_post_thread_discussion_reply,
        )(**kw),
        "read_thread_discussion": lambda **kw: _patched_private(
            "_handle_read_thread_discussion",
            _handle_read_thread_discussion,
        )(**kw),
        "manage_cycle": lambda **kw: _patched_private("_handle_manage_cycle", _handle_manage_cycle)(**kw),
        "manage_domain": lambda **kw: _patched_private("_handle_manage_domain", _handle_manage_domain)(**kw),
        "manage_inbound": lambda **kw: _patched_private("_handle_manage_inbound", _handle_manage_inbound)(**kw),
        "manage_idea": lambda **kw: _patched_private("_handle_manage_idea", _handle_manage_idea)(**kw),
        "manage_project": lambda **kw: _patched_private("_handle_manage_project", _handle_manage_project)(**kw),
        "manage_skill": lambda **kw: _patched_private("_handle_manage_skill", _handle_manage_skill)(**kw),
        "manage_workspace_app": lambda **kw: _patched_private(
            "_handle_manage_workspace_app",
            _handle_manage_workspace_app,
        )(**kw),
        # Session scratchpad tools
        "session_write": _handle_session_write,
        "session_read": _handle_session_read,
        "session_append": _handle_session_append,
        "session_list": _handle_session_list,
        # Memory lifecycle tools (coordinator only)
        "session_promote": _handle_session_promote,
        "session_close": _handle_session_close,
        "cortex_reply": _handle_cortex_reply,
        "cortex_visual_reply": lambda **kwargs: _handle_cortex_visual_reply(**kwargs),
        "my_activity": _handle_my_activity,
        "browser": lambda **kw: _patched_private("_handle_browser", _handle_browser)(**kw),
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
    handlers.update({
        "exec_command": lambda command, working_dir=None, timeout=60, workspace=None: (
            _handle_exec_command(
                command,
                working_dir=working_dir,
                timeout=timeout,
                _workspace=_select_workspace(workspace, ws, allowed_workspaces),
            )
        ),
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
        "run_script": lambda script, description=None, timeout=60, workspace=None: (
            _handle_run_script(
                script,
                description,
                timeout,
                _workspace=_select_workspace(workspace, ws, allowed_workspaces),
            )
        ),
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
        handlers["test_runner"] = lambda target, pattern=None, verbose=False: (
            extended_handlers["test_runner"](
                target,
                pattern=pattern,
                verbose=verbose,
                workspace_root=_workspace_hint(),
            )
        )
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

        threadlocal_context = vars(_agent_context).copy()
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

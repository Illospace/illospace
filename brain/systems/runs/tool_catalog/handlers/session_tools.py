"""Session Tools orchestration tool handlers."""

from __future__ import annotations

from brain.systems.runs.tool_catalog.handlers.common import *

def _handle_session_write(section: str, value: str, key: str | None = None) -> str:
    """Write a structured entry to the session scratchpad."""
    run_id, err = _require_run_id("session_write")
    if err:
        return err
    if section not in _VALID_SECTIONS:
        return json.dumps({"error": f"Invalid section '{section}'. Must be one of: {sorted(_VALID_SECTIONS)}"})

    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    worker = _get_current_worker_name()
    with UnitOfWork() as uow:
        entry = uow.scratchpad.write(
            run_id=run_id, section=section, value=value,
            key=key, worker_name=worker,
        )
        return json.dumps({"written": True, "id": entry.id, "section": section, "key": key})


def _handle_session_read(section: str | None = None, key: str | None = None) -> str:
    """Read entries from the session scratchpad."""
    run_id, err = _require_run_id("session_read")
    if err:
        return err

    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    with UnitOfWork() as uow:
        entries = uow.scratchpad.read(run_id=run_id, section=section, key=key)
        return json.dumps({"run_id": run_id, "count": len(entries), "entries": entries})


def _handle_read_thread_messages(
    mode: str = "recent",
    start_index: int | None = None,
    end_index: int | None = None,
    query: str | None = None,
    limit: int = 20,
    max_chars: int = 8_000,
) -> str:
    """Read/search raw stored messages for the current persistent agent thread."""
    session_id = getattr(_agent_context, "session_id", None)
    if not session_id:
        return json.dumps({"error": "No active persistent thread session is bound."})

    from brain.systems.sessions import read_thread_messages

    payload = read_thread_messages(
        str(session_id),
        mode=mode,
        start_index=start_index,
        end_index=end_index,
        query=query,
        limit=limit,
        max_chars=max_chars,
    )
    return json.dumps(payload, default=str)


def _handle_session_append(section: str, value: str) -> str:
    """Shorthand for session_write without a key."""
    return _handle_session_write(section=section, value=value, key=None)


def _handle_session_list(section: str | None = None) -> str:
    """List sections or entries in a section."""
    run_id, err = _require_run_id("session_list")
    if err:
        return err

    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    with UnitOfWork() as uow:
        if section:
            entries = uow.scratchpad.read(run_id=run_id, section=section)
            return json.dumps({"run_id": run_id, "section": section, "count": len(entries), "entries": entries})
        sections = uow.scratchpad.list_sections(run_id=run_id)
        return json.dumps({"run_id": run_id, "sections": sections})


# ── Coordinator Tool Handlers ─────────────────────────────────


def _handle_session_promote() -> str:
    """Gather all scratchpad entries for the current run, formatted for review."""
    run_id, err = _require_run_id("session_promote")
    if err:
        return err

    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    with UnitOfWork() as uow:
        return json.dumps(uow.scratchpad.promote(run_id=run_id))


def _handle_session_close() -> str:
    """Mark the session scratchpad as closed for the current run."""
    run_id, err = _require_run_id("session_close")
    if err:
        return err

    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    with UnitOfWork() as uow:
        count = uow.scratchpad.close(run_id=run_id)
        return json.dumps({"closed": True, "run_id": run_id, "entries_closed": count})


# ── Task Pool Handlers (Living Task Graph) ────────────────────

__all__ = [name for name in globals() if not name.startswith("__")]

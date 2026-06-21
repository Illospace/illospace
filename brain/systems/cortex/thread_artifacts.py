"""Thread-scoped interactive artifact publishing."""
from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.idea import Idea
from brain.systems.cortex.thread_links import thread_route_for_id, thread_url_for_route
from brain.systems.workspace_apps.compiler import compile_workspace_app_input
from brain.systems.workspace_apps.contracts import (
    APP_CAPSULE_RENDERER_KEY,
    APP_CAPSULE_SOURCE_KIND,
    APP_KIT_NAME,
    CONTRACT_VERSION,
)
from brain.systems.workspace_apps.service import (
    WorkspaceAppNotFound,
    a_active_version,
    a_create_app,
    a_get_app,
    a_get_or_create_state,
    a_serialize_app,
    a_update_app,
    slugify,
)

THREAD_ARTIFACT_SOURCE = "thread_artifact"
THREAD_COLLABORATION_SOURCE = "system_thread_collaboration"
DEFAULT_THREAD_ARTIFACT_KIND = "interactive"
SYSTEM_COLLABORATION_APP_KEY = "system-team-collaboration-board"
SYSTEM_COLLABORATION_APP_NAME = "Team Collaboration Board"
SYSTEM_COLLABORATION_STATE_PREFIX = "thread-collab"


class ThreadArtifactError(ValueError):
    """Raised when a thread artifact cannot be published."""


def _clean_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _clean_kind(value: Any) -> str:
    text = slugify(str(value or DEFAULT_THREAD_ARTIFACT_KIND))
    return text or DEFAULT_THREAD_ARTIFACT_KIND


def _thread_artifact_key(thread_id: str, title: str, artifact_kind: str) -> str:
    thread_part = slugify(str(thread_id))[:28] or "thread"
    title_part = slugify(title)[:48] or "artifact"
    kind_part = slugify(artifact_kind)[:24] or DEFAULT_THREAD_ARTIFACT_KIND
    return f"thread-{thread_part}-{kind_part}-{title_part}"[:100]


def _thread_collaboration_state_key(thread_id: str, session_key: str | None = None) -> str:
    thread_part = slugify(str(thread_id))[:72] or "thread"
    session_part = slugify(str(session_key or "default"))[:32] or "default"
    return f"{SYSTEM_COLLABORATION_STATE_PREFIX}-{thread_part}-{session_part}"[:120]


def _default_manifest(thread_id: str, artifact_kind: str, manifest: Mapping[str, Any] | None) -> dict[str, Any]:
    next_manifest = dict(manifest or {})
    next_manifest.setdefault("contract_version", CONTRACT_VERSION)
    next_manifest.setdefault("state_key", f"thread-artifact-{thread_id}"[:120])
    next_manifest.setdefault("data_plan", {"mode": "capability", "bindings": {}})
    next_manifest.setdefault(
        "design_contract",
        {"kit": APP_KIT_NAME, "theme_modes": ["dark", "light"]},
    )
    next_manifest["thread_artifact"] = {
        **dict(next_manifest.get("thread_artifact") or {}),
        "thread_id": str(thread_id),
        "kind": artifact_kind,
        "source": THREAD_ARTIFACT_SOURCE,
    }
    return next_manifest


def _default_visual_spec(title: str, artifact_kind: str, visual_spec: Mapping[str, Any] | None) -> dict[str, Any]:
    next_visual = dict(visual_spec or {})
    next_visual.setdefault(
        "thumbnail",
        {
            "label": title,
            "status": "Interactive",
            "secondary": artifact_kind.replace("-", " ").title(),
        },
    )
    return next_visual


def _artifact_metadata(
    *,
    thread_id: str,
    artifact_kind: str,
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    next_metadata = dict(metadata or {})
    next_metadata["source"] = THREAD_ARTIFACT_SOURCE
    next_metadata["artifact_scope"] = "thread"
    next_metadata["thread_id"] = str(thread_id)
    next_metadata["artifact_kind"] = artifact_kind
    next_metadata["thread_artifact"] = {
        **dict(next_metadata.get("thread_artifact") or {}),
        "thread_id": str(thread_id),
        "kind": artifact_kind,
        "source": THREAD_ARTIFACT_SOURCE,
    }
    return next_metadata


def _system_collaboration_metadata(metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    next_metadata = dict(metadata or {})
    next_metadata["source"] = THREAD_COLLABORATION_SOURCE
    next_metadata["system_app"] = True
    next_metadata["artifact_scope"] = "thread"
    next_metadata.setdefault(
        "description",
        "Product-owned reusable collaboration board for thread-scoped votes, notes, and decisions.",
    )
    return next_metadata


def _system_collaboration_manifest() -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "state_key": "default",
        "data_plan": {"mode": "capability", "bindings": {}},
        "collaboration": {
            "mode": "event_sourced",
            "state_key": "default",
            "actions": {
                "vote.cast": {
                    "label": "Cast vote",
                    "description": "Record or update one participant vote.",
                    "reducer": {
                        "type": "choice_by_actor",
                        "state_path": "votes",
                        "value_field": "optionId",
                    },
                },
                "note.add": {
                    "label": "Add note",
                    "description": "Append one participant note.",
                    "reducer": {"type": "append", "state_path": "notes"},
                },
                "status.change": {
                    "label": "Change status",
                    "description": "Set the current collaboration status or decision.",
                    "reducer": {"type": "set", "state_path": "status"},
                },
            },
        },
        "design_contract": {"kit": APP_KIT_NAME, "theme_modes": ["dark", "light"]},
    }


def _system_collaboration_visual_spec() -> dict[str, Any]:
    return {
        "accent": "teal",
        "thumbnail": {
            "label": "Collaboration",
            "value": "Live",
            "secondary": "Votes, notes, decisions",
        },
    }


def _clean_collaboration_options(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    options: list[dict[str, str]] = []
    for index, item in enumerate(value[:12], start=1):
        if isinstance(item, Mapping):
            label = _clean_text(item.get("label") or item.get("title") or item.get("name"))
            option_id = _clean_text(item.get("id") or item.get("key") or label)
            description = _clean_text(item.get("description") or item.get("body") or item.get("text"))
        else:
            label = _clean_text(item)
            option_id = label
            description = None
        if not label:
            continue
        options.append(
            {
                "id": slugify(option_id or label)[:80] or f"option-{index}",
                "label": label[:120],
                "description": (description or "")[:280],
            }
        )
    return options


def _collaboration_initial_data(
    *,
    title: str,
    prompt: str,
    mode: str,
    options: list[dict[str, str]],
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "config": {
            "title": title,
            "prompt": prompt,
            "mode": mode,
            "options": options,
            "allow_notes": True,
            "metadata": dict(metadata or {}),
        },
        "votes": {},
        "notes": [],
        "status": {"phase": "collecting_signal", "decision": None},
    }


SYSTEM_COLLABORATION_APP_SOURCE = r"""
<main class="illo-app">
  <style>
    .illo-app { box-sizing: border-box; min-height: 100%; padding: 28px; background: #f7f4ec; color: #182724; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    .wrap { max-width: 880px; margin: 0 auto; display: grid; gap: 18px; }
    .eyebrow { margin: 0; color: #2d877b; font-size: 13px; font-weight: 800; letter-spacing: 0; text-transform: uppercase; }
    h1 { margin: 4px 0 0; font-size: clamp(30px, 5vw, 46px); line-height: 1.03; letter-spacing: 0; }
    .prompt { margin: 0; color: #61706c; font-size: 18px; line-height: 1.48; }
    .metrics { display: flex; flex-wrap: wrap; gap: 10px; }
    .pill { border: 1px solid #ddd4c7; background: #fffdf8; border-radius: 999px; padding: 9px 13px; color: #66736f; font-size: 14px; white-space: nowrap; }
    .pill strong { color: #1d2d2a; }
    .options, .notes { display: grid; gap: 12px; }
    .option, .note-panel { border: 1px solid #ded6ca; background: #fffdf8; border-radius: 8px; padding: 18px; }
    .option { border-left: 7px solid var(--accent); display: grid; gap: 14px; }
    .option.selected { outline: 3px solid rgba(45,135,123,.24); border-color: #8abdb5; }
    .option-top, .option-meta, .note-form { display: flex; align-items: start; justify-content: space-between; gap: 12px; }
    .option h2, .note-panel h2 { margin: 0; font-size: 20px; line-height: 1.15; letter-spacing: 0; }
    .option p { margin: 7px 0 0; color: #65736f; line-height: 1.42; }
    .bar { height: 12px; border-radius: 999px; background: #e5e4df; overflow: hidden; }
    .fill { height: 100%; width: var(--pct); background: var(--accent); border-radius: inherit; transition: width .18s ease; }
    button { border: 0; border-radius: 7px; background: #162724; color: white; cursor: pointer; font-size: 15px; font-weight: 800; min-width: 78px; padding: 11px 16px; }
    button:hover { background: #24403b; }
    button:disabled { cursor: wait; opacity: .58; }
    textarea { min-height: 62px; resize: vertical; border: 1px solid #d6cec0; border-radius: 7px; padding: 12px; font: inherit; background: white; color: #182724; flex: 1; }
    ul { display: grid; gap: 8px; list-style: none; margin: 0; padding: 0; }
    li { border: 1px solid #ebe4d8; border-radius: 7px; padding: 10px 12px; color: #43504d; background: #fff; }
    .muted { color: #7a8581; }
    @media (max-width: 620px) { .illo-app { padding: 20px; } .option-top, .option-meta, .note-form { display: grid; } button { width: 100%; } }
  </style>
  <section class="wrap">
    <header>
      <p class="eyebrow" id="modeLabel">Team collaboration</p>
      <h1 id="title">Team Collaboration Board</h1>
      <p class="prompt" id="prompt">Loading...</p>
    </header>
    <div class="metrics" id="metrics"></div>
    <section class="options" id="options"></section>
    <section class="note-panel">
      <h2>Notes</h2>
      <div class="note-form">
        <textarea id="noteText" placeholder="Add a note, concern, or rationale..."></textarea>
        <button id="noteButton" type="button">Add note</button>
      </div>
      <ul id="notes"></ul>
    </section>
  </section>
  <script>
    const FALLBACK_OPTIONS = [
      { id: 'option-a', label: 'Option A', description: '' },
      { id: 'option-b', label: 'Option B', description: '' }
    ];
    const ACCENTS = ['#68aaa0', '#ee8c70', '#8a92e6', '#d1a64d', '#5c9ed6'];
    let snapshot = null;
    let busy = false;
    let runtimeStatus = 'loading';

    function object(value) {
      return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
    }
    function dataFrom(value) {
      const snap = object(value);
      const state = object(snap.state);
      if (state.data && typeof state.data === 'object') return state.data;
      return snap;
    }
    function config() {
      return object(dataFrom(snapshot).config);
    }
    function options() {
      const items = Array.isArray(config().options) ? config().options : [];
      return items.length ? items : FALLBACK_OPTIONS;
    }
    function votes() {
      const raw = object(dataFrom(snapshot).votes);
      return Object.values(raw).map((entry) => {
        if (typeof entry === 'string') return entry;
        const item = object(entry);
        return item.value || object(item.payload).optionId || null;
      }).filter(Boolean);
    }
    function notes() {
      const raw = dataFrom(snapshot).notes;
      return (Array.isArray(raw) ? raw : []).map((note) => {
        if (typeof note === 'string') return note;
        return object(note).body || object(note).text || '';
      }).filter(Boolean);
    }
    function stateVersion() {
      return Number(object(object(snapshot).state).version || 0);
    }
    function escapeHtml(value) {
      return String(value == null ? '' : value).replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[char]));
    }
    function render() {
      const cfg = config();
      const allVotes = votes();
      const total = allVotes.length;
      document.getElementById('modeLabel').textContent = `${cfg.mode || 'team'} collaboration`;
      document.getElementById('title').textContent = cfg.title || 'Team Collaboration Board';
      document.getElementById('prompt').textContent = cfg.prompt || 'Collect signal from the team.';
      document.getElementById('metrics').innerHTML = [
        `<span class="pill">Runtime: <strong>${escapeHtml(runtimeStatus)}</strong></span>`,
        `<span class="pill">Participants: <strong>${total}</strong></span>`,
        `<span class="pill">Notes: <strong>${notes().length}</strong></span>`,
        `<span class="pill">State version: <strong>${stateVersion()}</strong></span>`
      ].join('');
      document.getElementById('options').innerHTML = options().map((option, index) => {
        const count = allVotes.filter((vote) => vote === option.id).length;
        const pct = total ? Math.round((count / total) * 100) : 0;
        return `<article class="option" style="--accent:${ACCENTS[index % ACCENTS.length]}">
          <div class="option-top">
            <div><h2>${escapeHtml(option.label)}</h2><p>${escapeHtml(option.description || '')}</p></div>
            <button type="button" data-vote="${escapeHtml(option.id)}" ${busy ? 'disabled' : ''}>Vote</button>
          </div>
          <div class="bar"><div class="fill" style="--pct:${pct}%"></div></div>
          <div class="option-meta"><span>${count} vote${count === 1 ? '' : 's'} / ${pct}%</span></div>
        </article>`;
      }).join('');
      document.getElementById('notes').innerHTML = notes().length
        ? notes().slice(-8).reverse().map((note) => `<li>${escapeHtml(note)}</li>`).join('')
        : '<li class="muted">No notes yet.</li>';
      document.querySelectorAll('[data-vote]').forEach((button) => {
        button.addEventListener('click', () => castVote(button.getAttribute('data-vote')));
      });
    }
    async function refresh() {
      try {
        snapshot = await window.illo.collab.get({ limit: 100 });
        runtimeStatus = 'ready';
      } catch (error) {
        runtimeStatus = error && error.message ? error.message : 'refresh failed';
      }
      render();
    }
    async function castVote(optionId) {
      if (!optionId || busy) return;
      busy = true;
      runtimeStatus = 'saving vote';
      render();
      try {
        snapshot = await window.illo.collab.event('vote.cast', { optionId, at: new Date().toISOString() });
        runtimeStatus = 'vote saved';
      } catch (error) {
        runtimeStatus = error && error.message ? error.message : 'vote failed';
      } finally {
        busy = false;
        render();
      }
    }
    async function addNote() {
      const input = document.getElementById('noteText');
      const body = input.value.trim();
      if (!body || busy) return;
      busy = true;
      runtimeStatus = 'saving note';
      render();
      try {
        snapshot = await window.illo.collab.event('note.add', { body, at: new Date().toISOString() });
        input.value = '';
        runtimeStatus = 'note saved';
      } catch (error) {
        runtimeStatus = error && error.message ? error.message : 'note failed';
      } finally {
        busy = false;
        render();
      }
    }
    document.getElementById('noteButton').addEventListener('click', addNote);
    window.addEventListener('illo:collab', (event) => {
      snapshot = event.detail;
      runtimeStatus = 'synced';
      render();
    });
    if (window.illo && window.illo.collab) {
      window.illo.collab.subscribe((nextSnapshot) => {
        snapshot = nextSnapshot;
        runtimeStatus = 'synced';
        render();
      }, { intervalMs: 3000 });
      refresh();
    } else {
      runtimeStatus = 'bridge unavailable';
      render();
    }
  </script>
</main>
""".strip()


async def _require_thread(session: AsyncSession, *, org_id: str, thread_id: str) -> None:
    idea_id = await session.scalar(
        select(Idea.id).where(
            Idea.id == str(thread_id),
            Idea.org_id == str(org_id),
            Idea.archived_at.is_(None),
        )
    )
    if idea_id is None:
        raise ThreadArtifactError("Thread not found for this workspace")


async def _ensure_system_collaboration_app(
    session: AsyncSession,
    *,
    org_id: str,
    user_id: str | None,
) -> tuple[str, dict[str, Any], str]:
    manifest = _system_collaboration_manifest()
    visual_spec = _system_collaboration_visual_spec()
    metadata = _system_collaboration_metadata()
    action = "created"

    try:
        app = await a_get_app(session, org_id, None, key=SYSTEM_COLLABORATION_APP_KEY)
    except WorkspaceAppNotFound:
        app = await a_create_app(
            session,
            org_id=org_id,
            key=SYSTEM_COLLABORATION_APP_KEY,
            name=SYSTEM_COLLABORATION_APP_NAME,
            description="Reusable system collaboration board for thread-scoped team input.",
            renderer_key=APP_CAPSULE_RENDERER_KEY,
            source_kind=APP_CAPSULE_SOURCE_KIND,
            source_code=SYSTEM_COLLABORATION_APP_SOURCE,
            manifest=manifest,
            visual_spec=visual_spec,
            metadata=metadata,
            created_by_user_id=user_id,
            anchor_user_id=user_id,
        )
    else:
        action = "reused"
        version = await a_active_version(session, app.id)
        if (
            version is None
            or version.source_code != SYSTEM_COLLABORATION_APP_SOURCE
            or version.manifest != manifest
            or app.visual_spec != visual_spec
        ):
            app = await a_update_app(
                session,
                org_id=org_id,
                app_id=str(app.id),
                name=SYSTEM_COLLABORATION_APP_NAME,
                description="Reusable system collaboration board for thread-scoped team input.",
                renderer_key=APP_CAPSULE_RENDERER_KEY,
                source_kind=APP_CAPSULE_SOURCE_KIND,
                source_code=SYSTEM_COLLABORATION_APP_SOURCE,
                manifest=manifest,
                visual_spec=visual_spec,
                metadata=metadata,
                anchor_user_id=user_id,
                updated_by_user_id=user_id,
            )
            action = "updated"

    return action, await a_serialize_app(session, app), str(app.id)


async def publish_thread_collaboration_app(
    session: AsyncSession,
    *,
    org_id: str,
    user_id: str | None,
    thread_id: str,
    title: str,
    prompt: str,
    mode: str | None = None,
    options: list[Any] | None = None,
    session_key: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Start or update a thread-scoped session on the reusable collaboration board."""

    clean_thread_id = _clean_text(thread_id)
    clean_title = _clean_text(title)
    clean_prompt = _clean_text(prompt)
    if not clean_thread_id:
        raise ThreadArtifactError("thread_id is required")
    if not clean_title:
        raise ThreadArtifactError("title is required")
    if not clean_prompt:
        raise ThreadArtifactError("prompt is required")

    await _require_thread(session, org_id=org_id, thread_id=clean_thread_id)
    action, app, app_id = await _ensure_system_collaboration_app(
        session,
        org_id=org_id,
        user_id=user_id,
    )
    state_key = _thread_collaboration_state_key(clean_thread_id, session_key)
    collaboration_options = _clean_collaboration_options(options or [])
    state = await a_get_or_create_state(
        session,
        org_id=org_id,
        app_id=app_id,
        key=state_key,
        user_id=user_id,
    )
    current_data = dict(state.data or {})
    next_data = {
        **_collaboration_initial_data(
            title=clean_title,
            prompt=clean_prompt,
            mode=slugify(mode or "decision") or "decision",
            options=collaboration_options,
            metadata={
                **dict(metadata or {}),
                "thread_id": clean_thread_id,
                "session_key": state_key,
            },
        ),
        **current_data,
        "config": _collaboration_initial_data(
            title=clean_title,
            prompt=clean_prompt,
            mode=slugify(mode or "decision") or "decision",
            options=collaboration_options,
            metadata={
                **dict(metadata or {}),
                "thread_id": clean_thread_id,
                "session_key": state_key,
            },
        )["config"],
    }
    state.data = next_data
    state.version = int(state.version or 0) + 1
    state.updated_by_user_id = user_id
    await session.flush()

    route = thread_route_for_id(clean_thread_id)
    artifact_route = f"{route}?{urlencode({'app': app_id, 'state_key': state_key})}"
    return {
        "action": action,
        "thread_id": clean_thread_id,
        "thread_route": route,
        "thread_url": thread_url_for_route(route),
        "artifact_route": artifact_route,
        "artifact_url": thread_url_for_route(artifact_route),
        "artifact_kind": "collaboration",
        "app_id": app_id,
        "app_key": app["key"],
        "app_name": app["name"],
        "state_key": state_key,
        "state": {
            "key": state.key,
            "version": int(state.version or 0),
            "data": state.data or {},
        },
        "app": app,
    }


async def publish_thread_artifact_app(
    session: AsyncSession,
    *,
    org_id: str,
    user_id: str | None,
    thread_id: str,
    title: str,
    source_code: str,
    description: str | None = None,
    artifact_kind: str | None = None,
    key: str | None = None,
    app_id: str | None = None,
    update_existing: bool = True,
    manifest: Mapping[str, Any] | None = None,
    visual_spec: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    initial_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or update a thread-scoped app-capsule artifact."""

    clean_thread_id = _clean_text(thread_id)
    clean_title = _clean_text(title)
    clean_source = str(source_code or "").strip()
    if not clean_thread_id:
        raise ThreadArtifactError("thread_id is required")
    if not clean_title:
        raise ThreadArtifactError("title is required")
    if not clean_source:
        raise ThreadArtifactError("source_code is required")

    await _require_thread(session, org_id=org_id, thread_id=clean_thread_id)
    clean_kind = _clean_kind(artifact_kind)
    app_key = _clean_text(key) or _thread_artifact_key(clean_thread_id, clean_title, clean_kind)
    route = thread_route_for_id(clean_thread_id)
    artifact_route = f"{route}?app={app_id or app_key}"
    thread_url = thread_url_for_route(route)

    compiled = compile_workspace_app_input(
        action="create",
        name=clean_title,
        key=app_key,
        renderer_key=APP_CAPSULE_RENDERER_KEY,
        source_kind=APP_CAPSULE_SOURCE_KIND,
        source_code=clean_source,
        manifest=_default_manifest(clean_thread_id, clean_kind, manifest),
        visual_spec=_default_visual_spec(clean_title, clean_kind, visual_spec),
        metadata=_artifact_metadata(thread_id=clean_thread_id, artifact_kind=clean_kind, metadata=metadata),
        initial_state=dict(initial_state or {}) or None,
    )

    action = "created"
    target_app_id = _clean_text(app_id)
    target_key = app_key
    if update_existing:
        try:
            existing = await a_get_app(session, org_id, target_app_id, key=None if target_app_id else target_key)
        except WorkspaceAppNotFound:
            existing = None
        if existing is not None:
            target_app_id = str(existing.id)

    if target_app_id:
        app = await a_update_app(
            session,
            org_id=org_id,
            app_id=target_app_id,
            name=clean_title,
            description=description,
            renderer_key=compiled.renderer_key,
            source_kind=compiled.source_kind,
            source_code=compiled.source_code,
            manifest=compiled.manifest,
            visual_spec=compiled.visual_spec,
            metadata=compiled.metadata,
            anchor_user_id=user_id,
            updated_by_user_id=user_id,
        )
        action = "updated"
    else:
        app = await a_create_app(
            session,
            org_id=org_id,
            key=target_key,
            name=clean_title,
            description=description,
            renderer_key=compiled.renderer_key,
            source_kind=compiled.source_kind,
            source_code=compiled.source_code,
            manifest=compiled.manifest or {},
            visual_spec=compiled.visual_spec or {},
            metadata=compiled.metadata or {},
            created_by_user_id=user_id,
            anchor_user_id=user_id,
            initial_state=dict(initial_state or {}) or None,
            state_key=str((compiled.manifest or {}).get("state_key") or "default"),
        )

    serialized = await a_serialize_app(session, app)
    artifact_route = f"{route}?app={serialized['id']}"
    return {
        "action": action,
        "thread_id": clean_thread_id,
        "thread_route": route,
        "thread_url": thread_url,
        "artifact_route": artifact_route,
        "artifact_url": thread_url_for_route(artifact_route),
        "artifact_kind": clean_kind,
        "app_id": serialized["id"],
        "app_key": serialized["key"],
        "app_name": serialized["name"],
        "version": (serialized.get("active_version") or {}).get("version"),
        "renderer_key": serialized["renderer_key"],
        "source_kind": (serialized.get("active_version") or {}).get("source_kind"),
        "app": serialized,
        "compiler_repairs": list(compiled.repairs),
        "warnings": list(compiled.warnings),
    }


__all__ = [
    "DEFAULT_THREAD_ARTIFACT_KIND",
    "SYSTEM_COLLABORATION_APP_KEY",
    "THREAD_ARTIFACT_SOURCE",
    "THREAD_COLLABORATION_SOURCE",
    "ThreadArtifactError",
    "publish_thread_collaboration_app",
    "publish_thread_artifact_app",
]

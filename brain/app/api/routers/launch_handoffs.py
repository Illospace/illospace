"""Launch handoff API and target routes."""

from __future__ import annotations

from html import escape

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import HTMLResponse, RedirectResponse, Response

from brain.app.api.auth import get_current_user
from brain.app.api.authorization import require_org_context
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.schemas.launch_handoffs import LaunchHandoffCreateRequest
from brain.systems import launch_handoffs


router = APIRouter(tags=["launch-handoffs"], dependencies=[Depends(rate_limit)])


def _user_id(user: dict | None) -> str | None:
    return str(user.get("id")) if user and user.get("id") else None


async def _require_handoff_for_api(
    db: AsyncSession,
    handoff_id: str,
    *,
    org_id: str,
) -> launch_handoffs.LaunchHandoff:
    try:
        return await launch_handoffs.require_launch_handoff(db, handoff_id, org_id=org_id)
    except launch_handoffs.LaunchHandoffNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _launch_page(row: launch_handoffs.LaunchHandoff) -> HTMLResponse:
    prompt = escape(launch_handoffs.claude_prompt_for_handoff(row))
    title = escape(str(row.title or "Illo launch handoff").strip())
    repo_origin_url = escape(str(row.repo_origin_url or "Not provided").strip())
    branch_hint = escape(str(row.branch_hint or "Not provided").strip())
    launched_url = escape(f"/api/launch-handoffs/{row.id}/launched", quote=True)
    codex_url = escape(
        launch_handoffs.launch_handoff_route_for_id(
            row.id,
            target_tool=launch_handoffs.TARGET_CODEX,
        ),
        quote=True,
    )
    content = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Launch handoff · Illo</title>
    <style>
      :root {{
        color-scheme: dark;
        --page: #050915;
        --surface: #0b111d;
        --surface-raised: #101826;
        --border: #263143;
        --text: #f0f0fa;
        --muted: #9aa7ba;
        --accent: #8db7ff;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        min-height: 100vh;
        background: var(--page);
        color: var(--text);
        font: 15px/1.6 Inter, ui-sans-serif, system-ui, -apple-system, sans-serif;
      }}
      main {{ width: min(720px, calc(100% - 32px)); margin: 0 auto; padding: 64px 0; }}
      .eyebrow {{
        margin: 0 0 8px;
        color: var(--accent);
        font: 600 11px/1.3 ui-monospace, SFMono-Regular, Menlo, monospace;
        letter-spacing: .08em;
        text-transform: uppercase;
      }}
      h1 {{ margin: 0; font-size: clamp(26px, 5vw, 36px); line-height: 1.15; font-weight: 600; }}
      .lede {{ margin: 12px 0 28px; color: var(--muted); }}
      section {{ padding: 20px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface); }}
      label {{ display: block; margin-bottom: 8px; font-weight: 600; }}
      textarea {{
        width: 100%;
        min-height: 180px;
        resize: vertical;
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 14px;
        background: var(--surface-raised);
        color: var(--text);
        font: 13px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace;
      }}
      textarea:focus, a:focus-visible, button:focus-visible {{
        outline: 2px solid var(--accent);
        outline-offset: 2px;
      }}
      .actions {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }}
      button, .button {{
        min-height: 42px;
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 10px 15px;
        background: var(--surface-raised);
        color: var(--text);
        font: 600 12px/1.3 ui-monospace, SFMono-Regular, Menlo, monospace;
        text-decoration: none;
        cursor: pointer;
      }}
      button {{ border-color: var(--text); background: var(--text); color: var(--page); }}
      button:disabled {{ opacity: .65; cursor: wait; }}
      #copy-status {{ min-height: 24px; margin: 10px 0 0; color: var(--muted); font-size: 13px; }}
      dl {{ display: grid; grid-template-columns: 110px minmax(0, 1fr); gap: 8px 16px; margin: 22px 0 0; }}
      dt {{ color: var(--muted); }}
      dd {{
        margin: 0;
        overflow-wrap: anywhere;
        font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
      }}
      @media (max-width: 520px) {{
        main {{ padding: 36px 0; }}
        section {{ padding: 16px; }}
        dl {{ grid-template-columns: 1fr; gap: 2px; }}
        dd + dt {{ margin-top: 8px; }}
      }}
    </style>
  </head>
  <body>
    <main>
      <p class="eyebrow">Illo launch handoff</p>
      <h1>{title}</h1>
      <p class="lede">
        Copy the starter prompt into Claude Code. The session will fetch the full,
        org-scoped context from Illo.
      </p>
      <section aria-labelledby="prompt-label">
        <label id="prompt-label" for="starter-prompt">Starter prompt</label>
        <textarea id="starter-prompt" readonly>{prompt}</textarea>
        <div class="actions">
          <button id="copy-prompt" type="button" data-launched-url="{launched_url}">Copy for Claude Code</button>
          <a class="button" href="{codex_url}">Open in Codex</a>
        </div>
        <p id="copy-status" role="status" aria-live="polite"></p>
        <dl>
          <dt>Repository</dt><dd>{repo_origin_url}</dd>
          <dt>Branch hint</dt><dd>{branch_hint}</dd>
        </dl>
      </section>
    </main>
    <script>
      const button = document.getElementById('copy-prompt');
      const prompt = document.getElementById('starter-prompt');
      const status = document.getElementById('copy-status');

      async function copyPrompt() {{
        let copied = false;
        button.disabled = true;
        try {{
          if (navigator.clipboard && window.isSecureContext) {{
            await navigator.clipboard.writeText(prompt.value);
          }} else {{
            prompt.focus();
            prompt.select();
            if (!document.execCommand('copy')) throw new Error('Copy unavailable');
          }}
          copied = true;
          const response = await fetch(button.dataset.launchedUrl, {{
            method: 'POST',
            credentials: 'same-origin',
            headers: {{ Accept: 'application/json' }},
          }});
          if (!response.ok) throw new Error('Launch record failed');
          status.textContent = 'Copied. Launch recorded by Illo.';
        }} catch (error) {{
          status.textContent = copied
            ? 'Prompt copied, but Illo could not record the launch.'
            : 'Copy failed. Select the prompt and copy it manually.';
        }} finally {{
          button.disabled = false;
        }}
      }}

      button.addEventListener('click', copyPrompt);
    </script>
  </body>
</html>"""
    return HTMLResponse(
        content,
        headers={
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                "connect-src 'self'; img-src 'none'; object-src 'none'; base-uri 'none'; "
                "form-action 'none'; frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/api/launch-handoffs", status_code=201)
async def create_launch_handoff(
    payload: LaunchHandoffCreateRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> dict:
    org_id = require_org_context(user)
    try:
        row = await launch_handoffs.create_launch_handoff(
            db,
            launch_handoffs.LaunchHandoffCreateInput(
                org_id=org_id,
                created_by_user_id=_user_id(user),
                title=payload.title,
                instructions=payload.instructions,
                target_tool=payload.target_tool,
                summary=payload.summary,
                source_surface=payload.source_surface,
                source_ref=payload.source_ref,
                context_parts=payload.context_parts,
                acceptance_criteria=payload.acceptance_criteria,
                repo_origin_url=payload.repo_origin_url,
                branch_hint=payload.branch_hint,
                idempotency_key=payload.idempotency_key,
                metadata=payload.metadata,
            ),
        )
    except launch_handoffs.LaunchHandoffError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"handoff": launch_handoffs.serialize_launch_handoff(row)}


@router.get("/api/launch-handoffs/{handoff_id}")
async def get_launch_handoff(
    handoff_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> dict:
    org_id = require_org_context(user)
    row = await _require_handoff_for_api(db, handoff_id, org_id=org_id)
    return {"handoff": launch_handoffs.serialize_launch_handoff(row)}


async def _launch_handoff_target(
    handoff_id: str,
    *,
    target: str | None,
    db: AsyncSession,
    user: dict,
) -> Response:
    org_id = require_org_context(user)
    row = await _require_handoff_for_api(db, handoff_id, org_id=org_id)

    target_tool = str(target or row.target_tool or launch_handoffs.TARGET_CODEX).strip().lower()
    if target_tool == launch_handoffs.TARGET_CODEX:
        await launch_handoffs.mark_launch_handoff_launched(
            db,
            row,
            launched_by_user_id=_user_id(user),
        )
        return RedirectResponse(launch_handoffs.codex_deep_link_for_handoff(row), status_code=302)
    return _launch_page(row)


@router.get("/api/launch-handoffs/{handoff_id}/launch")
async def redirect_api_launch_handoff(
    handoff_id: str,
    target: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> Response:
    return await _launch_handoff_target(handoff_id, target=target, db=db, user=user)


@router.post("/api/launch-handoffs/{handoff_id}/launched")
async def mark_api_launch_handoff_launched(
    handoff_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> dict:
    org_id = require_org_context(user)
    row = await _require_handoff_for_api(db, handoff_id, org_id=org_id)
    await launch_handoffs.mark_launch_handoff_launched(
        db,
        row,
        launched_by_user_id=_user_id(user),
    )
    return {"launched": True, "launch_count": int(row.launch_count or 0)}

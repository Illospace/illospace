# Slice 04 — Claude launch target (launch page; codex redirect preserved)

## Contract unlocked
A handoff is launchable into **any of the team's harnesses**, not just
Codex. Reda's daily driver is Claude Code; Axel's is Codex. `target_tool`
becomes real choice instead of a hardcoded enum of one.

## API seam
- `brain/systems/launch_handoffs.py`: add `TARGET_CLAUDE = "claude"`;
  `claude_prompt_for_handoff(row)` mirrors the codex starter prompt (same
  `illo_read` `handoff.get` fetch instruction — the Illo MCP is already in
  the team's Claude sessions).
- Launch route behavior (`brain/app/api/routers/launch_handoffs.py`):
  - `target=codex` → unchanged `codex://threads/new?...` redirect.
  - `target=claude` (and fallback for unknown targets) → render a minimal
    **launch page**: the starter prompt in a copy box (one-click copy), the
    repo/branch hints, and both target buttons. Claude Code has no
    registered URL scheme, so the copy-page IS the launch for it; it also
    covers phones and teammates without the scheme handler installed.
- Tool schema (`definitions/cortex_thread.py`): `target_tool` enum
  `["codex", "claude"]`; default stays `codex` only if the assignee is
  unknown — slice 05 passes the owner's preferred target from a small
  per-member map (env `ILLO_MEMBER_AGENT_TARGETS`, e.g.
  `reda=claude,axel=codex,jb=claude`).
- Frontend: `ThreadLinkPreviewCard` shows the target label; no layout
  change.

## What the human can run/see
From Slack: click a claude-target launch link → launch page renders →
copy → paste into a Claude Code session → the session fetches
`handoff.get` and has full context. Codex path re-verified unchanged.

## Verification
- Route tests: redirect for codex; HTML page (200, contains prompt, no
  secrets) for claude/unknown; org scoping intact; `mark_launched`
  increments for both paths.
- Manual click-through both targets from a real Slack message (paste
  evidence links in this file's PR).
- Launch page is a visual shot → run an unprimed `screenshot-critique` pass
  as the last check before accepting it (explicit step, not optional). It is
  a NEW page (no prior look), so no compare-screenshots target; keep it to
  Constellation tokens per DESIGN.md (page-layout change category, local).

## Stays green
Existing handoff API tests; codex deep-link snapshot unchanged.

## Feedback that would change this slice
A `claude://`-style scheme becomes available, or Reda prefers
`claude -p '<prompt>'` shell one-liner on the page instead of a paste
prompt (cheap to add both).

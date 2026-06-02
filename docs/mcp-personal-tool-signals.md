# MCP Personal Tool Coordination

Status: current external coordination guidance for personal-agent MCP clients
Parent PRD: `docs/prd-inbound-coordination-layer.md`
Related PRD: `docs/prd-universal-thread-context-ingress.md`

## Purpose

Personal tools such as Codex, Claude Code, OpenCode, Hermes, OpenClaw, and
local scripts should give Illo context and intent. They should not decide which
Thread, project, pin, teammate, Domain, or notification should receive the
work.

The product boundary is:

> External tools submit context and intent. Illospace owns coordination. Illo
> handles ambiguity. Deterministic policy handles the repeated and obvious.

Direct Thread or workspace mutation tools are internal Illo tools or explicit
compatibility surfaces. They are not the default surface for hooks or autonomous
personal agents.

## Canonical Tools

The hosted MCP surface for personal agents is intentionally small:

- `illo_submit`: send new context, traces, artifacts, progress, or handoff
  material into Illo. The tool acknowledges quickly, stores the inbound event,
  and returns any immediately available Thread URL or result handle.
- `illo_read`: ask for information Illo already has or can reason over without
  creating team-visible work by default.
- `illo_act`: ask Illo to coordinate or take an explicit action. The caller
  supplies intent and relevant context; Illo chooses the workspace tools and
  surfaces.
- `illo_get_result`: retrieve the status and final result for asynchronous
  submit, read, or act calls.

## Submit Envelope

`illo_submit` builds a shared inbound envelope. Callers should prefer generic
context fields over workflow-specific target fields:

```json
{
  "intent": "Share this Codex session with the team for review.",
  "parts": [
    {
      "type": "text",
      "label": "summary",
      "text": "Implemented the MCP coordination docs and renamed the public tool contract."
    },
    {
      "type": "diff",
      "label": "changed files",
      "text": "docs/prd-universal-thread-context-ingress.md\ndocs/mcp-personal-tool-signals.md"
    }
  ],
  "source": {
    "tool": "codex",
    "repo": "illospace-mcp-analysis",
    "branch": "codex/mcp-tool-docs",
    "session_id": "optional-external-session-id"
  },
  "constraints": {
    "visibility": "team",
    "urgency": "normal"
  },
  "correlation": {
    "thread_url": "https://illo.example.com/cortex?idea=existing-thread"
  },
  "idempotency_key": "codex:mcp-tool-docs:2026-06-01T15:00Z"
}
```

Do not send top-level `idea_id`, `thread_id`, `project_id`, `pin_id`,
`teammate_user_ids`, or `trigger_illo`. If the source has a likely existing
Thread, put it in `correlation`. If the user wants Illo to do something, use
`illo_act` with explicit intent instead of smuggling routing commands into
`illo_submit`.

## Hook Guidance

Automatic hooks should call `illo_submit` only when meaningful progress has
happened, such as:

- implementation completed or materially advanced;
- tests were added, fixed, or failed with useful diagnostics;
- a blocker was discovered;
- a handoff summary is needed before stopping work.

Hooks should avoid sending noise for every command, file read, or tiny edit.

## Shared-Service Test Boundary

MCP route unit tests may mock `submit_inbound_envelope` at the adapter boundary.
Combined integration coverage must prove that:

- `illo_submit` persists through the real shared inbound service;
- MCP and webhook events create the same inbound event/receipt record types;
- source actor and authority principal are preserved in provenance;
- replay works for MCP-created inbound events;
- `illo_read`, `illo_act`, and `illo_get_result` preserve the same auth,
  authority, and async-result boundaries as submit.

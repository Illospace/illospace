# MCP Personal Tool Signals

Status: implementation note for Reda's MCP lane  
Parent PRD: `docs/prd-inbound-coordination-layer.md`

## Purpose

Personal tools such as Codex, Claude Code, OpenCode, and local scripts should
submit work progress to IloSpace as signals. They should not decide which Ilo
thread, project, pin, or teammate should receive the update.

The default hosted MCP tool is:

```text
illo_submit_signal
```

Direct thread tools remain compatibility tools for explicit user-directed
publishing only.

## Envelope

`illo_submit_signal` builds this shared inbound envelope:

```json
{
  "kind": "signal",
  "origin": "codex.progress",
  "payload": {},
  "summary": "Implemented the MCP submit-signal tool and tests.",
  "hints": {
    "source_tool": "codex",
    "repo": "illospace-project",
    "branch": "codex/mcp-submit-signal",
    "task_title": "MCP personal-tool signal lane",
    "files_touched": [
      "brain/app/api/routers/agent_mcp.py",
      "tests/test_external_agent_routes.py"
    ]
  },
  "desired_outcome": "team_update",
  "idempotency_key": "codex:mcp-submit-signal:2026-05-18T18:30Z"
}
```

## Ingress Context

The MCP router also passes:

- source actor: the external source connection and personal tool identity;
- authority principal: the user who owns/configured the connection;
- auth context: token id and scopes;
- metadata: MCP tool name and caller metadata.

The required scope is:

```text
signal:submit
```

## Hook Guidance

Automatic hooks should call `illo_submit_signal` only when meaningful progress
has happened, such as:

- implementation completed or materially advanced;
- tests were added, fixed, or failed with useful diagnostics;
- a blocker was discovered;
- a handoff summary is needed before stopping work.

Hooks should avoid sending noise for every command, file read, or tiny edit.

## Example Arguments

```json
{
  "summary": "Added the hosted MCP submit-signal tool and mocked foundation tests.",
  "origin": "codex.progress",
  "source_tool": "codex",
  "repo": "illospace-project",
  "branch": "codex/mcp-submit-signal",
  "task_title": "MCP personal-tool signal lane",
  "files_touched": [
    "brain/app/api/routers/agent_mcp.py",
    "tests/test_external_agent_routes.py"
  ],
  "desired_outcome": "team_update",
  "idempotency_key": "codex:mcp-submit-signal:route-tests",
  "metadata": {
    "hook": "post-message"
  }
}
```

Do not send top-level `idea_id`, `thread_id`, `project_id`, `pin_id`,
`teammate_user_ids`, or `trigger_illo`. If the source has context that may help
IloSpace route the signal, put it in `hints` instead.

## Post-Merge Test Boundary

Until JB's foundation branch lands, MCP tests mock `submit_inbound_envelope`.
After both PRs merge, add integration coverage proving that:

- `illo_submit_signal` persists through the real shared service;
- MCP and webhook events create the same inbound event/receipt record types;
- source actor and authority principal are preserved in provenance;
- replay works for MCP-created inbound events.

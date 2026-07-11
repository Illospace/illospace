# Slice 02 — Packet composer (dual-audience render + idempotency)

## Contract unlocked
One `Dossier` → one **packet**: (a) a 5-line human brief in Slack mrkdwn,
(b) a ready `LaunchHandoffCreateInput` for the assignee's agent. Pure.

## API seam
`brain/systems/briefing/compose.py`:

```python
@dataclass(frozen=True)
class PacketRender:
    human_brief: str                 # Slack mrkdwn, ≤ ~1200 chars, ends with launch link placeholder "{launch_url}"
    handoff_input: LaunchHandoffCreateInput   # from brain/systems/launch_handoffs
    idempotency_key: str             # f"{job_ref}:{revision}" — supersede, don't duplicate
    revision: str                    # content hash of the dossier (stable for unchanged truth)

def compose_packet(dossier: Dossier, *, owner_label: str | None,
                   ask: str, target_tool: str, repo_origin_url: str | None,
                   branch_hint: str | None) -> PacketRender: ...
```

Human brief shape (fixed template, no model call):
```
*<headline>* → <owner_label or "unclaimed">
*What happened:* <1-2 lines from top sections>
*Evidence:* <n links, comma-separated refs>
*Prior decisions:* <line or "none on record">
*Ask:* <ask>   ·   <omissions note if any>
Launch: {launch_url}
```

Agent side: `handoff_input.instructions` = the ask + pointers; dossier
sections map to `context_parts` (ordered dicts: `{source, ref, title,
excerpt, omitted_count}`) so `handoff.get` returns the full assembly;
`acceptance_criteria` passed through from the caller (triage supplies them
in slice 05). Omission markers MUST appear in both renders.

## What the human can run/see
Extend the slice-01 CLI probe: `--compose --ask "fix the melted hands batch"`
prints the human brief and the full handoff payload JSON side by side.

## Verification
- Golden tests against the slice-01 frozen dossier fixture: exact
  `human_brief` snapshot + exact `context_parts` snapshot.
- Property tests: brief length cap enforced by tightening excerpts (with
  markers), never by dropping the omissions note or the launch link;
  idempotency_key stable for identical dossiers, changed for changed truth.
- No model involvement — composer is deterministic (briefs must be trusted;
  determinism is the trust floor; model-polished phrasing is a later,
  eval-gated idea).

## Stays green
Fast suite; `launch_handoffs.py` untouched (composer only *builds* its input).

## Feedback that would change this slice
Reda wants a different brief template, per-member tone, or model-written
briefs in v1.

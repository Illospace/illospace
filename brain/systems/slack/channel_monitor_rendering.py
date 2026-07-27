"""Run rendering policy for ordinary monitored Slack channel messages."""

from __future__ import annotations

from typing import Any, Mapping


def slack_channel_monitor_message(
    payload: Mapping[str, Any],
    slack_trigger_payload: Mapping[str, Any],
) -> str:
    """Frame a monitored-channel message as a passive triage decision."""

    text = _clean(payload.get("text"))[:2000]
    channel_id = _clean(slack_trigger_payload.get("channel_id"))
    channel_name = _clean(payload.get("channel_name"))
    channel_label = f"#{channel_name}" if channel_name else f"channel {channel_id}"
    author = _clean(slack_trigger_payload.get("slack_user_id")) or "unknown (may be an app/bot)"
    lines = [
        f"You are passively monitoring Slack {channel_label}. A new message was posted "
        "and has already been acknowledged with a 👀 reaction — do not acknowledge it again.",
        "Do not use react_to_slack_message for another routine acknowledgement in this passive triage run.",
        "",
        durable_preference_guidance(),
        "",
        "Classify this message and act accordingly:",
        "- A human explicitly stating a durable presentation/behaviour preference requires a "
        "visible reply. Follow the durable-preference contract above before the general triage "
        "branches below.",
        "- Casual chatter, or discussion about an existing alert that does not itself ask for "
        "work: take NO visible action. Do not reply. A message is NOT alert commentary merely "
        "because it arrived near an alert — if a human is asking for something, it belongs in "
        "the underspecified-request branch below, not here.",
        "- A genuine automated alert (Sentry, Rollbar, CI) or a user-reported problem that is "
        "ticket-worthy AND the target repo and incident are both clear: open a REAL GitHub issue "
        "with create_github_issue in the correct uwear-ai repo. Load the 'uwear-engineering-triage' "
        "skill (brain_skills then skill_view) first for routing/ownership rules, and before filing "
        "fetch its on-demand 'creating work items' playbook per the skill's On-demand Run Modes "
        "section (a Domain 37 doc_page record; skill_asset references/creating-work-items.md as "
        "fallback), then optionally "
        "post a brief Slack note with post_slack_reply citing the issue number and URL.",
        "- If your investigation reaches a root-cause hypothesis naming the target repo, that IS "
        "'repo and incident clear': file the issue in that repo in the same run. Include the "
        "investigation findings in the issue body — not just a link to Slack — so the analysis is "
        "captured in the work item instead of being left only in Slack.",
        "- A human request/report or apparent request for work that is actionable but "
        "underspecified because the target repo or incident is unclear: do NOT stay silent and do "
        "NOT return 'No visible action taken.' Ask exactly ONE focused clarifying question "
        "in-thread with post_slack_reply, then act on the answer. Proximity to an existing alert "
        "alone does not turn an apparent human request into alert commentary. This branch does "
        "not apply to casual chatter or genuine commentary that does not ask for work.",
        "- A message that @-mentions Illo must NEVER end in silence or 'No visible action taken.' "
        "Either file the issue or post a visible reply; if the details are insufficient to file, "
        "ask exactly ONE focused clarifying question in-thread.",
        "- A user-submitted feature request or product idea (feedback relayed by a bot such as "
        "Retool — e.g. '*New:* Idea' or '*New:* Feedback' with a user email and profile id — is "
        "a real customer ask, NOT chatter and NOT low-signal): if the ask is concrete and "
        "actionable, it IS ticket-worthy — follow the same flow as above to open a REAL GitHub "
        "issue in the correct uwear-ai repo, quoting the user's ask and including their "
        "email/profile id in the issue body, then post a brief thread note with post_slack_reply "
        "citing the issue number and URL so the team knows it was captured. Only if the ask is "
        "too vague to act on, or it duplicates an existing open issue, comment on the existing "
        "issue or stay silent instead.",
        "- An alert that matches an EXISTING ticket or issue (same tracked error signature; a "
        "Rollbar item id alone is not enough when the title names a different failure mode): "
        "do NOT refile and do NOT blindly skip — follow the triage skill's Deploy-State Ladder: "
        "note occurrences while unfixed; a fix merged to staging but not promoted is expected "
        "noise (annotate, no owner re-ping, even if the ticket was closed early); a fix deployed "
        "to prod that still fires PAST the settle window (~30 min after deploy) means the fix "
        "did not work — reopen the ticket and escalate to the fix author (re-fires inside the "
        "settle window are expected drain noise).",
        "- If create_github_issue reports no write-capable token can reach the repo "
        "(no_write_token / 403 / 404): do NOT claim a GitHub issue was filed. Surface the failure "
        "with post_slack_reply, or record an internal tracker record + handoff so it is not lost.",
        "- Ambiguous or low-signal content that is neither a human request/report nor a direct "
        "@-mention: prefer no visible action; the 👀 already confirms you saw it.",
        "",
        "Silence is the correct default for casual chatter and genuine alert commentary. Use "
        "post_slack_reply when you have opened/flagged a ticket, must ask the one clarifying "
        "question required above, or must surface something important. Use read_slack_conversation "
        "(scope=recent_channel or thread) for more context before deciding. An internal Domain/"
        "tracker record is NOT a GitHub issue — only a successful create_github_issue opens a real "
        "issue; never describe a tracker record as a filed GitHub issue.",
        "When an automated alert creates or updates a Domain 1 tracker record, preserve this "
        "origin in the structured alert_slack_channel and alert_slack_thread_ts fields when the "
        "schema exposes them, so future sweeps can re-read human resolution replies.",
        "",
    ]
    provider_alert = payload.get("provider_alert")
    if isinstance(provider_alert, Mapping):
        lines.extend(
            [
                "Deterministic provider-alert identity (use this for ticket dedup):",
                f"- Service/subsystem: {provider_alert.get('service')} / {provider_alert.get('subsystem')}",
                f"- External id: {provider_alert.get('external_id')}",
                f"- Tracked signature: {provider_alert.get('tracked_signature')}",
                f"- Signature title: {provider_alert.get('signature_title')}",
            ]
        )
        if provider_alert.get("is_new_error"):
            lines.append(
                "- Rollbar marked this `New error:`. Dedup only against a ticket whose tracked "
                "signature matches; otherwise file it or add an explicit new-signature entry to "
                "the parent issue."
            )
        if provider_alert.get("surge_open"):
            if provider_alert.get("material_posted"):
                lines.append(
                    "- The ingest gate already posted the ONE consolidated material incident to "
                    "the software channel. Do not post another consolidated incident; continue "
                    "only the normal per-alert annotation/ticket action."
                )
            elif provider_alert.get("material_post_error"):
                lines.append(
                    "- A material surge is open, but its consolidated Slack post failed: "
                    f"{provider_alert.get('material_post_error')}. Surface that delivery failure."
                )
        lines.append("")
    lines.extend(
        [
            f"Channel: {channel_id}" + (f" ({channel_name})" if channel_name else ""),
            f"Team: {slack_trigger_payload.get('team_id')}",
            f"Message ts: {slack_trigger_payload.get('message_ts')}",
            f"Author (Slack id): {author}",
        ]
    )
    if slack_trigger_payload.get("permalink"):
        lines.append(f"Permalink: {slack_trigger_payload.get('permalink')}")
    lines.extend(["", f"Message text: {text}"])
    return "\n".join(lines)


def durable_preference_guidance() -> str:
    return "\n".join(
        [
            "Durable preference contract:",
            "- Never answer a stated durable presentation/behaviour preference with a bare "
            "promise such as 'Yes, I will.' A durable promise requires a successful concrete "
            "settings write.",
            "- The only known writable presentation mapping is a requested time zone: call "
            "manage_runtime_preferences with action='set', setting='display_timezone', and the "
            "requested IANA zone. Map ET/Eastern to America/New_York.",
            "- Only after manage_runtime_preferences returns status='saved', reply with post_slack_reply "
            "and include its confirmation verbatim. That confirmation names the concrete setting "
            "and vault_config storage key that changed.",
            "- If the request has no known writable setting, or the write is denied/fails, say it "
            "was not saved. For no write target, reply: 'I can do that for this message, but I "
            "have no way to make it stick — file it?' Never imply persistence.",
        ]
    )


def _clean(value: Any) -> str:
    return str(value or "").strip()


__all__ = ["durable_preference_guidance", "slack_channel_monitor_message"]

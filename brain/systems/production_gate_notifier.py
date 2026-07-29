"""Slack batching and rendering for production-gate findings."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import logging
from typing import Any

from brain.systems.production_gate_policy import ProductionGateFinding


SOFTWARE_CHANNEL = "#4_software"
logger = logging.getLogger("illo.production_gate.notifier")


async def post_production_gate_findings(
    findings: Sequence[ProductionGateFinding],
    *,
    slack: Any | None = None,
) -> tuple[int, list[str]]:
    """Post at most one sweep-bounded message per issue closer."""

    if not findings:
        return 0, []
    errors: list[str] = []
    if slack is None:
        try:
            from brain.systems.slack.client import slack_web_client_from_runtime

            slack = await slack_web_client_from_runtime(
                requested_by="staging_only_closure_sweep",
                reason=(
                    "Post one batched promote/verify action for prematurely "
                    "closed GitHub issues."
                ),
            )
        except Exception as exc:  # noqa: BLE001 - tracker corrections remain valid
            logger.warning("production-gate Slack client unavailable: %s", exc)
            return 0, [f"slack_client:{exc}"]

    try:
        software_channel = await resolve_slack_channel(slack, SOFTWARE_CHANNEL)
    except Exception as exc:  # noqa: BLE001
        logger.warning("production-gate channel lookup failed: %s", exc)
        errors.append(f"slack_channel:{exc}")
        software_channel = SOFTWARE_CHANNEL

    grouped: dict[str, list[ProductionGateFinding]] = {}
    for finding in findings:
        grouped.setdefault(_closer_key(finding), []).append(finding)

    posted = 0
    for group in grouped.values():
        try:
            await slack.post_message(
                channel=software_channel,
                text=_render_batch(group),
            )
            posted += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("production-gate Slack post failed: %s", exc)
            errors.append(f"slack_post:{exc}")
    return posted, errors


def _render_batch(findings: Sequence[ProductionGateFinding]) -> str:
    closer = next(
        (
            _text(finding.closure.closed_by)
            for finding in findings
            if _text(finding.closure.closed_by)
        ),
        "closer",
    )
    lines = [f"@{closer}: closed issues still need production promotion/verification:"]
    for finding in findings:
        closure = finding.closure
        pull_request = finding.pull_request
        evidence = finding.production_evidence
        severity = ""
        evidence_suffix = ""
        if evidence is not None:
            severity = "⚠️ *PROD FAILURE STILL LIVE* — "
            evidence_suffix = (
                f" Live evidence: {evidence.source} {evidence.reference} "
                f"at {_utc(evidence.occurred_at).isoformat()}."
            )
        lines.append(
            f"• {severity}#{closure.number} is closed, but #{pull_request.number} is on "
            f"`{pull_request.base_ref_name}` only and `main` does not contain it; "
            f"the action is promote/verify.{evidence_suffix}"
        )
    return "\n".join(lines)


def _closer_key(finding: ProductionGateFinding) -> str:
    return _text(finding.closure.closed_by).casefold()


async def resolve_slack_channel(client: Any, channel: str) -> str:
    list_channels = getattr(client, "conversations_list", None)
    if not channel.startswith("#") or not callable(list_channels):
        return channel
    name = channel.removeprefix("#")
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        response = await list_channels(
            types="public_channel,private_channel",
            limit=200,
            cursor=cursor,
            exclude_archived=True,
        )
        for candidate in response.get("channels") or []:
            if (
                isinstance(candidate, Mapping)
                and _text(candidate.get("name")) == name
            ):
                return _text(candidate.get("id")) or channel
        metadata = response.get("response_metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        next_cursor = _text(metadata.get("next_cursor"))
        if not next_cursor or next_cursor in seen:
            return channel
        seen.add(next_cursor)
        cursor = next_cursor


def _text(value: object) -> str:
    return str(value or "").strip()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

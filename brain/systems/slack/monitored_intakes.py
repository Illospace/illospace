"""One typed dispatcher for every monitored Slack intake."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import re
from typing import Any, Awaitable, Callable, Mapping

from brain.systems.inbound.handlers import InboundCompletion
from brain.systems.inbound.status import STATUS_PROCESSED
from brain.systems.liveness_state import LivenessSnapshot
from brain.systems.runs.obligation_specs import (
    ObligationAnswerer,
    ObligationSettlementPolicy,
    ObligationSpec,
)
from brain.systems.slack.channel_monitor_rendering import (
    slack_channel_monitor_message,
)
from brain.systems.slack.contact_form_lead_owner import (
    CONTACT_FORM_OWNER_POLICY,
)
from brain.systems.slack.contact_form_lead_rendering import (
    CONTACT_FORM_LEAD_SKILL,
    ContactFormLeadReminderRenderer,
    contact_form_lead_run_message,
)
from brain.systems.slack.contact_form_leads import (
    CONTACT_FORM_LEAD_ORIGIN,
    ContactFormLead,
)
from brain.systems.slack.interrupt_delivery import (
    SlackAcknowledgementMode,
    SlackInterruptDeliveryDirective,
    SlackInterruptReply,
)
from brain.systems.slack.monitors import contact_form_lead_mandate


logger = logging.getLogger(__name__)

SLACK_CHANNEL_MESSAGE_ORIGIN = "slack.channel_message"
DIRECT_LIVENESS_PROBE_ORIGIN = "slack.direct_liveness_probe"
SLACK_REPLY_TOOL = "post_slack_reply"

DIRECT_LIVENESS_PROBE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:are\s+you\s+(?:still\s+)?alive|you\s+(?:still\s+)?alive|still\s+alive)[\s?!.,:;…]*", re.IGNORECASE),
    re.compile(r"(?:are\s+you\s+dead|you\s+dead)[\s?!.,:;…]*", re.IGNORECASE),
    re.compile(r"(?:are\s+you\s+(?:still\s+)?there|you\s+(?:still\s+)?there|still\s+there)[\s?!.,:;…]*", re.IGNORECASE),
    re.compile(r"(?:are\s+you\s+up|you\s+up|still\s+up)[\s?!.,:;…]*", re.IGNORECASE),
)
_NO_MATCH = object()


@dataclass(frozen=True, slots=True)
class SlackVisibleContent:
    """Visible Slack content decoded once before intake recognition."""

    message_text: str
    block_text: str

    @property
    def contact_form_text(self) -> str:
        return _bounded_text(
            "\n".join(
                part for part in (self.message_text, self.block_text) if part
            )
        )


@dataclass(frozen=True, slots=True)
class ContactFormLeadRecognition:
    lead: ContactFormLead
    requester_slack_id: str
    visible_text: str


@dataclass(frozen=True, slots=True)
class ChannelMessageRecognition:
    text: str


@dataclass(frozen=True, slots=True)
class DirectLivenessProbeRecognition:
    text: str


@dataclass(frozen=True, slots=True)
class MonitoredIntakeRecognition:
    """Shared classification data for one ordered Slack intake recognizer."""

    origin: str
    event_kind: str
    recognize: Callable[
        [Mapping[str, Any], SlackVisibleContent, str | None],
        Any,
    ]
    text: Callable[[Any], str]
    allowed_ignored_subtypes: frozenset[str] = frozenset()
    can_disable: bool = False


@dataclass(frozen=True, slots=True)
class MonitoredIntakeMatch:
    policy: MonitoredIntakePolicy
    decoded: Any

    @property
    def text(self) -> str:
        return self.policy.recognition.text(self.decoded)


@dataclass(frozen=True, slots=True)
class RenderedMonitoredIntake:
    run_message: str
    metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class MonitoredIntakeRoute:
    metadata_origin: str
    metadata: Mapping[str, Any]
    run_message: str


@dataclass(frozen=True, slots=True)
class MonitoredIntakeContext:
    connection: Any
    bot_token: str


@dataclass(frozen=True, slots=True)
class RunIntakePolicy:
    """Recognition and shaping behavior for an admitted AgentRun."""

    recognition: MonitoredIntakeRecognition
    enrich_payload: Callable[[dict[str, Any], Any], None]
    enrich: Callable[
        [dict[str, Any], MonitoredIntakeContext],
        Awaitable[None],
    ]
    render: Callable[
        [Mapping[str, Any], Mapping[str, Any]],
        RenderedMonitoredIntake,
    ]
    routing: Callable[[Mapping[str, Any]], Mapping[str, Any]]
    obligation: Callable[
        [Mapping[str, Any], Mapping[str, Any]],
        ObligationSpec | None,
    ]


@dataclass(frozen=True, slots=True)
class ImmediateReplyDisposition:
    """One immediate action, including completion and visible Slack behavior."""

    completion: InboundCompletion
    delivery: SlackInterruptDeliveryDirective


@dataclass(frozen=True, slots=True)
class ImmediateReplyPolicy:
    """Recognition and completion behavior for a reply without an AgentRun."""

    recognition: MonitoredIntakeRecognition
    action_type: str
    operation: str
    render_reply: Callable[[LivenessSnapshot], str]
    acknowledgement: SlackAcknowledgementMode

    def build_disposition(
        self,
        envelope: Mapping[str, Any],
        *,
        event_id: str,
        source_kind: str | None,
        snapshot: LivenessSnapshot,
    ) -> ImmediateReplyDisposition:
        payload = _mapping(envelope.get("payload"))
        response_target = _mapping(payload.get("response_target"))
        delivery = SlackInterruptDeliveryDirective(
            acknowledgement=self.acknowledgement,
            reply=SlackInterruptReply(
                channel_id=_clean(
                    response_target.get("channel_id") or payload.get("channel_id")
                ),
                thread_ts=_clean(response_target.get("thread_ts")) or None,
                text=self.render_reply(snapshot),
                idempotency_key=_clean(envelope.get("idempotency_key")),
            ),
        )
        completion = InboundCompletion(
            status=STATUS_PROCESSED,
            action_type=self.action_type,
            action_result={
                "operation": self.operation,
                "event_id": event_id,
                "origin": envelope.get("origin"),
                "delivery_directive": delivery.to_payload(),
            },
            confidence=1.0,
            target={
                "kind": "slack_message",
                "channel_id": response_target.get("channel_id"),
                "thread_ts": response_target.get("thread_ts"),
            },
            tool_use={"type": SLACK_REPLY_TOOL, "status": "interrupt"},
            reasoning_summary=(
                "A typed Slack intake policy routed this direct liveness "
                "probe to a deterministic process-local reply."
            ),
            reusable_pattern_candidate={
                "kind": "slack_message",
                "origin": envelope.get("origin"),
                "source_kind": source_kind,
            },
        )
        return ImmediateReplyDisposition(completion=completion, delivery=delivery)


MonitoredIntakePolicy = RunIntakePolicy | ImmediateReplyPolicy


def visible_slack_content(
    event: Mapping[str, Any],
    *,
    limit: int | None = 4000,
) -> SlackVisibleContent:
    """Decode Slack text and blocks once without changing the alert fallback."""

    return SlackVisibleContent(
        message_text=_event_message_text(event, limit=limit),
        block_text=_slack_block_text(event.get("blocks"), limit=limit),
    )


def recognize_monitored_intake(
    event: Mapping[str, Any],
    content: SlackVisibleContent,
    *,
    bot_user_id: str | None = None,
) -> MonitoredIntakeMatch:
    """Return the first typed policy match; the ordinary alert policy is fallback."""

    for policy in MONITORED_INTAKE_POLICIES:
        recognition = policy.recognition
        decoded = recognition.recognize(event, content, bot_user_id)
        if decoded is not _NO_MATCH:
            return MonitoredIntakeMatch(policy=policy, decoded=decoded)
    raise RuntimeError("monitored intake registry requires a fallback policy")


def enrich_monitored_intake_payload(
    payload: dict[str, Any],
    match: MonitoredIntakeMatch,
) -> None:
    if isinstance(match.policy, RunIntakePolicy):
        match.policy.enrich_payload(payload, match.decoded)


async def enrich_monitored_intake(
    envelope: dict[str, Any],
    *,
    connection: Any,
    bot_token: str,
) -> None:
    policy = monitored_intake_policy(envelope)
    if not isinstance(policy, RunIntakePolicy):
        return
    await policy.enrich(
        envelope,
        MonitoredIntakeContext(
            connection=connection,
            bot_token=bot_token,
        ),
    )


def route_monitored_intake(
    payload: Mapping[str, Any],
    slack_trigger_payload: Mapping[str, Any],
) -> MonitoredIntakeRoute | None:
    policy = monitored_intake_policy(payload)
    if not isinstance(policy, RunIntakePolicy):
        return None
    rendered = policy.render(payload, slack_trigger_payload)
    metadata = {
        **dict(policy.routing(payload)),
        **dict(rendered.metadata),
    }
    obligation = policy.obligation(payload, slack_trigger_payload)
    if obligation is not None:
        metadata["obligation_spec"] = obligation.to_metadata()
    return MonitoredIntakeRoute(
        metadata_origin=str(metadata.pop("origin")),
        metadata=metadata,
        run_message=rendered.run_message,
    )


def monitored_intake_policy(
    envelope_or_payload: Mapping[str, Any],
) -> MonitoredIntakePolicy | None:
    payload = envelope_or_payload.get("payload")
    source = payload if isinstance(payload, Mapping) else envelope_or_payload
    origin = _clean(
        envelope_or_payload.get("origin")
        or source.get("origin")
    )
    event_kind = _clean(source.get("event_kind"))
    if origin:
        for policy in MONITORED_INTAKE_POLICIES:
            if origin == policy.recognition.origin:
                return policy
        return None
    for policy in MONITORED_INTAKE_POLICIES:
        if event_kind == policy.recognition.event_kind:
            return policy
    return None


def is_monitored_intake(envelope_or_payload: Mapping[str, Any]) -> bool:
    return monitored_intake_policy(envelope_or_payload) is not None


def slack_response_thread_ts(
    channel_type: str,
    thread_ts: str,
    message_ts: str,
    *,
    is_monitored: bool,
) -> str | None:
    """Own Slack reply threading for every intake and ordinary Slack event."""

    if channel_type == "im":
        return None
    if is_monitored:
        return thread_ts or message_ts or None
    if thread_ts and thread_ts != message_ts:
        return thread_ts
    return None


def _recognize_contact_form(
    event: Mapping[str, Any],
    content: SlackVisibleContent,
    _bot_user_id: str | None,
) -> ContactFormLeadRecognition | object:
    lead = ContactFormLead.decode(content.contact_form_text)
    if lead is None:
        return _NO_MATCH
    requester_slack_id = _clean(event.get("bot_id") or event.get("user"))
    if not requester_slack_id:
        return _NO_MATCH
    return ContactFormLeadRecognition(
        lead=lead,
        requester_slack_id=requester_slack_id,
        visible_text=content.contact_form_text,
    )


def _contact_form_text(recognition: ContactFormLeadRecognition) -> str:
    return recognition.visible_text


def _enrich_contact_form_payload(
    payload: dict[str, Any],
    recognition: ContactFormLeadRecognition,
) -> None:
    payload["contact_form_lead"] = recognition.lead.to_payload()
    payload["obligation_requester"] = {
        "name": recognition.lead.name,
        "slack_user_id": recognition.requester_slack_id,
        "user_id": None,
    }


async def _enrich_contact_form(
    envelope: dict[str, Any],
    context: MonitoredIntakeContext,
) -> None:
    payload = _mapping(envelope.get("payload"))
    lead = _mapping(payload.get("contact_form_lead"))
    if not lead:
        return
    owner = CONTACT_FORM_OWNER_POLICY.resolve(context.connection)
    lead["owner"] = owner.to_metadata()
    payload["contact_form_lead"] = lead
    configured_mandate = contact_form_lead_mandate(context.connection)
    if configured_mandate:
        payload["contact_form_lead_mandate"] = configured_mandate
    envelope["payload"] = payload


def _render_contact_form(
    payload: Mapping[str, Any],
    slack_trigger_payload: Mapping[str, Any],
) -> RenderedMonitoredIntake:
    lead_payload = _mapping(payload.get("contact_form_lead"))
    lead = ContactFormLead.from_payload(lead_payload)
    owner = ObligationAnswerer.from_metadata(
        _mapping(lead_payload.get("owner"))
    )
    configured_mandate = _clean(payload.get("contact_form_lead_mandate"))
    return RenderedMonitoredIntake(
        run_message=contact_form_lead_run_message(
            lead,
            owner,
            slack_trigger_payload,
            mandate=configured_mandate or None,
        ),
        metadata={
            "contact_form_lead": lead_payload,
            "contact_form_lead_skill": CONTACT_FORM_LEAD_SKILL,
            "contact_form_lead_mandate_source": (
                "installed_skill_with_connection_overlay"
                if configured_mandate
                else "installed_skill"
            ),
            "obligation_requester": _mapping(
                payload.get("obligation_requester")
            ),
            "obligation_ask_text": lead.message,
        },
    )


def _contact_form_routing(
    _payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    return {
        "origin": "slack_teammate",
        "headless": True,
        "required_response_tool": SLACK_REPLY_TOOL,
        "final_answer_target_surface": "headless",
        "execution_profile": "fast",
    }


def _contact_form_obligation(
    payload: Mapping[str, Any],
    _slack_trigger_payload: Mapping[str, Any],
) -> ObligationSpec:
    lead_payload = _mapping(payload.get("contact_form_lead"))
    lead = ContactFormLead.from_payload(lead_payload)
    owner = ObligationAnswerer.from_metadata(
        _mapping(lead_payload.get("owner"))
    )
    return ObligationSpec(
        condition="contact_form_lead:answerer_reply",
        answerer=owner,
        notice_after=timedelta(hours=24),
        renderer=ContactFormLeadReminderRenderer(lead=lead, owner=owner),
        settlement_policy=ObligationSettlementPolicy.ANSWERER_SLACK_REPLY,
    )


def _recognize_channel_message(
    _event: Mapping[str, Any],
    content: SlackVisibleContent,
    _bot_user_id: str | None,
) -> ChannelMessageRecognition:
    return ChannelMessageRecognition(text=content.message_text)


def _channel_message_text(recognition: ChannelMessageRecognition) -> str:
    return recognition.text


def _enrich_channel_message_payload(
    _payload: dict[str, Any],
    _recognition: ChannelMessageRecognition,
) -> None:
    return None


async def _enrich_channel_message(
    envelope: dict[str, Any],
    context: MonitoredIntakeContext,
) -> None:
    """Record Rollbar identity before the corresponding triage run starts."""

    from brain.systems.slack.client import SlackWebClient
    from brain.systems.slack.provider_alert_surge import (
        handle_provider_alert_ingest_durable,
    )

    payload = _mapping(envelope.get("payload"))
    org_id = _connection_org_id(context.connection)
    channel_id = _clean(payload.get("channel_id"))
    message_ts = _clean(payload.get("message_ts"))
    text = str(payload.get("text") or "")
    if not org_id or not channel_id or not message_ts:
        return
    try:
        result = await handle_provider_alert_ingest_durable(
            SlackWebClient(context.bot_token),
            org_id=org_id,
            channel_id=channel_id,
            message_ts=message_ts,
            text=text,
            occurred_at=_slack_message_datetime(message_ts),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("provider_alert_ingest_failed: %s", exc)
        return
    if result is None:
        return
    payload["provider_alert"] = {
        "service": result.alert.service,
        "subsystem": result.alert.subsystem,
        "external_id": result.alert.external_id,
        "tracked_signature": result.alert.signature,
        "signature_title": result.alert.signature_title,
        "occurrence_milestone": result.alert.occurrence_milestone,
        "is_new_error": result.alert.is_new_error,
        "surge_open": result.surge is not None,
        "material_posted": result.material_posted,
        "material_post_error": result.material_post_error,
    }
    envelope["payload"] = payload


def _render_channel_message(
    payload: Mapping[str, Any],
    slack_trigger_payload: Mapping[str, Any],
) -> RenderedMonitoredIntake:
    return RenderedMonitoredIntake(
        run_message=slack_channel_monitor_message(
            payload,
            slack_trigger_payload,
        ),
        metadata={},
    )


def _channel_message_routing(
    _payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    return {
        "origin": "slack_channel_monitor",
        "slack_monitor": True,
        "headless": True,
        "final_answer_target_surface": "headless",
        "execution_profile": "fast",
    }


def _no_obligation(
    _payload: Mapping[str, Any],
    _slack_trigger_payload: Mapping[str, Any],
) -> None:
    return None


def _recognize_direct_liveness_probe(
    event: Mapping[str, Any],
    content: SlackVisibleContent,
    bot_user_id: str | None,
) -> DirectLivenessProbeRecognition | object:
    text = content.message_text.strip()
    channel_type = _clean(event.get("channel_type"))
    if channel_type != "im":
        mention = f"<@{_clean(bot_user_id)}>" if _clean(bot_user_id) else ""
        if not mention or not text.startswith(mention):
            return _NO_MATCH
        text = text[len(mention):].lstrip(" \t,:;-—")
    elif bot_user_id:
        mention = f"<@{_clean(bot_user_id)}>"
        if text.startswith(mention):
            text = text[len(mention):].lstrip(" \t,:;-—")
    if not any(pattern.fullmatch(text) for pattern in DIRECT_LIVENESS_PROBE_PATTERNS):
        return _NO_MATCH
    return DirectLivenessProbeRecognition(text=content.message_text)


def _direct_liveness_probe_text(
    recognition: DirectLivenessProbeRecognition,
) -> str:
    return recognition.text


def _direct_liveness_probe_reply(
    snapshot: LivenessSnapshot,
) -> str:
    last_run_id = str(snapshot.last_run_id) if snapshot.last_run_id is not None else "none"
    # Honesty boundary: handling this message proves that the API process ran;
    # it does not prove that the scheduler or any other process is ticking.
    return (
        "Yes — the Illo API process handled this message. "
        f"Liveness snapshot: timestamp `{snapshot.ts}`, last run ID `{last_run_id}`, "
        f"coarse last surface `{snapshot.last_surface}`. "
        "This confirms API message handling; it does not confirm that the scheduler is ticking."
    )


# Recognition order lives in one registry. Each entry then selects exactly one
# action shape: admit a run or complete with an immediate reply.
MONITORED_INTAKE_POLICIES: tuple[MonitoredIntakePolicy, ...] = (
    ImmediateReplyPolicy(
        recognition=MonitoredIntakeRecognition(
            origin=DIRECT_LIVENESS_PROBE_ORIGIN,
            event_kind="direct_liveness_probe",
            recognize=_recognize_direct_liveness_probe,
            text=_direct_liveness_probe_text,
        ),
        action_type="slack.liveness_probe_interrupt",
        operation="slack_liveness_probe_interrupt",
        render_reply=_direct_liveness_probe_reply,
        acknowledgement=SlackAcknowledgementMode.SUPPRESS,
    ),
    RunIntakePolicy(
        recognition=MonitoredIntakeRecognition(
            origin=CONTACT_FORM_LEAD_ORIGIN,
            event_kind=CONTACT_FORM_LEAD_ORIGIN,
            recognize=_recognize_contact_form,
            text=_contact_form_text,
            allowed_ignored_subtypes=frozenset({"bot_message"}),
            can_disable=True,
        ),
        enrich_payload=_enrich_contact_form_payload,
        enrich=_enrich_contact_form,
        render=_render_contact_form,
        routing=_contact_form_routing,
        obligation=_contact_form_obligation,
    ),
    RunIntakePolicy(
        recognition=MonitoredIntakeRecognition(
            origin=SLACK_CHANNEL_MESSAGE_ORIGIN,
            event_kind="channel_message",
            recognize=_recognize_channel_message,
            text=_channel_message_text,
            allowed_ignored_subtypes=frozenset({"bot_message"}),
        ),
        enrich_payload=_enrich_channel_message_payload,
        enrich=_enrich_channel_message,
        render=_render_channel_message,
        routing=_channel_message_routing,
        obligation=_no_obligation,
    ),
)


def configurable_monitored_intake_origins() -> frozenset[str]:
    """Return passive intake origins that a Slack connection can disable."""

    return frozenset(
        policy.recognition.origin
        for policy in MONITORED_INTAKE_POLICIES
        if policy.recognition.can_disable
    )


def _bounded_text(value: Any, *, limit: int | None = 4000) -> str:
    text = str(value or "")
    return text if limit is None else text[:limit]


def _event_message_text(
    event: Mapping[str, Any],
    *,
    limit: int | None = 4000,
) -> str:
    """Preserve origin/main's alert text and attachment fallback byte-for-byte."""

    text = _bounded_text(event.get("text"), limit=limit)
    if text.strip():
        return text
    previews: list[str] = []
    attachments = event.get("attachments")
    if not isinstance(attachments, list):
        return text
    for attachment in attachments[:2]:
        if not isinstance(attachment, Mapping):
            continue
        preview = attachment.get("fallback") or attachment.get("title")
        preview_limit = 500 if limit is not None else None
        bounded = _bounded_text(preview, limit=preview_limit).strip()
        if bounded:
            previews.append(bounded)
    return _bounded_text("\n".join(previews), limit=limit)


def _slack_block_text(blocks: Any, *, limit: int | None = 4000) -> str:
    parts: list[str] = []

    def _collect(value: Any) -> None:
        if isinstance(value, Mapping):
            text = value.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
            elif isinstance(text, Mapping):
                _collect(text)
            for key in ("fields", "elements"):
                _collect(value.get(key))
        elif isinstance(value, list):
            for item in value:
                _collect(item)

    _collect(blocks)
    return _bounded_text("\n".join(parts), limit=limit)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _connection_org_id(connection: Any) -> str | None:
    if isinstance(connection, Mapping):
        return _clean(connection.get("org_id")) or None
    return _clean(getattr(connection, "org_id", None)) or None


def _slack_message_datetime(message_ts: str) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(message_ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


__all__ = [
    "DIRECT_LIVENESS_PROBE_ORIGIN",
    "DIRECT_LIVENESS_PROBE_PATTERNS",
    "ImmediateReplyDisposition",
    "ImmediateReplyPolicy",
    "MONITORED_INTAKE_POLICIES",
    "MonitoredIntakeMatch",
    "MonitoredIntakePolicy",
    "MonitoredIntakeRecognition",
    "MonitoredIntakeRoute",
    "RunIntakePolicy",
    "SLACK_CHANNEL_MESSAGE_ORIGIN",
    "SlackVisibleContent",
    "configurable_monitored_intake_origins",
    "enrich_monitored_intake",
    "enrich_monitored_intake_payload",
    "is_monitored_intake",
    "monitored_intake_policy",
    "recognize_monitored_intake",
    "route_monitored_intake",
    "slack_response_thread_ts",
    "visible_slack_content",
]

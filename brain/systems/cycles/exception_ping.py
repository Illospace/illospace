"""Shared material-change and per-person throttle gate for Cycle Slack pings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Mapping
from uuid import uuid4

from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.cycles.common import (
    OFF_SLOT_MATERIAL_ALERT_RUN_KIND,
    SCHEDULED_DIGEST_RUN_KIND,
    cycle_run_launch_context,
)

EXCEPTION_PING_LEDGER_KEY = "exception_ping_ledger"
EXCEPTION_PING_THROTTLE_MINUTES = 60
EXCEPTION_PING_CLAIM_MINUTES = 10
NO_MATERIAL_CHANGE_LEDGER_LINE = "Slack skipped: no material todo-list change"
_THROTTLED_LEDGER_LINE = "Slack skipped: teammate already pinged within 60 minutes"
_SCHEMA_VERSION = 1
_SLACK_MENTION = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]+)?>", re.IGNORECASE)
_VALID_RUN_KINDS = {
    SCHEDULED_DIGEST_RUN_KIND,
    OFF_SLOT_MATERIAL_ALERT_RUN_KIND,
}


@dataclass(frozen=True)
class ExceptionPingRequest:
    target_teammate_id: str
    item_ref: str
    change_types: tuple[str, ...]
    facts: dict[str, Any]


@dataclass(frozen=True)
class MaterialChangeDecision:
    material: bool
    reason: str
    matched_change_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExceptionPingPostGate:
    suppress: bool
    reason: str
    ledger_line: str
    request: ExceptionPingRequest
    material: MaterialChangeDecision
    claim_id: str | None = None
    previous_cycle_run_id: int | None = None
    previous_item_ref: str | None = None


def _utcnow(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _normalized_id(value: Any) -> str:
    return _text(value).casefold()


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    return None


def _datetime(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return _utcnow(datetime.fromisoformat(text))
    except ValueError:
        return None


def _change_types(value: Any) -> tuple[str, ...]:
    values = value if isinstance(value, list | tuple | set) else [value]
    normalized = []
    for raw in values:
        change_type = _text(raw).lower().replace("-", "_")
        if change_type and change_type not in normalized:
            normalized.append(change_type)
    return tuple(normalized)


def exception_ping_request(value: Mapping[str, Any] | None) -> ExceptionPingRequest:
    payload = dict(value) if isinstance(value, Mapping) else {}
    target_teammate_id = _text(payload.get("target_teammate_id"))
    if not target_teammate_id:
        raise ValueError("exception_ping.target_teammate_id is required")
    item_ref = _text(payload.get("item_ref"))
    if not item_ref:
        raise ValueError("exception_ping.item_ref is required")
    change_types = _change_types(
        payload.get("change_types")
        if payload.get("change_types") is not None
        else payload.get("change_type")
    )
    if not change_types:
        raise ValueError("exception_ping.change_types must name at least one observed change")
    facts = payload.get("facts")
    if not isinstance(facts, Mapping):
        raise ValueError("exception_ping.facts must be an object")
    return ExceptionPingRequest(
        target_teammate_id=target_teammate_id,
        item_ref=item_ref,
        change_types=change_types,
        facts=dict(facts),
    )


def _is_transition(
    facts: Mapping[str, Any],
    before_key: str,
    after_key: str,
    *,
    before: bool,
    after: bool,
) -> bool:
    return _bool(facts.get(before_key)) is before and _bool(facts.get(after_key)) is after


def evaluate_material_change(
    request: ExceptionPingRequest,
    *,
    now: datetime | None = None,
) -> MaterialChangeDecision:
    """Apply the authoritative todo-list materiality predicate."""

    facts = request.facts
    if (
        _bool(facts.get("auto_filed_alert_issue")) is True
        and _bool(facts.get("posted_to_alerts")) is True
    ):
        return MaterialChangeDecision(
            material=False,
            reason="auto_filed_alert_already_posted",
        )

    matched: list[str] = []
    for change_type in request.change_types:
        if change_type == "ownership_change":
            if (
                "previous_owner_id" in facts
                and "current_owner_id" in facts
                and _normalized_id(facts.get("previous_owner_id"))
                != _normalized_id(facts.get("current_owner_id"))
            ):
                matched.append(change_type)
        elif change_type == "blocker_hit":
            if _is_transition(
                facts,
                "blocker_before",
                "blocker_after",
                before=False,
                after=True,
            ):
                matched.append(change_type)
        elif change_type == "blocker_clear":
            if _is_transition(
                facts,
                "blocker_before",
                "blocker_after",
                before=True,
                after=False,
            ):
                matched.append(change_type)
        elif change_type == "active_set_enter":
            if _is_transition(
                facts,
                "active_before",
                "active_after",
                before=False,
                after=True,
            ):
                matched.append(change_type)
        elif change_type == "active_set_leave":
            if _is_transition(
                facts,
                "active_before",
                "active_after",
                before=True,
                after=False,
            ):
                matched.append(change_type)
        elif change_type == "new_unassigned_high_severity":
            if (
                _text(facts.get("severity")).lower() in {"high", "critical"}
                and _bool(facts.get("is_unassigned")) is True
            ):
                matched.append(change_type)
        elif change_type == "chantier_must_surface":
            if _bool(facts.get("must_surface")) is True:
                matched.append(change_type)

    if matched:
        return MaterialChangeDecision(
            material=True,
            reason="material_todo_list_change",
            matched_change_types=tuple(matched),
        )

    if "ci_status_transition" in request.change_types:
        opened_at = _datetime(facts.get("pr_opened_at"))
        opened_by = _normalized_id(facts.get("pr_opened_by_teammate_id"))
        target = _normalized_id(request.target_teammate_id)
        if opened_at is not None and opened_by and opened_by == target:
            age = _utcnow(now) - opened_at
            if timedelta(0) <= age < timedelta(minutes=EXCEPTION_PING_THROTTLE_MINUTES):
                return MaterialChangeDecision(
                    material=False,
                    reason="recent_owner_pr_ci_churn",
                )

    return MaterialChangeDecision(
        material=False,
        reason="no_material_todo_list_change",
    )


def empty_exception_ping_state() -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "throttle_minutes": EXCEPTION_PING_THROTTLE_MINUTES,
        "last_ping_by_teammate": {},
        "pending_by_teammate": {},
    }


def _state(value: Any) -> dict[str, Any]:
    raw = dict(value) if isinstance(value, Mapping) else {}
    state = empty_exception_ping_state()
    for key in ("last_ping_by_teammate", "pending_by_teammate"):
        candidate = raw.get(key)
        if isinstance(candidate, Mapping):
            state[key] = {
                _text(person_id): dict(item)
                for person_id, item in candidate.items()
                if _text(person_id) and isinstance(item, Mapping)
            }
    return state


def exception_ping_ledger_snapshot(value: Any) -> dict[str, Any]:
    state = _state(value)
    return {
        "schema_version": state["schema_version"],
        "throttle_minutes": state["throttle_minutes"],
        "last_ping_by_teammate": state["last_ping_by_teammate"],
        "decisions": [],
    }


def cycle_exception_ping_context(metadata: Any) -> dict[str, Any] | None:
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    cycle_run_id = metadata.get("cycle_run_id")
    if cycle_run_id in (None, ""):
        return None
    try:
        cycle_run_id = int(cycle_run_id)
    except (TypeError, ValueError):
        return None
    launch_envelope = metadata.get("launch_envelope")
    launch_envelope = (
        dict(launch_envelope) if isinstance(launch_envelope, Mapping) else {}
    )
    launch_context = launch_envelope.get("launch_context")
    launch_context = (
        dict(launch_context) if isinstance(launch_context, Mapping) else {}
    )
    if not launch_context and isinstance(metadata.get("launch_context"), Mapping):
        launch_context = dict(metadata["launch_context"])
    run_kind = _text(launch_context.get("run_kind")).lower()
    if run_kind not in _VALID_RUN_KINDS:
        return None
    return {
        "cycle_run_id": cycle_run_id,
        "run_kind": run_kind,
    }


def slack_mentioned_teammates(body: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_SLACK_MENTION.findall(str(body or ""))))


def exception_ping_payload_required(
    body: str,
    *,
    cycle_context: Mapping[str, Any] | None,
) -> bool:
    if not cycle_context:
        return False
    if cycle_context.get("run_kind") == OFF_SLOT_MATERIAL_ALERT_RUN_KIND:
        return True
    return bool(slack_mentioned_teammates(body))


def _decision_payload(
    gate: ExceptionPingPostGate,
    *,
    observed_at: datetime,
    run_kind: str,
    decision: str,
) -> dict[str, Any]:
    return {
        "observed_at": observed_at.isoformat(),
        "run_kind": run_kind,
        "target_teammate_id": gate.request.target_teammate_id,
        "item_ref": gate.request.item_ref,
        "change_types": list(gate.request.change_types),
        "facts": dict(gate.request.facts),
        "material": gate.material.material,
        "matched_change_types": list(gate.material.matched_change_types),
        "decision": decision,
        "reason": gate.reason,
        "ledger_line": gate.ledger_line,
        "claim_id": gate.claim_id,
        "previous_cycle_run_id": gate.previous_cycle_run_id,
        "previous_item_ref": gate.previous_item_ref,
    }


def _append_run_decision(run: CycleRun, payload: Mapping[str, Any]) -> None:
    context_snapshot = dict(run.context_snapshot or {})
    ledger = context_snapshot.get(EXCEPTION_PING_LEDGER_KEY)
    ledger = dict(ledger) if isinstance(ledger, Mapping) else {}
    decisions = list(ledger.get("decisions") or [])
    decisions.append(dict(payload))
    ledger.update(
        {
            "schema_version": _SCHEMA_VERSION,
            "throttle_minutes": EXCEPTION_PING_THROTTLE_MINUTES,
            "decisions": decisions,
        }
    )
    context_snapshot[EXCEPTION_PING_LEDGER_KEY] = ledger
    run.context_snapshot = context_snapshot


def _update_run_decision(
    run: CycleRun,
    claim_id: str,
    patch: Mapping[str, Any],
) -> None:
    context_snapshot = dict(run.context_snapshot or {})
    ledger = context_snapshot.get(EXCEPTION_PING_LEDGER_KEY)
    ledger = dict(ledger) if isinstance(ledger, Mapping) else {}
    decisions = list(ledger.get("decisions") or [])
    updated = False
    for index in range(len(decisions) - 1, -1, -1):
        decision = decisions[index]
        if not isinstance(decision, Mapping) or decision.get("claim_id") != claim_id:
            continue
        decisions[index] = {**dict(decision), **dict(patch)}
        updated = True
        break
    if not updated:
        decisions.append({"claim_id": claim_id, **dict(patch)})
    ledger["decisions"] = decisions
    context_snapshot[EXCEPTION_PING_LEDGER_KEY] = ledger
    run.context_snapshot = context_snapshot


def _record_run_last_ping(
    run: CycleRun,
    teammate_id: str,
    payload: Mapping[str, Any],
) -> None:
    context_snapshot = dict(run.context_snapshot or {})
    ledger = context_snapshot.get(EXCEPTION_PING_LEDGER_KEY)
    ledger = dict(ledger) if isinstance(ledger, Mapping) else {}
    last_ping_by_teammate = ledger.get("last_ping_by_teammate")
    last_ping_by_teammate = (
        dict(last_ping_by_teammate)
        if isinstance(last_ping_by_teammate, Mapping)
        else {}
    )
    last_ping_by_teammate[teammate_id] = dict(payload)
    ledger["last_ping_by_teammate"] = last_ping_by_teammate
    context_snapshot[EXCEPTION_PING_LEDGER_KEY] = ledger
    run.context_snapshot = context_snapshot


def _actual_run_kind(run: CycleRun, requested_run_kind: str) -> str:
    persisted = _text(cycle_run_launch_context(run).get("run_kind")).lower()
    if persisted not in _VALID_RUN_KINDS:
        raise ValueError("CycleRun does not carry an exception-ping-capable run kind")
    if requested_run_kind != persisted:
        raise ValueError("CycleRun run_kind does not match the posting context")
    return persisted


async def gate_exception_ping(
    *,
    cycle_run_id: int,
    run_kind: str,
    payload: Mapping[str, Any],
    now: datetime | None = None,
) -> ExceptionPingPostGate:
    """Claim one material per-person post or persist why it was suppressed."""

    request = exception_ping_request(payload)
    current_time = _utcnow(now)
    material = evaluate_material_change(request, now=current_time)
    async with UnitOfWork() as uow:
        run = await uow.session.get(CycleRun, int(cycle_run_id))
        if run is None:
            raise ValueError("CycleRun not found for exception ping")
        cycle = await uow.session.get(Cycle, int(run.cycle_id), with_for_update=True)
        if cycle is None:
            raise ValueError("Cycle not found for exception ping")
        run = await uow.session.get(CycleRun, int(cycle_run_id), with_for_update=True)
        actual_run_kind = _actual_run_kind(run, run_kind)
        state = _state(getattr(cycle, "exception_ping_state", None))

        if not material.material:
            gate = ExceptionPingPostGate(
                suppress=True,
                reason=material.reason,
                ledger_line=NO_MATERIAL_CHANGE_LEDGER_LINE,
                request=request,
                material=material,
            )
            _append_run_decision(
                run,
                _decision_payload(
                    gate,
                    observed_at=current_time,
                    run_kind=actual_run_kind,
                    decision="suppressed",
                ),
            )
            await uow.session.flush()
            return gate

        teammate_id = request.target_teammate_id
        previous = state["last_ping_by_teammate"].get(teammate_id)
        if isinstance(previous, Mapping):
            last_ping_at = _datetime(previous.get("last_ping_ts"))
            if (
                last_ping_at is not None
                and current_time - last_ping_at
                < timedelta(minutes=EXCEPTION_PING_THROTTLE_MINUTES)
            ):
                gate = ExceptionPingPostGate(
                    suppress=True,
                    reason="person_throttled_within_60_minutes",
                    ledger_line=_THROTTLED_LEDGER_LINE,
                    request=request,
                    material=material,
                    previous_cycle_run_id=previous.get("cycle_run_id"),
                    previous_item_ref=_text(previous.get("item_ref")) or None,
                )
                _append_run_decision(
                    run,
                    _decision_payload(
                        gate,
                        observed_at=current_time,
                        run_kind=actual_run_kind,
                        decision="suppressed",
                    ),
                )
                await uow.session.flush()
                return gate

        pending = state["pending_by_teammate"].get(teammate_id)
        if isinstance(pending, Mapping):
            claimed_at = _datetime(pending.get("claimed_at"))
            if (
                claimed_at is not None
                and current_time - claimed_at
                < timedelta(minutes=EXCEPTION_PING_CLAIM_MINUTES)
            ):
                gate = ExceptionPingPostGate(
                    suppress=True,
                    reason="person_ping_in_flight",
                    ledger_line=_THROTTLED_LEDGER_LINE,
                    request=request,
                    material=material,
                    previous_cycle_run_id=pending.get("cycle_run_id"),
                    previous_item_ref=_text(pending.get("item_ref")) or None,
                )
                _append_run_decision(
                    run,
                    _decision_payload(
                        gate,
                        observed_at=current_time,
                        run_kind=actual_run_kind,
                        decision="suppressed",
                    ),
                )
                await uow.session.flush()
                return gate
            state["pending_by_teammate"].pop(teammate_id, None)

        claim_id = str(uuid4())
        state["pending_by_teammate"][teammate_id] = {
            "claim_id": claim_id,
            "claimed_at": current_time.isoformat(),
            "cycle_run_id": int(run.id),
            "run_kind": actual_run_kind,
            "item_ref": request.item_ref,
        }
        cycle.exception_ping_state = state
        gate = ExceptionPingPostGate(
            suppress=False,
            reason=material.reason,
            ledger_line="Slack pending: material todo-list change",
            request=request,
            material=material,
            claim_id=claim_id,
        )
        _append_run_decision(
            run,
            _decision_payload(
                gate,
                observed_at=current_time,
                run_kind=actual_run_kind,
                decision="claimed",
            ),
        )
        await uow.session.flush()
        return gate


async def record_exception_ping_posted(
    *,
    cycle_run_id: int,
    run_kind: str,
    gate: ExceptionPingPostGate,
    channel_id: str,
    message_ts: str | None,
    thread_ts: str | None,
    now: datetime | None = None,
) -> None:
    """Finalize a successful Slack delivery into shared and per-run ledgers."""

    if gate.claim_id is None:
        raise ValueError("exception ping claim_id is required")
    current_time = _utcnow(now)
    async with UnitOfWork() as uow:
        run = await uow.session.get(CycleRun, int(cycle_run_id))
        if run is None:
            raise ValueError("CycleRun not found while finalizing exception ping")
        cycle = await uow.session.get(Cycle, int(run.cycle_id), with_for_update=True)
        if cycle is None:
            raise ValueError("Cycle not found while finalizing exception ping")
        run = await uow.session.get(CycleRun, int(cycle_run_id), with_for_update=True)
        actual_run_kind = _actual_run_kind(run, run_kind)
        state = _state(getattr(cycle, "exception_ping_state", None))
        pending = state["pending_by_teammate"].get(gate.request.target_teammate_id)
        if isinstance(pending, Mapping) and pending.get("claim_id") == gate.claim_id:
            state["pending_by_teammate"].pop(gate.request.target_teammate_id, None)
        last_ping = {
            "last_ping_ts": current_time.isoformat(),
            "cycle_run_id": int(run.id),
            "run_kind": actual_run_kind,
            "item_ref": gate.request.item_ref,
            "channel_id": channel_id,
            "message_ts": message_ts,
            "thread_ts": thread_ts,
        }
        state["last_ping_by_teammate"][gate.request.target_teammate_id] = last_ping
        cycle.exception_ping_state = state
        _record_run_last_ping(run, gate.request.target_teammate_id, last_ping)
        _update_run_decision(
            run,
            gate.claim_id,
            {
                "decision": "posted",
                "reason": "material_todo_list_change",
                "ledger_line": "Slack posted: material todo-list change",
                "posted_at": current_time.isoformat(),
                "channel_id": channel_id,
                "message_ts": message_ts,
                "thread_ts": thread_ts,
            },
        )
        await uow.session.flush()


async def release_exception_ping_claim(
    *,
    cycle_run_id: int,
    gate: ExceptionPingPostGate,
    reason: str,
) -> None:
    """Release a failed delivery so it does not consume the one-hour throttle."""

    if gate.claim_id is None:
        return
    async with UnitOfWork() as uow:
        run = await uow.session.get(CycleRun, int(cycle_run_id))
        if run is None:
            return
        cycle = await uow.session.get(Cycle, int(run.cycle_id), with_for_update=True)
        if cycle is None:
            return
        run = await uow.session.get(CycleRun, int(cycle_run_id), with_for_update=True)
        state = _state(getattr(cycle, "exception_ping_state", None))
        pending = state["pending_by_teammate"].get(gate.request.target_teammate_id)
        if isinstance(pending, Mapping) and pending.get("claim_id") == gate.claim_id:
            state["pending_by_teammate"].pop(gate.request.target_teammate_id, None)
            cycle.exception_ping_state = state
        _update_run_decision(
            run,
            gate.claim_id,
            {
                "decision": "delivery_failed",
                "reason": reason,
                "ledger_line": "Slack failed: exception ping not delivered",
            },
        )
        await uow.session.flush()


async def record_exception_ping_metadata_skip(
    *,
    cycle_run_id: int,
    run_kind: str,
    reason: str = "exception_ping_metadata_required",
) -> None:
    """Audit a fail-closed Cycle post that omitted structured ping metadata."""

    async with UnitOfWork() as uow:
        run = await uow.session.get(CycleRun, int(cycle_run_id), with_for_update=True)
        if run is None:
            return
        actual_run_kind = _actual_run_kind(run, run_kind)
        context_snapshot = dict(run.context_snapshot or {})
        ledger = context_snapshot.get(EXCEPTION_PING_LEDGER_KEY)
        ledger = dict(ledger) if isinstance(ledger, Mapping) else {}
        decisions = list(ledger.get("decisions") or [])
        decisions.append(
            {
                "observed_at": _utcnow().isoformat(),
                "run_kind": actual_run_kind,
                "decision": "suppressed",
                "reason": reason,
                "ledger_line": "Slack skipped: exception_ping metadata required",
            }
        )
        ledger.update(
            {
                "schema_version": _SCHEMA_VERSION,
                "throttle_minutes": EXCEPTION_PING_THROTTLE_MINUTES,
                "decisions": decisions,
            }
        )
        context_snapshot[EXCEPTION_PING_LEDGER_KEY] = ledger
        run.context_snapshot = context_snapshot
        await uow.session.flush()


__all__ = [
    "EXCEPTION_PING_LEDGER_KEY",
    "EXCEPTION_PING_THROTTLE_MINUTES",
    "ExceptionPingPostGate",
    "ExceptionPingRequest",
    "MaterialChangeDecision",
    "cycle_exception_ping_context",
    "empty_exception_ping_state",
    "evaluate_material_change",
    "exception_ping_ledger_snapshot",
    "exception_ping_payload_required",
    "exception_ping_request",
    "gate_exception_ping",
    "record_exception_ping_metadata_skip",
    "record_exception_ping_posted",
    "release_exception_ping_claim",
    "slack_mentioned_teammates",
]

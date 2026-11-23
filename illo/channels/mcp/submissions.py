"""Save knowledge in one transaction. Send other requests through the gateway."""

import hashlib
import json
import uuid
from datetime import UTC, datetime

from fastapi.encoders import jsonable_encoder

from illo.gateway import InboundEvent
from illo.gateway.replies import error_text
from illo.ledger import mcp as ledger


def normalize(args: dict) -> dict:
    """Source fields take precedence over the old top-level aliases."""
    source = dict(args["source"])
    for key in (
        "source_tool",
        "repo",
        "branch",
        "task_title",
        "session_id",
        "run_id",
        "files_touched",
    ):
        if args.get(key):
            source.setdefault(key, args[key])
    source.setdefault("source_tool", "external")
    parts = [p if isinstance(p, dict) else {"type": "text", "text": p} for p in args["parts"]]
    return {
        "message": args["message"],
        "desired_outcome": args["desired_outcome"],
        "origin": args["origin"] or f"{source['source_tool']}.submit",
        "source": source,
        "parts": parts,
        "constraints": args["constraints"],
        "correlation": args["correlation"],
        "response": args["response"],
        "metadata": args["metadata"],
    }


def text_body(envelope: dict) -> str:
    texts = [envelope["message"]]
    for part in envelope["parts"]:
        if isinstance(part.get("text"), str) and part["text"].strip():
            texts.append(part["text"])
    return "\n\n".join(texts)


def private(constraints: dict) -> bool:
    """Unrecognized privacy restrictions fail closed to a private save."""
    return bool(
        constraints.get("private")
        or (constraints.get("privacy") not in (None, False, "team", "public"))
        or (constraints.get("visibility") not in (None, "team", "public"))
    )


def receipt(event_id: str, *, saved=False, refs=None, handle=None) -> dict:
    status = "completed" if saved else "accepted"
    evidence = "satisfied" if saved else "not_required"
    handling = {"status": status, "run_id": None}
    return {
        "status": status,
        "event_id": event_id,
        "submission_id": event_id,
        "result_id": event_id,
        "operation": "preserve_knowledge" if saved else "request",
        "message": "The knowledge is saved." if saved else "The request is accepted.",
        "handling": handling,
        "handling_status": status,
        "run_id": None,
        "idempotent_replay": False,
        "error": None,
        "evidence_status": evidence,
        "mutated_target_refs": refs or [],
        "ilo_outcome": {
            "operation": "preserve_knowledge" if saved else "request",
            "message": "The knowledge is saved." if saved else "The request is accepted.",
            "handling": handling,
            "evidence_status": evidence,
            "mutated_target_refs": refs or [],
            "handle": handle,
        },
    }


async def submit(ctx, args: dict) -> dict:
    envelope = normalize(args)
    digest = hashlib.sha256(json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode())
    row, replay = await ledger.reserve(
        ctx.conn,
        ctx.token["id"],
        uuid.uuid4().hex,
        args["idempotency_key"],
        envelope,
        digest.hexdigest(),
    )
    if replay:
        result = {
            **row["result"],
            "idempotent_replay": True,
            "replay_body_matches": row["envelope_digest"] == digest.hexdigest(),
        }
        if not result["replay_body_matches"]:
            outcome = {
                "evidence_status": "replay_mismatch",
                "mutated_target_refs": [],
                "reason": "idempotency_key_reused_with_different_body",
            }
            result.update(ilo_outcome=outcome, **outcome)
        return result
    saved = envelope["desired_outcome"] == "preserve_knowledge"
    if saved:
        body = text_body(envelope)
        item = await ledger.save_item(
            ctx.conn,
            title=envelope["source"].get("task_title") or body.splitlines()[0],
            body=body,
            person_id=ctx.token["person_id"],
            visibility="private" if private(envelope["constraints"]) else "team",
            meta={**envelope, **envelope["source"]},
        )
        refs = [
            {
                "kind": "item",
                "id": item["id"],
                "source_ref": item["source_ref"],
                "role": "preserved",
                "visibility": item["visibility"],
            }
        ]
        result = receipt(row["event_id"], saved=True, refs=refs, handle=item["source_ref"])
        result["completed_at"] = item["source_at"].isoformat()
    else:
        await ledger.check_hourly_limit(ctx.conn, ctx.token["id"], submission=True)
        result = receipt(row["event_id"])
        # Dispatch only after the endpoint commits the receipt and its audit row.
        ctx.events.append(
            InboundEvent(
                channel="mcp",
                external_id=row["event_id"],
                kind="request",
                sender=ctx.bearer,
                text=text_body(envelope),
                payload={"submission_id": row["event_id"]},
            )
        )
    await ledger.finish_submission(ctx.conn, row["event_id"], result, result["status"])
    return result


async def get_result(ctx, args: dict) -> dict:
    event_id = args["event_id"] or args["submission_id"] or args["result_id"]
    row = await ledger.submission(ctx.conn, event_id, ctx.token["person_id"])
    if not row:
        raise ValueError("The submission was not found")
    result = row["result"] or {}
    run = await ledger.submission_run(ctx.conn, event_id)
    saved = row["desired_outcome"] == "preserve_knowledge"
    status = row["status"]
    run_status = "completed" if saved else run["status"] if run else None
    # Reconcile a run after a restart, even if delivery did not complete.
    if run_status in {"failed", "timeout"}:
        status = "failed"
    terminal = status in {"completed", "failed"}
    completed = row["completed_at"] or (run["ended_at"] if run else None)
    failure = result.get("failure")
    if status == "failed" and not failure:
        category = (run.get("usage") or {}).get("failure_category") if run else None
        if category not in {"limit", "timeout", "provider", "internal"}:
            category = "timeout" if run_status == "timeout" else "internal"
        failure = {"category": category, "message": error_text(category)}
    return {
        "event_id": event_id,
        "submission_id": event_id,
        "result_id": event_id,
        "status": status,
        "handling_status": status,
        "run_id": run["id"] if run else None,
        "run_status": run_status,
        "evidence_status": result.get("evidence_status", "not_required"),
        "requires_durable_evidence": saved,
        "mutated_target_refs": result.get("mutated_target_refs", []),
        "completed_at": completed,
        "reconciled_at": completed if terminal else None,
        "terminal": terminal,
        "final_answer": result.get("final_answer", result["message"] if saved else None),
        "failure": failure,
        "attribution": {
            "source": row["envelope"]["source"],
            "mutated_target_refs": result.get("mutated_target_refs", []),
        },
    }


class Adapter:
    def __init__(self, app):
        self.app = app

    async def show_working(self, event):
        pass

    async def deliver_final(self, event, text):
        await self._finish(event, text, "completed")

    async def deliver_error(self, event, *, category="internal"):
        await self._finish(event, error_text(category), "failed", category=category)

    async def _finish(self, event, text, status, *, category=None):
        async with self.app.pool.connection() as conn:
            run = await ledger.submission_run(conn, event.external_id)
            result = receipt(event.external_id)
            run_id = run["id"] if run else None
            handling = {"status": status, "run_id": run_id}
            result.update(
                status=status,
                handling_status=status,
                final_answer=text,
                completed_at=datetime.now(UTC).isoformat(),
                run_id=run_id,
                handling=handling,
                message=text,
            )
            if category:
                result["failure"] = {"category": category, "message": error_text(category)}
            result["ilo_outcome"].update(handling=handling, message=text)
            await ledger.finish_submission(
                conn,
                event.external_id,
                jsonable_encoder(result),
                status,
                run_id,
            )

"""Accept private forms before the event ledger or the model sees their values."""

import logging
import secrets
from datetime import UTC, datetime, timedelta

from illo.db import one
from illo.ledger import kv, people


class FormLogFilter(logging.Filter):
    def filter(self, record):
        if "view_submission" in record.getMessage() or "connect_form" in record.getMessage():
            record.msg = "Slack received a private form."
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True


# The SDK can log the full wire message before it calls our listener.
logging.getLogger("slack_sdk.socket_mode.aiohttp").addFilter(FormLogFilter())


async def open_form(ctx, person, trigger_id, kind, fields, *, draft_id=None, initial_values=None):
    nonce = secrets.token_urlsafe(32)
    async with ctx.pool.connection() as conn:
        await kv.set(
            conn,
            "connect:form:" + nonce,
            {
                "person_id": person["id"],
                "kind": kind,
                "draft_id": draft_id,
                "expires": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            },
        )
    await ctx.slack_client.views_open(
        trigger_id=trigger_id,
        view={
            "type": "modal",
            "callback_id": "connect_form",
            "private_metadata": nonce,
            "title": {"type": "plain_text", "text": "Connect a tool"},
            "submit": {"type": "plain_text", "text": "Save"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": key,
                    "optional": kind == "mcp_client" and key == "secret",
                    "label": {"type": "plain_text", "text": label},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                **({"initial_value": initial_values[key]}
                                   if initial_values and key in initial_values else {})},
                }
                for key, label in fields
            ],
        },
    )


async def prepare(ctx, payload):
    """Return a Slack response and work to run after the response."""
    view = payload.get("view", {})
    if view.get("callback_id") != "connect_form":
        return {}, None
    nonce = view.get("private_metadata", "")
    values = {
        key: value.get("value", {}).get("value", "") or ""
        for key, value in view.get("state", {}).get("values", {}).items()
    }
    errors = {}
    async with ctx.pool.connection() as conn:
        person = await people.by_slack_id(conn, payload.get("user", {}).get("id", ""))
        row = await one(
            conn, "SELECT value FROM kv WHERE key=%s FOR UPDATE", ("connect:form:" + nonce,)
        )
        form = row["value"] if row else None
        if (
            not form
            or not person
            or person["role"] != "owner"
            or person["id"] != form["person_id"]
            or datetime.fromisoformat(form["expires"]) <= datetime.now(UTC)
        ):
            errors[next(iter(values), "secret")] = (
                "This form expired or needs an active owner. Open it again."
            )
        else:
            from illo.connectors.chat import form_errors

            errors = form_errors(form["kind"], values)
        if not errors:
            await kv.delete(conn, "connect:form:" + nonce)
    if errors:
        return {"response_action": "errors", "errors": errors}, None

    async def finish():
        from illo.connectors.chat import submit_form

        try:
            await submit_form(ctx, person, form, values)
        except Exception:
            # A provider exception can contain a key. Do not log its text.
            from illo.connectors.chat import dm

            await dm(ctx, person, "The connection did not finish. Open the form to try again.")

    return {"response_action": "clear"}, finish

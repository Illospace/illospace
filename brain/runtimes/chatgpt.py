"""ChatGPT transport details. The Responses loop still owns the tool boundary."""

from datetime import UTC, datetime

from brain.models import RunError

BASE_URL = "https://chatgpt.com/backend-api/codex"


def limit_error(error, status=None):
    if not isinstance(error, dict):
        return None
    if error.get("type") != "usage_limit_reached" and error.get("code") != "usage_limit_reached":
        return None
    details = {"code": "usage_limit_reached", "provider_type": "usage_limit_reached"}
    if status is not None:
        details["http_status"] = status
    reset = error.get("resets_at")
    message = "Your ChatGPT limit is reached."
    # Never copy a provider message or an unchecked reset string into an error.
    if type(reset) is int and 0 < reset < 253402300800:
        details["resets_at"] = reset
        message += (
            " It resets " + datetime.fromtimestamp(reset, UTC).strftime("%Y-%m-%d %H:%M UTC") + "."
        )
    return RunError("limit", message, **details)

"""Plain failure text stays outside the brain prompt."""

import httpx

FAILURE_TEXTS = {
    "limit": 'I could not finish this: I reached my step limit. Ask again, or ask for less.',
    "timeout": 'I could not finish this in time. Ask again, or ask for less.',
    "provider": 'I could not finish this: the model service failed. Ask again in a minute.',
    "transient": "The model service is temporarily unavailable. Ask again in a minute.",
    "config": "Illo is not set up for this kind of request. An operator must fix the settings.",
    "internal": 'I could not finish this because of an internal error. Ask again in a minute.',
}


def error_text(category="internal"):
    return FAILURE_TEXTS.get(category, FAILURE_TEXTS["internal"])


RUN_ERROR = error_text()


class RunFailure(RuntimeError):
    def __init__(self, category, *, detail=None):
        self.category = category if category in FAILURE_TEXTS else "internal"
        self.detail = detail
        super().__init__(error_text(self.category))


def failure_category(error):
    if isinstance(error, RunFailure):
        return error.category
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(error, httpx.HTTPError):
        return "provider"
    return "internal"

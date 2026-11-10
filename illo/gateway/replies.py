"""Plain failure text stays outside the brain prompt."""

import httpx

FAILURE_TEXTS = {
    "limit": "Illo could not finish this request. The run reached its limit. Please try again.",
    "timeout": "Illo could not finish this request. The run reached its time limit. Please try again.",
    "provider": "Illo could not finish this request. The model provider failed. Please try again.",
    "internal": "Illo could not finish this request. An internal error occurred. Please try again.",
}


def error_text(category="internal"):
    return FAILURE_TEXTS.get(category, FAILURE_TEXTS["internal"])


RUN_ERROR = error_text()


class RunFailure(RuntimeError):
    def __init__(self, category):
        self.category = category if category in FAILURE_TEXTS else "internal"
        super().__init__(error_text(self.category))


def failure_category(error):
    if isinstance(error, RunFailure):
        return error.category
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(error, httpx.HTTPError):
        return "provider"
    return "internal"

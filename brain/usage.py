"""Usage arithmetic. The run keeps every numeric field and detail object that a provider reports."""


class Usage(dict):
    """The raw usage of one model response, as the provider reports it."""


# Keep the completion total and reasoning detail as reported. Verified by live
# Gemini smoke (gemini-4-neon, high): completion_tokens excludes reasoning tokens
# and completion_tokens_details is omitted; total_tokens includes reasoning tokens.
def add_usage(total: dict, usage: dict) -> None:
    for key, value in usage.items():
        if isinstance(value, dict):
            add_usage(total.setdefault(key, {}), value)
        elif isinstance(value, (int, float)):
            total[key] = total.get(key, 0) + value

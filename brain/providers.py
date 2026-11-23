"""The provider table. The service builds it once at start. Only this module reads provider keys.

`ILLO_PROVIDER_<NAME>_API_KEY` sets a key and `ILLO_PROVIDER_<NAME>_BASE_URL` changes a URL.
A custom provider also sets `_WIRE` and `_CAPABILITIES`. A provider with no key is not in the
table, except `chatgpt`: its tokens come with each run. `fake` needs `ILLO_PROVIDER_FAKE=1`.
`python -m brain.providers` prints the description of `GET /providers`. The update uses it.
"""

import json
import os
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

PREFIX = "ILLO_PROVIDER_"
SUFFIXES = ("API_KEY", "BASE_URL", "WIRE", "CAPABILITIES")
EFFORTS = ("low", "medium", "high", "xhigh")
NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
EFFORT = re.compile(r"[a-z]{1,16}")
# The Chat Completions wire sends text only and has no web search tool.
TEXT_ONLY = {"chat"}


@dataclass(frozen=True)
class Provider:
    name: str
    wire: str
    base_url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    images: bool = False
    web_search: bool = False
    efforts: tuple[str, ...] = ()
    transcription: bool = False

    def describe(self) -> dict:
        """The public description. It never holds a key or a URL."""
        return {"name": self.name, "wire": self.wire, "capabilities": {
            "images": self.images, "web_search": self.web_search, "efforts": list(self.efforts),
            "transcription": self.transcription}}

    def wire_dict(self) -> dict:
        """The private form for the worker's standard input."""
        return asdict(self)


BUILT_IN = {
    "chatgpt": Provider("chatgpt", "responses", "https://chatgpt.com/backend-api/codex",
                        images=True, web_search=True, efforts=EFFORTS),
    "openai": Provider("openai", "responses", "https://api.openai.com/v1",
                       images=True, web_search=True, efforts=EFFORTS, transcription=True),
    "anthropic": Provider("anthropic", "agent_sdk", efforts=EFFORTS),
    "fake": Provider("fake", "fake", images=True, web_search=True, efforts=EFFORTS,
                     transcription=True),
}


def description(table: dict[str, Provider]) -> dict:
    return {"providers": [provider.describe() for provider in table.values()]}


def base_url(name: str, value: str) -> str:
    parts = urlsplit(value)
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment):
        raise ValueError(f"{PREFIX}{name.upper()}_BASE_URL: use an HTTP address with no "
                         "credentials, query, or fragment")
    return value.rstrip("/")


def capabilities(name: str, wire: str, value: str) -> dict:
    """Read `images,web_search,transcription,effort:low,effort:high`."""
    result = {"images": False, "web_search": False, "transcription": False, "efforts": []}
    for item in filter(None, (part.strip() for part in value.split(","))):
        if item in ("images", "web_search") and wire in TEXT_ONLY:
            raise ValueError(f"{PREFIX}{name.upper()}_CAPABILITIES: the {wire} wire cannot "
                             f"send {item}")
        if item in ("images", "web_search", "transcription"):
            result[item] = True
        elif item.startswith("effort:") and EFFORT.fullmatch(item[7:]):
            result["efforts"].append(item[7:])
        else:
            raise ValueError(f"{PREFIX}{name.upper()}_CAPABILITIES: unknown capability")
    result["efforts"] = tuple(result["efforts"])
    return result


def load(environ=os.environ) -> dict[str, Provider]:
    """Build the table. A wrong variable stops the service. Errors name variables, not values."""
    settings: dict[str, dict[str, str]] = {}
    for variable, value in environ.items():
        if not variable.startswith(PREFIX) or variable == PREFIX + "FAKE":
            continue
        rest = variable[len(PREFIX):]
        suffix = next((s for s in SUFFIXES if rest.endswith("_" + s)), None)
        name = rest[: -len(suffix) - 1].lower() if suffix else ""
        if not NAME.fullmatch(name) or name == "fake":
            raise ValueError(f"{variable}: unknown provider variable")
        if value:
            settings.setdefault(name, {})[suffix] = value
    table = {"chatgpt": BUILT_IN["chatgpt"]}
    if environ.get(PREFIX + "FAKE") == "1":
        table["fake"] = BUILT_IN["fake"]
    for name, values in sorted(settings.items()):
        built_in = BUILT_IN.get(name)
        if built_in and {"WIRE", "CAPABILITIES"} & values.keys():
            raise ValueError(f"{PREFIX}{name.upper()}: a built-in provider has a fixed wire "
                             "and fixed capabilities")
        if name == "chatgpt" and "API_KEY" in values:
            raise ValueError(f"{PREFIX}CHATGPT_API_KEY: ChatGPT tokens come with each run")
        url = base_url(name, values["BASE_URL"]) if "BASE_URL" in values else None
        if built_in:
            provider = Provider(**{**built_in.wire_dict(), "api_key": values.get("API_KEY"),
                                   **({"base_url": url} if url else {})})
        elif values.get("WIRE") not in ("responses", "chat") or not url:
            raise ValueError(f"{PREFIX}{name.upper()}: a custom provider needs _WIRE "
                             "(responses or chat) and _BASE_URL")
        else:
            provider = Provider(name, values["WIRE"], url, values.get("API_KEY"),
                                **capabilities(name, values["WIRE"], values.get("CAPABILITIES", "")))
        if provider.api_key or name == "chatgpt":
            table[name] = provider
    return table


def unsupported(request, provider: Provider) -> str | None:
    """The one capability rule. It applies to the provider and to its fallback."""
    if request.images and not provider.images:
        return "The provider cannot show images"
    if request.web_search and not provider.web_search:
        return "The provider has no web search"
    if request.effort is not None and request.effort not in provider.efforts:
        return "The provider does not accept this effort"
    return None


def problem(request, table: dict[str, Provider]) -> str | None:
    """Give the reason why the table cannot serve this request, or None.

    The fallback must serve the whole request too: the loop can switch to it during the run.
    """
    provider = table.get(request.provider)
    if provider is None:
        return "The brain has no provider with this name"
    if (provider.name == "chatgpt") != (request.provider_auth.kind == "chatgpt"):
        return "Only the chatgpt provider uses a ChatGPT sign-in, and it needs one"
    if request.smoke and provider.wire != "responses":
        return "The smoke check needs the Responses wire"
    if request.fallback_provider is not None:
        fallback = table.get(request.fallback_provider)
        if provider.name != "chatgpt" or not fallback or fallback.wire != "responses" \
                or fallback.name == "chatgpt":
            return "The fallback needs the chatgpt provider and an API-key Responses provider"
        if reason := unsupported(request, fallback):
            return "The fallback: " + reason
    return unsupported(request, provider)


if __name__ == "__main__":
    print(json.dumps(description(load())))

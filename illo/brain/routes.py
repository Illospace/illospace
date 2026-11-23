"""Run kinds, routes, and payers. Each concept has one function here.

- `run_kind` classifies a run from the verified event channel and the authority.
- `resolve_route` gives the settings of a run kind: `default`, with the kind's route on top.
- `select_payer` gives who pays. It reads the authority and never changes it.
- `check_policy` and `check_routes` check the workspace, alone and against the brain's providers.
"""

from dataclasses import dataclass, field
from typing import get_args

from illo.brain.client import ProviderAuth
from illo.connectors import chatgpt
from illo.db import one
from illo.workspace import RunKind

# The limits of a `default` route that omits them.
DEFAULT_LIMITS = {"max_tool_calls": 12, "timeout_s": 300}


def run_kind(event, authority, *, deep: bool) -> RunKind:
    """A personal routine and a personal digest have the person_dm profile, not its route."""
    if deep:
        return "deep"
    if authority.tainted:
        return "tainted"
    channel, profile = event.channel, authority.profile
    if channel in {"slack", "email"} and profile in {"person_dm", "team_channel"}:
        return profile
    if channel == "routine":
        return "routine"
    if channel in {"digest", "team_digest"}:
        return "digest"
    if channel == "mcp":
        return "agent_request"
    if channel == "signals":
        return "tainted"
    raise ValueError(f"No run kind for the {channel} channel with the {profile} profile")


@dataclass(frozen=True)
class Route:
    kind: RunKind
    provider: str
    model: str
    effort: str | None
    max_tool_calls: int
    timeout_s: float
    max_output_tokens: int | None


def resolve_route(workspace, kind: RunKind) -> Route:
    routes = workspace.config.brain.routes
    fields = {**DEFAULT_LIMITS, "effort": None, "max_output_tokens": None,
              **routes["default"].model_dump(exclude_none=True)}
    if kind in routes:
        fields.update(routes[kind].model_dump(exclude_none=True))
    return Route(kind=kind, **fields)


@dataclass(frozen=True)
class Payer:
    """`provider` is the provider that runs: the route's, or the fallback before the run."""

    kind: str
    provider: str
    person_id: int | None = None
    person_key: str | None = None
    auth: ProviderAuth = field(default_factory=ProviderAuth, repr=False)


async def select_payer(ctx, route: Route, authority, *, force=False, rejected_token=None,
                       notify=True) -> Payer | None:
    """Give the payer, or None when no payer is allowed. A refresh in progress raises."""
    if route.provider != "chatgpt":
        return Payer("api_key", route.provider)
    policy = ctx.workspace.config.brain.chatgpt
    api_key = Payer("api_key", policy.fallback) if policy.fallback != "none" else None
    if authority.tainted or authority.profile in {"tainted", "outsider"}:
        if policy.tainted_credential == "none":
            await chatgpt.notify_team_missing(ctx, tainted=True)
            return None
        if policy.tainted_credential == "api_key":
            return api_key
    # Own mode: a person pays only for the person's own requests.
    own = (policy.person_credential == "own" and not authority.tainted
           and authority.profile in {"person_dm", "agent_request"})
    key = (authority.principal[7:]
           if own and authority.person_id is not None and authority.principal.startswith("person:")
           else policy.team_credential)
    async with ctx.pool.connection() as conn:
        person = await one(conn, "SELECT id,key FROM people WHERE key=%s AND active", (key,))
    # In own mode, a mismatched authority cannot borrow the team grant, even with a fallback.
    if own and (not person or person["id"] != authority.person_id
                or authority.principal != "person:" + key):
        return None
    auth = (await chatgpt.access(ctx, person, force=force, rejected_token=rejected_token,
                                 notify=notify) if person else None)
    if auth is not None:
        return Payer("chatgpt", "chatgpt", person["id"], person["key"], auth)
    if api_key:
        return api_key
    if person:
        async with ctx.pool.connection() as conn:
            pending = await one(
                conn,
                "SELECT 1 FROM connector_grants WHERE connector_key='chatgpt' AND person_id=%s AND broken_at IS NULL AND next_refresh_at>clock_timestamp()",
                (person["id"],),
            )
        if pending:
            raise chatgpt.TemporaryRefresh(chatgpt.TEMPORARY)
    if not own:
        await chatgpt.notify_team_missing(ctx)
    return None


def check_policy(brain, people: set[str]) -> None:
    """Check the routes and the ChatGPT policy without the brain."""
    policy = brain.chatgpt
    uses_chatgpt = any(route.provider == "chatgpt" for route in brain.routes.values())
    if "chatgpt" in brain.model_fields_set and not uses_chatgpt:
        raise ValueError("brain.chatgpt: no route uses the chatgpt provider")
    if uses_chatgpt and policy.team_credential is None:
        raise ValueError("brain.chatgpt.team_credential: ChatGPT needs an explicit person")
    if policy.team_credential is not None and policy.team_credential not in people:
        raise ValueError("brain.chatgpt.team_credential: unknown or inactive person")
    if policy.tainted_credential == "api_key" and policy.fallback == "none":
        raise ValueError("brain.chatgpt.tainted_credential: api_key needs brain.chatgpt.fallback")


def check_routes(workspace, providers: dict[str, dict]) -> None:
    """Check each route against the brain's providers. Raise ValueError with the reason."""
    for kind in get_args(RunKind):
        route = resolve_route(workspace, kind)
        provider = providers.get(route.provider)
        if provider is None:
            raise ValueError(f"brain.routes.{kind}: the brain has no provider {route.provider}")
        if route.effort is not None and route.effort not in provider["capabilities"]["efforts"]:
            raise ValueError(f"brain.routes.{kind}: {route.provider} does not accept the "
                             f"effort {route.effort}")
    fallback = workspace.config.brain.chatgpt.fallback
    if fallback != "none" and (
        fallback == "chatgpt" or providers.get(fallback, {}).get("wire") != "responses"
    ):
        raise ValueError("brain.chatgpt.fallback: use a provider with an API key on the "
                         "Responses wire")
    transcription = workspace.config.brain.transcription.provider
    if workspace.config.voice.transcription == "brain" and transcription not in providers:
        raise ValueError(f"brain.transcription: the brain has no provider {transcription}")

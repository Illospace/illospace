"""Pin every payer rule. Only the two helpers below know the call site and the config keys.

Each case gives the policy, the grants that exist, and the run's authority. The result is
the payer: the ChatGPT grant of one person, the API key, no payer, or a temporary refresh.
"""

import pytest

from illo.brain.run_manager import provider_auth
from illo.connectors.chatgpt import TemporaryRefresh
from illo.db import one
from illo.gateway.principal import Authority
from tests.chatgpt.conftest import save_grant

API_KEY = ("api_key", None)


def ada(_):
    return ("chatgpt", "ada")


def ben(_):
    return ("chatgpt", "ben")


def policy(ctx, *, chatgpt=True, fallback=False, **settings):
    """The call site of the policy. `chatgpt=False` is a route on an API-key provider."""
    brain = ctx.workspace.config.brain
    brain.runtime = "openai_responses"
    brain.auth = "chatgpt" if chatgpt else "api_key"
    brain.api_key_fallback = fallback
    for key, value in settings.items():
        setattr(brain, key, value)


async def payer(ctx, authority):
    """The call site of payer selection. Give (kind, person key), None, or "temporary"."""
    try:
        auth = await provider_auth(ctx, authority, "openai_responses")
    except TemporaryRefresh:
        return "temporary"
    if auth is None:
        return None
    if auth.kind == "api_key":
        return API_KEY
    return ("chatgpt", auth.access_token.get_secret_value().removesuffix("-token"))


def people(ids):
    """Authorities of the example people. `ids` maps a person key to its database ID."""
    return {
        "ada_dm": Authority("person:ada", "person_dm", ids["ada"]),
        "ben_dm": Authority("person:ben", "person_dm", ids["ben"]),
        "ada_agent": Authority("person:ada", "agent_request", ids["ada"]),
        # A personal routine and a personal digest have the person_dm profile.
        "ada_routine": Authority("person:ada", "person_dm", ids["ada"]),
        "ada_tainted_dm": Authority("person:ada", "person_dm", ids["ada"], tainted=True),
        "ada_as_ben": Authority("person:ada", "person_dm", ids["ben"]),
        "team_channel": Authority("team", "team_channel"),
        "team_routine": Authority("team", "routine"),
        "tainted": Authority("team", "tainted", tainted=True),
        "email_outsider": Authority("outsider", "tainted", None, True),
        "outsider": Authority("outsider", "outsider"),
    }


CASES = [
    # An API-key route pays with the API key for every authority.
    ("api_key_route_dm", {"chatgpt": False}, ["ada"], "ada_dm", API_KEY, None),
    ("api_key_route_tainted", {"chatgpt": False}, [], "tainted", API_KEY, None),
    ("api_key_route_outsider", {"chatgpt": False}, [], "outsider", API_KEY, None),
    # Own mode: a person pays for the person's own requests.
    ("own_dm", {"team_credential": "ben"}, ["ada", "ben"], "ada_dm", ada, None),
    ("own_agent", {"team_credential": "ben"}, ["ada", "ben"], "ada_agent", ada, None),
    ("own_personal_routine", {"team_credential": "ben"}, ["ada", "ben"], "ada_routine", ada, None),
    ("own_dm_missing", {"team_credential": "ben"}, ["ben"], "ada_dm", None, None),
    ("own_dm_missing_fallback", {"team_credential": "ben", "fallback": True}, ["ben"],
     "ada_dm", API_KEY, None),
    ("own_mismatch", {"team_credential": "ben"}, ["ada", "ben"], "ada_as_ben", None, None),
    ("own_mismatch_fallback", {"team_credential": "ben", "fallback": True}, ["ada", "ben"],
     "ada_as_ben", None, None),
    ("own_pending_refresh", {"team_credential": "ben"}, ["ada", "ada:pending"], "ada_dm",
     "temporary", None),
    ("own_pending_refresh_fallback", {"team_credential": "ben", "fallback": True},
     ["ada", "ada:pending"], "ada_dm", API_KEY, None),
    # Team runs use the configured team credential.
    ("team_channel", {"team_credential": "ada"}, ["ada", "ben"], "team_channel", ada, None),
    ("team_routine", {"team_credential": "ben"}, ["ada", "ben"], "team_routine", ben, None),
    ("team_missing_grant", {"team_credential": "ada"}, ["ben"], "team_channel", None, "team"),
    ("team_missing_grant_fallback", {"team_credential": "ada", "fallback": True}, ["ben"],
     "team_channel", API_KEY, None),
    ("team_no_credential", {"team_credential": None}, ["ada"], "team_routine", None, "team"),
    ("team_no_credential_fallback", {"team_credential": None, "fallback": True}, ["ada"],
     "team_routine", API_KEY, None),
    ("team_pending_refresh", {"team_credential": "ada"}, ["ada", "ada:pending"],
     "team_channel", "temporary", None),
    # Team mode: the team grant pays for every person.
    ("team_mode_dm", {"person_credential": "team", "team_credential": "ada"}, ["ada", "ben"],
     "ben_dm", ada, None),
    ("team_mode_agent", {"person_credential": "team", "team_credential": "ada"}, ["ada"],
     "ada_agent", ada, None),
    ("team_mode_missing", {"person_credential": "team", "team_credential": "ada"}, ["ben"],
     "ben_dm", None, "team"),
    ("team_mode_missing_fallback",
     {"person_credential": "team", "team_credential": "ada", "fallback": True}, ["ben"],
     "ben_dm", API_KEY, None),
    # A tainted run never uses a grant without the explicit team policy.
    ("tainted_none", {"team_credential": "ada"}, ["ada"], "tainted", None, "tainted"),
    ("tainted_none_fallback", {"team_credential": "ada", "fallback": True}, ["ada"], "tainted",
     None, "tainted"),
    ("tainted_person_dm_none", {"team_credential": "ben"}, ["ada", "ben"], "ada_tainted_dm",
     None, "tainted"),
    ("tainted_outsider_none", {"team_credential": "ada"}, ["ada"], "outsider", None, "tainted"),
    ("tainted_email_none", {"team_credential": "ada"}, ["ada"], "email_outsider", None,
     "tainted"),
    ("tainted_api_key", {"team_credential": "ada", "tainted_credential": "api_key",
                         "fallback": True}, ["ada"], "tainted", API_KEY, None),
    ("tainted_team", {"team_credential": "ada", "tainted_credential": "team"}, ["ada"],
     "tainted", ada, None),
    ("tainted_team_person_dm", {"team_credential": "ben", "tainted_credential": "team"},
     ["ada", "ben"], "ada_tainted_dm", ben, None),
    ("tainted_team_outsider", {"team_credential": "ada", "tainted_credential": "team"},
     ["ada"], "outsider", ada, None),
    ("tainted_team_missing", {"team_credential": "ada", "tainted_credential": "team"}, [],
     "tainted", None, "team"),
    ("tainted_team_mode", {"person_credential": "team", "team_credential": "ada"}, ["ada"],
     "tainted", None, "tainted"),
]


@pytest.mark.parametrize(
    "settings,grants,who,expected,notice", [case[1:] for case in CASES], ids=[c[0] for c in CASES]
)
async def test_payer(chatgpt_app, settings, grants, who, expected, notice):
    ctx = chatgpt_app[0].state.ctx
    policy(ctx, **settings)
    for grant in grants:
        key, _, state = grant.partition(":")
        person = await save_grant(ctx, key, access=key + "-token")
        if state == "pending":
            async with ctx.pool.connection() as conn:
                await conn.execute(
                    "UPDATE connector_grants SET expires_at=clock_timestamp(),"
                    "next_refresh_at=clock_timestamp()+interval '1 hour' WHERE person_id=%s",
                    (person["id"],),
                )
    async with ctx.pool.connection() as conn:
        ids = {
            row["key"]: row["id"]
            for row in await (await conn.execute("SELECT id,key FROM people")).fetchall()
        }
    result = await payer(ctx, people(ids)[who])
    assert result == (expected(None) if callable(expected) else expected)
    async with ctx.pool.connection() as conn:
        for key in ("team", "tainted"):
            sensor = await one(conn, "SELECT 1 FROM sensors WHERE key=%s", ("chatgpt:" + key,))
            assert bool(sensor) == (notice == key), key

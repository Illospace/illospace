"""The Slack commands need no database connection."""

import pytest

from illo.channels.slack.cli import execute
from illo.cli import execute as execute_main
from illo.cli import parser
from illo.config import Config


async def test_whoami(slack_client, capsys):
    assert await execute("whoami", Config(), slack_client) == 0
    assert capsys.readouterr().out == "U_ACME_ILLO  Illo  T_ACME\n"


async def test_users_pagination(slack_client, capsys):
    cursors = []

    async def users_list(**kwargs):
        cursors.append(kwargs["cursor"])
        if kwargs["cursor"] is None:
            return {
                "members": [
                    {
                        "id": "U_ACME_ADA",
                        "profile": {"real_name": "Ada", "email": "ada@example.com"},
                    }
                ],
                "response_metadata": {"next_cursor": "page2"},
            }
        return {
            "members": [{"id": "U_ACME_BEN", "name": "Ben"}, {"id": "U_DELETED", "deleted": True}]
        }

    slack_client.users_list = users_list
    await execute("users", Config(), slack_client)
    assert cursors == [None, "page2"]
    assert capsys.readouterr().out == "U_ACME_ADA  Ada  ada@example.com\nU_ACME_BEN  Ben  \n"


async def test_cli_dispatch_without_database(monkeypatch):
    async def fake_execute(action, config):
        assert action == "users"
        return 0

    monkeypatch.setattr("illo.channels.slack.cli.execute", fake_execute)
    assert await execute_main(parser().parse_args(["slack", "users"]), Config()) == 0


async def test_cli_requires_bot_token():
    with pytest.raises(ValueError, match="ILLO_SLACK_BOT_TOKEN"):
        await execute("whoami", Config())

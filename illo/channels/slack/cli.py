"""Read Slack identities to help a person complete people.yaml."""

from slack_sdk.web.async_client import AsyncWebClient

from illo.config import Config


async def execute(action: str, config: Config, client: AsyncWebClient | None = None) -> int:
    if not config.slack_bot_token and client is None:
        raise ValueError("Set ILLO_SLACK_BOT_TOKEN before this command")
    owned = client is None
    client = client or AsyncWebClient(token=config.slack_bot_token.get_secret_value())
    try:
        if action == "whoami":
            identity = await client.auth_test()
            print(f"{identity['user_id']}  {identity.get('user', 'Illo')}  {identity['team_id']}")
        else:
            cursor = None
            while True:
                response = await client.users_list(limit=200, cursor=cursor)
                for member in response.get("members", []):
                    if member.get("deleted"):
                        continue
                    profile = member.get("profile", {})
                    name = (
                        profile.get("real_name")
                        or member.get("real_name")
                        or member.get("name", "")
                    )
                    print(f"{member['id']}  {name}  {profile.get('email', '')}")
                cursor = response.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
    finally:
        if owned and client.session:
            await client.session.close()
    return 0

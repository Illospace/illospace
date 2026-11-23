"""A model cannot change or cancel a person's sign-in."""


async def test_u07_model_has_only_signin_status(app):
    run = {
        "profile": "person_dm",
        "principal": "person:ada",
        "tainted": False,
        "personal_read": False,
    }
    names = {t["name"] for t in app.state.ctx.registry.list_tools(run)}
    assert "chatgpt_status" in names
    assert not names & {"chatgpt_connect", "chatgpt_disconnect"}

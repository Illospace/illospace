"""The prompt lists skills by name. The tool `skills_get` loads the steps."""

from illo.ledger import people, runs


async def _token(app, profile, principal="person:ada", tainted=False):
    async with app.state.ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        run = await runs.create(
            conn,
            principal=principal,
            person_id=ada["id"] if principal.startswith("person:") else None,
            profile=profile,
            tainted=tainted,
            runtime="fake",
            model="fake",
        )
        return await runs.mint_token(conn, run["id"], 60)


async def test_skills_get_returns_the_steps(app, rpc):
    token = await _token(app, "person_dm")
    result = (
        await rpc(
            token, "tools/call", {"name": "skills_get", "arguments": {"name": "release-check"}}
        )
    ).json()["result"]
    content = result["structuredContent"]["result"]
    assert content["found"] and "Illo does not release" in content["steps"]


async def test_unknown_skill_lists_the_names(app, rpc):
    token = await _token(app, "person_dm")
    result = (
        await rpc(token, "tools/call", {"name": "skills_get", "arguments": {"name": "nope"}})
    ).json()["result"]
    assert result["structuredContent"]["result"] == {"found": False, "names": ["release-check"]}


async def test_tainted_run_has_skills_and_outsider_does_not(app, rpc):
    tainted = await _token(app, "tainted", principal="team", tainted=True)
    outsider = await _token(app, "tainted", principal="outsider", tainted=True)
    for token, expected in ((tainted, True), (outsider, False)):
        names = {t["name"] for t in (await rpc(token, "tools/list")).json()["result"]["tools"]}
        assert ("skills_get" in names) is expected

"""Memory tests use a temporary workspace and a local git history."""

import os
import shutil
import subprocess

import pytest

from illo.db import one
from illo.ledger import people, runs
from illo.tools.registry import ToolContext
from illo.workspace import load_workspace


def git(root, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


@pytest.fixture
async def memory_ctx(app, tmp_path):
    ctx = app.state.ctx
    root = tmp_path / "workspace"
    shutil.copytree(ctx.workspace.root, root)
    ctx.config.workspace_dir = root
    ctx.workspace = load_workspace(root)
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Example")
    git(root, "config", "user.email", "example@example.invalid")
    git(root, "add", ".")
    git(root, "commit", "-m", "Start the test workspace")
    return ctx


@pytest.fixture
def call(memory_ctx):
    async def invoke(name, args, *, person="ada", profile="team_channel", tainted=False,
                     personal=False, run=None):
        async with memory_ctx.pool.connection() as conn:
            acting = await people.get(conn, person) if person else None
            if run is None:
                event = await one(conn, """INSERT INTO events(channel,external_id,kind,payload)
                    VALUES ('slack',gen_random_uuid()::text,'message',jsonb_build_object('task_actor',%s::text)) RETURNING *""", (person,))
                run = await runs.create(conn, principal="outsider" if profile == "outsider" else
                    "person:" + person if profile in {"person_dm", "agent_request"} else "team",
                    person_id=acting["id"] if profile in {"person_dm", "agent_request"} else None,
                    profile="tainted" if profile == "outsider" else profile, tainted=tainted,
                    runtime="fake", model="fake", event_id=event["id"])
            if personal:
                await conn.execute("UPDATE runs SET personal_read=true WHERE id=%s", (run["id"],))
                run["personal_read"] = True
            result = await memory_ctx.registry.call(ToolContext(conn, run, memory_ctx), name, args)
        return result, run
    return invoke

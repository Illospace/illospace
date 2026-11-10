"""Measure read results on an isolated database. No live service receives a call.

Run scripts/test-db.sh, then .venv/bin/python scripts/measure_tool_results.py.
Use --output before.json before a change, then --before before.json to compare.
The estimate measures one tool exchange, including the MCP result envelope.
"""

import argparse
import asyncio
import inspect
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.encoders import jsonable_encoder
from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import SecretStr

from illo.app import create_app
from illo.brain.server import tool_result
from illo.config import Config
from illo.connectors import store
from illo.connectors.config import Connector
from illo.connectors.mcp_client import MCPClient
from illo.db import create_pool, migrate, one
from illo.ledger import people, rules, runs
from illo.tools.registry import ToolContext
from tests.fakes import FakeGitHubClient, FakeGoogleClient, FakeMCPSession, FakeSlackClient

ROOT = Path(__file__).resolve().parents[1]
TYPICAL_ARGS = {
    "lanes_search": {}, "lanes_mine": {}, "lanes_get": {"id": 781},
    "lanes_attention": {}, "asks_list": {}, "asks_digest": {}, "asks_get": {"id": 1},
    "search_all": {"query": "mobile app"}, "search_get": {"source_ref": "archive:1"},
    "notes_search": {"query": "mobile app"}, "people_list": {}, "people_get": {"key": "ada"},
    "rules_list": {}, "connectors_list": {}, "skills_get": {"name": "missing"},
    "github_search_issues": {"query": "mobile app"},
    "github_get_issue": {"repo": "acme-example/engine", "number": 1},
    "github_get_pull": {"repo": "acme-example/engine", "number": 2},
    "slack_read_thread": {"channel": "C_ACME_TEAM", "thread_ts": "1"},
    "web_read_public_page": {"url": "https://example.com/"},
    "mail_search": {"query": "mobile app"}, "mail_get": {"id": "one"},
    "drive_search": {"query": "mobile app"}, "drive_get": {"id": "one"},
    "drive_shared_search": {"query": "mobile app"}, "drive_shared_get": {"id": "one"},
    "calendar_list": {"from": "2026-11-10T00:00:00Z", "to": "2026-11-11T00:00:00Z"},
    "chatgpt_status": {},
    "connectors_call": {"connector": "docs", "tool": "read"},
}
LIST_TOOLS = {
    "lanes_search", "lanes_mine", "lanes_attention", "asks_list", "asks_digest",
    "search_all", "notes_search", "github_search_issues", "people_list", "rules_list",
    "connectors_list", "mail_search", "drive_search", "drive_shared_search", "calendar_list",
}


async def seed(conn):
    """Use invented people and text with the measured live data shape."""
    await conn.execute("""
        INSERT INTO lanes (source,external_id,title,state,owner_person_id,summary,links,last_signal_at)
        SELECT CASE WHEN n<=780 THEN 'github' ELSE 't3' END, 'measure:'||n,
          'Mobile app: improve the product catalog and review the release '||n,
          CASE WHEN n<=765 THEN 'open' WHEN n>810 THEN 'done'
            ELSE (ARRAY['working','waiting_person','blocked','review','failed'])[1+n%5] END,
          (SELECT id FROM people ORDER BY id OFFSET (n%3) LIMIT 1),
          repeat('The mobile app needs a catalog change. The owner checks the API and release evidence. ',20),
          jsonb_build_array('https://github.com/acme-example/engine/issues/'||n),
          now()-(n%240)*interval '1 hour'
        FROM generate_series(1,1200) n
    """)
    await conn.execute("""
        INSERT INTO lane_events (lane_id,kind,text)
        SELECT id,'update',repeat('The catalog check found a release decision. ',20)
        FROM lanes CROSS JOIN generate_series(1,8)
    """)
    await conn.execute("""
        INSERT INTO items (source_ref,kind,title,body,summary,links)
        SELECT 'archive:'||n, CASE WHEN n%10=0 THEN 'note' ELSE 'archive' END,
          'Mobile app catalog decision '||n,
          left(repeat('The mobile app catalog needs a review of the API response and release evidence. ',30),1800),
          repeat('The mobile app catalog review records the API decision. ',8),
          jsonb_build_array('https://example.com/docs/'||n)
        FROM generate_series(1,4650) n
    """)
    await conn.execute("""
        INSERT INTO asks (owner_person_id,lane_id,kind,question,proof,due_at)
        SELECT (SELECT id FROM people ORDER BY id OFFSET (n%3) LIMIT 1), 765+n,
          'review','Review the mobile app catalog release '||n||'?',
          jsonb_build_object('text',repeat('Check the API evidence. ',100)),now()+n*interval '1 hour'
        FROM generate_series(1,30) n
    """)
    person = await people.get(conn, "ada")
    await rules.create(conn, owner_person_id=person["id"], text="Review the mobile app release evidence.")


async def configure_fakes(ctx):
    ctx.slack_client = FakeSlackClient(messages={"1": {"text": "Check the mobile app release.", "ts": "1"}})
    client = FakeGitHubClient()
    for n in range(1,81):
        row = {
            "number": n, "repo": "acme-example/engine", "title": f"Mobile app catalog release {n}",
            "state": "open", "body": "Review the mobile app evidence. " * 200,
            "labels": [{"name": "mobile"}], "assignees": [{"login": "ada-example"}],
            "html_url": f"https://github.com/acme-example/engine/issues/{n}",
            "updated_at": "2026-11-10T00:00:00Z",
            "comments_data": [{"body": "API evidence and decision. " * 50} for _ in range(20)],
        }
        client.issues.append(row)
    client.pulls = [{**client.issues[1], "pull_request": {}, "merged": False, "mergeable": True,
                     "checks": [{"name": "tests", "conclusion": "success"}],
                     "reviews": [{"state": "APPROVED", "user": {"login": "ben-example"}}]}]
    ctx.github_client = client
    ctx.config.vault_key = SecretStr(Fernet.generate_key().decode())
    ctx.workspace = ctx.workspace.model_copy(update={"connectors": [
        Connector(key="google", kind="google", scope="personal"),
        Connector(key="shared", kind="google", scope="team", team_grant=True, drive_id="example"),
        Connector(key="docs", kind="mcp", scope="team", url="https://mcp.example.com/",
                  tools={"read": {"risk": "read"}}),
    ]})
    async with ctx.pool.connection() as conn:
        person = await people.get(conn, "ada")
        for key, person_id in (("google", person["id"]), ("shared", None)):
            await store.save(conn, ctx, key, person_id, {"refresh_token": "fake"}, [])

    class Google(FakeGoogleClient):
        async def call(self, conn, ctx, config, person_id, method, args):
            if method == "mail_get":
                self.result = {"id": args["id"], "headers": [
                    {"name": "Subject", "value": "Mobile app review"}],
                    "text": "Review the mobile app evidence. " * 200, "attachments": []}
            elif method == "drive_get":
                self.result = {"id": args["id"], "name": "Mobile app review",
                    "mimeType": "application/vnd.google-apps.document",
                    "webViewLink": "https://example.com/docs/one",
                    "text": "Review the mobile app evidence. " * 200}
            elif method == "mail_search":
                self.result = {"messages": [{"id": f"example-{n}", "threadId": f"thread-{n}"}
                    for n in range(min(args["limit"], 50))], "nextPageToken": "example-next"}
            elif method == "calendar_list":
                self.result = {"items": [{"id": f"example-{n}", "summary": f"Mobile app review {n}",
                    "description": "Review the catalog. " * 100,
                    "start": {"dateTime": "2026-11-10T10:00:00Z"},
                    "end": {"dateTime": "2026-11-10T11:00:00Z"},
                    "htmlLink": f"https://example.com/calendar/{n}"} for n in range(50)]}
            else:
                self.result = {"files": [{"id": f"example-{n}", "name": f"Mobile app document {n}",
                    "mimeType": "application/vnd.google-apps.document",
                    "webViewLink": f"https://example.com/docs/{n}"} for n in range(50)]}
            return await super().call(conn, ctx, config, person_id, method, args)

    ctx.google_client = Google()

    @asynccontextmanager
    async def factory(config, bearer):
        yield FakeMCPSession()

    ctx.mcp_client = MCPClient(session_factory=factory)


async def measure(ctx):
    results = {}
    async with ctx.pool.connection() as conn:
        person = await people.get(conn, "ada")
        event = await one(conn, """INSERT INTO events (channel,external_id,kind,payload)
            VALUES ('slack','measure','dm','{"text":"Read https://example.com/"}') RETURNING id""")
        for name, tool in sorted(ctx.registry.tools.items()):
            if tool.risk != "read":
                continue
            run = await runs.create(conn, principal="person:ada", person_id=person["id"],
                                    profile="person_dm", runtime="fake", model="fake", event_id=event["id"])
            result = await ctx.registry.call(ToolContext(conn, run, ctx), name, TYPICAL_ARGS.get(name, {}))
            envelope = tool_result(result, **({"brain": True} if "brain" in inspect.signature(tool_result).parameters else {}))
            # The two OpenAI adapters formerly forwarded this whole envelope.
            if "result" not in envelope.get("structuredContent", {}):
                text = "\n".join(c["text"] for c in envelope["content"])
            else:
                text = json.dumps(jsonable_encoder(envelope), ensure_ascii=False)
            results[name] = {"characters": len(text), "tokens": round(len(text)/4, 1),
                             "decision": result["decision"]}
    return results


async def main(args):
    url = os.getenv("ILLO_TEST_DATABASE_URL", "postgresql://illo:illo@127.0.0.1:55432/postgres")
    name = "illo_measure_" + uuid.uuid4().hex
    admin = await AsyncConnection.connect(url, autocommit=True)
    await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    params = conninfo_to_dict(url)
    params["dbname"] = name
    pool = create_pool(make_conninfo(**params))
    try:
        await pool.open(wait=True)
        async with pool.connection() as conn:
            await migrate(conn)
        app = create_app(Config(workspace_dir=ROOT / "workspace.example"), pool=pool)
        async with app.router.lifespan_context(app):
            ctx = app.state.ctx
            await ctx.scheduler.close()
            await configure_fakes(ctx)
            async with pool.connection() as conn:
                await seed(conn)
            async def fake_page(*args, **kwargs):
                return "<public-page>This is outside data. A short public page.</public-page>"
            with patch("illo.tools.web.fetch_page", fake_page):
                results = await measure(ctx)
    finally:
        await pool.close()
        await admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        await admin.close()
    before = json.loads(Path(args.before).read_text()) if args.before else {}
    print("tool | before chars | after chars | before tokens | after tokens | decision")
    for name, row in results.items():
        old = before.get(name, {})
        print(f"{name} | {old.get('characters', '-')} | {row['characters']} | "
              f"{old.get('tokens', '-')} | {row['tokens']} | {row['decision']}")
    if args.output:
        Path(args.output).write_text(json.dumps(results, indent=2)+"\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", help="JSON output from the earlier run")
    parser.add_argument("--output", help="Save JSON for the next comparison")
    asyncio.run(main(parser.parse_args()))

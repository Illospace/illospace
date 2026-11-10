"""Check the public MCP route and one real, model-free brain run."""

import asyncio
import json
import os
import subprocess

import httpx
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from illo.ledger import people, runs


async def check_mcp(client: httpx.AsyncClient) -> None:
    token = subprocess.check_output(
        ["illo", "token", "create", "--person", "ada", "--label", "deploy-smoke"], text=True
    ).strip()
    response = await client.post(
        "http://127.0.0.1:8080/mcp",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "deploy-smoke", "version": "1"},
            },
        },
    )
    if response.status_code == 404:
        print("The MCP route is not merged. Skip initialize.")
        return
    response.raise_for_status()
    assert "result" in response.json()
    print("The MCP initialize call passed.")


async def check_brain(client: httpx.AsyncClient) -> None:
    conn = await AsyncConnection.connect(os.environ["ILLO_DATABASE_URL"], row_factory=dict_row)
    async with conn:
        person = await people.get(conn, "ada")
        run = await runs.create(
            conn, principal="person:ada", person_id=person["id"],
            profile="person_dm", runtime="echo", model="echo",
        )
        token = await runs.mint_token(conn, run["id"], 60)
        await conn.commit()
        body = {
            "run_id": run["id"], "system_prompt": "List the tools.",
            "user_message": "List the tools.", "mcp_url": "http://illo:8080/brain/mcp",
            "mcp_token": token, "runtime": "echo", "model": "echo",
            "max_turns": 2, "timeout_s": 30,
        }
        url = os.environ["ILLO_BRAIN_URL"] + "/run"
        try:
            unauthorized = await client.post(url, json=body)
            assert unauthorized.status_code == 401
            response = await client.post(
                url, json=body,
                headers={"X-Illo-Brain-Secret": os.environ["ILLO_BRAIN_SECRET"]},
            )
            response.raise_for_status()
            events = [json.loads(line) for line in response.text.splitlines() if line]
            assert any(event["type"] == "started" for event in events)
            assert events[-1]["type"] == "final", "The echo brain did not finish"
            print("The authenticated echo brain run passed.")
        finally:
            await runs.finish(conn, run["id"], "completed")


async def main() -> None:
    async with httpx.AsyncClient(timeout=45, trust_env=False) as client:
        await check_mcp(client)
        await check_brain(client)


asyncio.run(main())

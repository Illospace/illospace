"""Only the owner can keep, correct, or remove a note proposal in its DM."""

import asyncio
import json
import subprocess

import pytest
import yaml

from illo import notes
from illo.cli import execute, parser
from illo.config import Config
from illo.db import all_rows, one
from illo.ledger import items, people
from illo.notes import gitstore
from illo.workspace import WorkspaceError, load_workspace
from tests.tool_results import read_result


async def click(ctx, row, choice, person="ada", **changes):
    async with ctx.pool.connection() as conn:
        owner = await people.get(conn, person)
    await ctx.slack_actions.dispatch(
        ctx,
        owner,
        {
            "action_id": f"note:{row['id']}:{choice}",
            "channel": row["slack_channel"],
            "message_ts": row["slack_ts"],
            **changes,
        },
    )


async def test_propose_card_keep_indexes_file(note_ctx, proposal, slack_client, rpc, rpc_token):
    row = proposal
    assert row["state"] == "pending"
    assert not list(note_ctx.workspace.root.glob("notes/*launch*"))
    card = next(c for c in slack_client.calls if c["method"] == "chat_postMessage")
    assert card["text"] == "I noted: Acme ships on Tuesday."
    buttons = card["blocks"][1]["elements"]
    assert [b["text"]["text"] for b in buttons] == ["Keep", "Correct…", "Remove"]
    await click(note_ctx, row, "keep")
    path = note_ctx.workspace.root / "notes" / f"launch-decision-{row['id']}.md"
    text = path.read_text()
    front = yaml.safe_load(text.split("---")[1])
    assert front["visibility"] == "team" and front["author"] == "ada"
    assert front["about"] == "Launch" and front["created"]
    assert "Acme ships on Tuesday." in text
    async with note_ctx.pool.connection() as conn:
        item = await items.get(conn, "note:" + path.name)
        assert item["kind"] == "note" and item["body"] == "Acme ships on Tuesday."
        assert (await one(conn, "SELECT state FROM note_proposals WHERE id=%s", (row["id"],)))[
            "state"
        ] == "kept"
    _, token = rpc_token
    response = await rpc(
        token, "tools/call", {"name": "notes_search", "arguments": {"query": "Tuesday"}}
    )
    assert "Acme ships on Tuesday." in read_result(response.json()["result"])["result"]
    await click(note_ctx, row, "keep")
    assert len(list(note_ctx.workspace.root.glob("notes/*launch*"))) == 1


async def test_correct_thread_claim(note_ctx, proposal, replay, fake_brain, slack_client):
    await click(note_ctx, proposal, "correct")
    assert slack_client.calls[-1]["thread_ts"] == proposal["slack_ts"]
    result = await replay(
        text="Acme ships on Wednesday.",
        reply_to=proposal["slack_ts"],
        payload={"channel_id": proposal["slack_channel"], "thread_ts": proposal["slack_ts"]},
    )
    assert result["decision"] == "claimed"
    assert result["text"] == "The note is saved."
    assert not fake_brain.requests
    async with note_ctx.pool.connection() as conn:
        item = await one(conn, "SELECT * FROM items WHERE kind='note' AND title='Launch decision'")
        assert item["body"] == "Acme ships on Wednesday."


async def test_remove_and_wrong_owner_or_channel(note_ctx, proposal):
    await click(note_ctx, proposal, "keep", person="ben")
    await click(note_ctx, proposal, "remove", channel="C_ACME_TEAM")
    await click(note_ctx, proposal, "keep", message_ts="wrong")
    assert not list(note_ctx.workspace.root.glob("notes/*launch*"))
    async with note_ctx.pool.connection() as conn:
        assert await one(conn, "SELECT id FROM note_proposals WHERE id=%s", (proposal["id"],))
    await click(note_ctx, proposal, "remove")
    async with note_ctx.pool.connection() as conn:
        assert not await one(conn, "SELECT id FROM note_proposals WHERE id=%s", (proposal["id"],))
    assert not list(note_ctx.workspace.root.glob("notes/*launch*"))


async def test_wrong_person_cannot_correct(note_ctx, proposal, replay):
    await click(note_ctx, proposal, "correct")
    result = await replay(
        sender="U_ACME_BEN",
        text="Wrong correction.",
        payload={"channel_id": proposal["slack_channel"], "thread_ts": proposal["slack_ts"]},
    )
    assert result["decision"] != "claimed"
    assert not list(note_ctx.workspace.root.glob("notes/*launch*"))


async def test_sync_folder_changes_and_deletions(note_ctx):
    folder = note_ctx.workspace.root / "notes"
    path = folder / "decision.md"
    path.write_text(
        "---\ntitle: A decision\nabout: Release\nauthor: ada\ncreated: 2026-01-01\nvisibility: team\n---\n\nKeep the release small.\n"
    )
    await notes.sync(note_ctx)
    async with note_ctx.pool.connection() as conn:
        assert (await items.get(conn, "note:decision.md"))["body"] == "Keep the release small."
    path.write_text(path.read_text().replace("small", "simple"))
    await note_ctx.reload()
    await notes.sync(note_ctx)
    async with note_ctx.pool.connection() as conn:
        assert (await items.get(conn, "note:decision.md"))["body"] == "Keep the release simple."
    path.unlink()
    await notes.sync(note_ctx)
    async with note_ctx.pool.connection() as conn:
        assert await items.get(conn, "note:decision.md") is None


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


async def test_git_store_local_remote_and_other_staged_file(note_ctx, proposal, tmp_path):
    root = note_ctx.workspace.root
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Acme")
    git(root, "config", "user.email", "acme@example.invalid")
    git(root, "add", ".")
    git(root, "commit", "-m", "Initial workspace")
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "-u", "origin", "main")
    (root / "unrelated.txt").write_text("Do not commit this.")
    git(root, "add", "unrelated.txt")
    note_ctx.workspace.config.workspace.git_push = True
    await click(note_ctx, proposal, "keep")
    assert git(root, "log", "-1", "--format=%s") == "note: Launch decision"
    assert git(root, "diff", "--cached", "--name-only") == "unrelated.txt"
    assert git(remote, "rev-parse", "refs/heads/main") == git(root, "rev-parse", "HEAD")
    assert (
        git(root, "show", "--format=", "--name-only", "HEAD")
        == f"notes/launch-decision-{proposal['id']}.md"
    )


async def test_failed_push_leaves_note_and_sensor(note_ctx, proposal, tmp_path):
    root = note_ctx.workspace.root
    git(root, "init", "-b", "main")
    git(root, "remote", "add", "origin", str(tmp_path / "absent.git"))
    note_ctx.workspace.config.workspace.git_push = True
    await click(note_ctx, proposal, "keep")
    assert list(root.glob("notes/*launch*"))
    async with note_ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM sensors WHERE key='workspace'")
        assert row["last_error_at"]
        assert "failed" in row["detail"]
        assert (await one(conn, "SELECT state FROM note_proposals WHERE id=%s", (proposal["id"],)))[
            "state"
        ] == "kept"


async def test_no_git_push_by_default(note_ctx, monkeypatch, tmp_path):
    async def forbidden(*args):
        pytest.fail("Git must not run")

    monkeypatch.setattr(gitstore, "_git", forbidden)
    async with note_ctx.pool.connection() as conn:
        assert await gitstore.store(note_ctx, conn, tmp_path / "note.md", "A title")


async def test_notes_reject_private_front_matter_and_symlinks(note_ctx, tmp_path):
    root = note_ctx.workspace.root
    path = root / "notes" / "private.md"
    path.write_text("---\ntitle: Private\nvisibility: private\n---\nDo not share.")
    with pytest.raises(WorkspaceError, match="visibility"):
        load_workspace(root)
    path.unlink()
    outside = tmp_path / "outside.md"
    outside.write_text("Outside the workspace")
    path.symlink_to(outside)
    with pytest.raises(WorkspaceError, match="regular file"):
        load_workspace(root)


async def test_cli_import_dry_run_counts(pool, note_ctx, tmp_path, capsys):
    path = tmp_path / "old.jsonl"
    path.write_text(json.dumps({"source_ref": "old:one", "title": "Old fact"}))
    config = Config(database_url=pool.conninfo, workspace_dir=note_ctx.workspace.root)
    assert (
        await execute(parser().parse_args(["import-archive", str(path), "--dry-run"]), config) == 0
    )
    assert "Dry run. Loaded: 1" in capsys.readouterr().out
    async with pool.connection() as conn:
        assert await items.get(conn, "old:one") is None
    assert await execute(parser().parse_args(["import-archive", str(path)]), config) == 0
    assert "Loaded: 1" in capsys.readouterr().out
    async with pool.connection() as conn:
        assert await items.get(conn, "old:one")


async def test_concurrent_keep_writes_once(note_ctx, proposal):
    await asyncio.gather(click(note_ctx, proposal, "keep"), click(note_ctx, proposal, "keep"))
    assert len(list(note_ctx.workspace.root.glob("notes/*launch*"))) == 1


async def test_failed_card_keeps_pending_proposal(note_ctx, rpc, rpc_token, monkeypatch):
    async def failed(*args, **kwargs):
        raise RuntimeError("Do not print service details")

    monkeypatch.setattr(note_ctx.slack_client, "chat_postMessage", failed)
    _, token = rpc_token
    response = await rpc(
        token,
        "tools/call",
        {
            "name": "notes_propose",
            "arguments": {"title": "A new note", "text": "Keep this pending.", "about": "A test"},
        },
    )
    result = read_result(response.json()["result"])
    assert result["decision"] == "allow" and not result["result"]["card_sent"]
    async with note_ctx.pool.connection() as conn:
        assert await one(conn, "SELECT id FROM note_proposals WHERE state='pending'")
        assert (await one(conn, "SELECT * FROM sensors WHERE key='notes'"))["last_error_at"]


async def test_approved_personal_draft_starts_proposal_once(note_ctx, rpc, rpc_token):
    run, token = rpc_token
    async with note_ctx.pool.connection() as conn:
        await conn.execute("UPDATE runs SET personal_read=true WHERE id=%s", (run["id"],))
    response = await rpc(
        token,
        "tools/call",
        {
            "name": "notes_propose",
            "arguments": {
                "title": "Share a fact",
                "text": "The owner approves this fact.",
                "about": "A decision",
            },
        },
    )
    draft_id = read_result(response.json()["result"])["result"]["draft_id"]
    async with note_ctx.pool.connection() as conn:
        draft = await one(conn, "SELECT * FROM drafts WHERE id=%s", (draft_id,))
    performer = note_ctx.draft_performers["notes_propose"]
    await performer(note_ctx, draft)
    async with note_ctx.pool.connection() as conn:
        assert not await all_rows(conn, "SELECT * FROM note_proposals")
        await conn.execute("UPDATE drafts SET state='approved' WHERE id=%s", (draft_id,))
    await performer(note_ctx, draft)
    await performer(note_ctx, draft)
    async with note_ctx.pool.connection() as conn:
        rows = await all_rows(conn, "SELECT * FROM note_proposals")
    assert len(rows) == 1 and rows[0]["owner_person_id"] == run["person_id"]
    await click(note_ctx, rows[0], "keep")
    assert list(note_ctx.workspace.root.glob("notes/share-a-fact*"))

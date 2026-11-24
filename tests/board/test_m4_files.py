"""Board pictures keep their post's access rules and lifetime."""

import asyncio
from pathlib import Path

import pytest
from psycopg.errors import CheckViolation
from psycopg.types.json import Jsonb

from illo.db import migrate, one
from illo.ledger import asks, board, people
from illo.tasks.pages import images, outside
from tests.asks.test_block_pictures import BLOCK, PNG
from tests.asks.test_block_pictures import picture_transport as picture_transport
from tests.board.test_board import TICKET

MIGRATIONS = Path(__file__).resolve().parents[2] / 'illo/migrations'
ADD = '1904_board_files.sql'
VALIDATE = '1905_board_files_validate.sql'
LINKS = {'kind': 'links', 'items': [{'title': 'Report', 'url': 'https://example.com/report'}]}
MIRROR = {'kind': 'image', 'image': {'repo': 'example/project', 'ref': 'main', 'path': 'picture.png'}}


async def test_m4_upgrade_and_m3_writes(pool, tmp_path):
    for file in MIGRATIONS.glob('*.sql'):
        if file.name < ADD:
            (tmp_path / file.name).symlink_to(file)
    async with pool.connection() as conn:
        try:
            await conn.execute('CREATE SCHEMA m4_upgrade')
            await conn.execute('SET search_path TO m4_upgrade')
            assert (await migrate(conn, tmp_path))[-1] == '1903_board_coordinator_validate.sql'
            task = await one(conn, """INSERT INTO tasks(list_key,title,brief,created_by)
                VALUES ('bugs','Old task','','person:ada') RETURNING id""")
            person = await one(conn, "INSERT INTO people(key,name) VALUES ('ada','Ada') RETURNING id")
            ask = await asks.create(conn, owner_person_id=person['id'], question='Old ask?')
            thread = await one(conn, "INSERT INTO board_threads(title,created_by) VALUES ('Old thread','agent:a') RETURNING id")
            post = await one(conn, """INSERT INTO board_posts(thread_id,actor,kind,text)
                VALUES (%s,'agent:a','message','Old post') RETURNING id""", (thread['id'],))
            old_write = 'INSERT INTO page_files(task_id,ask_id,position,media_type,data) VALUES (%s,%s,0,%s,%s)'
            await conn.execute(old_write, (task['id'], None, 'image/png', PNG))
            await conn.commit()

            (tmp_path / ADD).symlink_to(MIGRATIONS / ADD)
            assert await migrate(conn, tmp_path) == [ADD]
            check = """SELECT convalidated FROM pg_constraint
                WHERE conrelid='page_files'::regclass AND conname='page_files_one_owner'"""
            assert (await one(conn, check))['convalidated'] is False
            await outside.replace(conn, [(0, 'image/png', PNG)], board_post_id=post['id'])
            await conn.commit()
            (tmp_path / VALIDATE).symlink_to(MIGRATIONS / VALIDATE)
            assert await migrate(conn, tmp_path) == [VALIDATE]
            assert (await one(conn, check))['convalidated'] is True

            # The previous release takes this migration path and writes the original two owners.
            (tmp_path / ADD).unlink()
            (tmp_path / VALIDATE).unlink()
            assert await migrate(conn, tmp_path) == []
            await conn.execute('DELETE FROM page_files WHERE task_id=%s OR ask_id=%s', (task['id'], ask['id']))
            await conn.execute(old_write, (task['id'], None, 'image/png', b'Task picture'))
            await conn.execute(old_write, (None, ask['id'], 'image/png', b'Ask picture'))
            assert (await outside.read(conn, 0, board_post_id=post['id']))['data'] == PNG
            with pytest.raises(CheckViolation):
                async with conn.transaction():
                    await conn.execute(old_write, (task['id'], ask['id'], 'image/png', PNG))
            with pytest.raises(CheckViolation):
                async with conn.transaction():
                    await conn.execute(old_write, (None, None, 'image/png', PNG))
            await conn.execute('DELETE FROM board_posts WHERE id=%s', (post['id'],))
            assert not await outside.positions(conn, board_post_id=post['id'])
        finally:
            await conn.rollback()
            await conn.execute('RESET search_path')
            await conn.execute('DROP SCHEMA IF EXISTS m4_upgrade CASCADE')
            await conn.commit()


async def test_fetch_guard_replay_attempts_and_log_redaction(app, client, task, picture_transport, caplog):
    settings, requests = picture_transport
    ctx = app.state.ctx
    ctx.workspace.config.pages.outside_images = True
    ada, ben = await client(), await client('ben')
    private = await ada.call('board_post', {'title': 'Private', 'visibility': 'private', 'text': 'Start'})
    hidden = {'thread_id': private['thread_id'], 'text': 'A picture', 'blocks': [BLOCK]}
    assert (await ben.call('board_post', hidden, error=True))['error']['message'] == board.NOT_FOUND
    await ada.call('board_resolve', {'thread_id': private['thread_id']})
    await ada.call('board_post', hidden, error=True)
    late = await ada.call('board_post', {'ticket_url': TICKET, 'text': 'Later linked'})
    await task(ticket=TICKET, owner=ada.person_id)
    await ben.call('board_post', {'ticket_url': TICKET, 'text': 'Hidden', 'blocks': [BLOCK]}, error=True)
    await ben.call('board_post', {**hidden, 'thread_id': late['thread_id']}, error=True)

    capped = await ada.call('board_post', {'title': 'Stall', 'text': 'Start'})
    ctx.workspace.config.board.max_consecutive_agent_posts = 1
    stalled = await ada.call('board_post', {**hidden, 'thread_id': capped['thread_id']}, error=True)
    assert stalled['stalled_reason'] == 'turn_cap'
    assert requests == []
    ctx.workspace.config.board.max_consecutive_agent_posts = 20

    url = 'https://example.com/picture.png?token=example-test-secret'
    args = {'title': 'Report', 'source_ref': 'picture-a', 'text': 'Picture',
            'blocks': [LINKS, {**BLOCK, 'image': {'url': url}}]}

    async def reserved():
        async with ctx.pool.connection() as conn:
            row = await one(conn, """SELECT attempts FROM agent_oauth_limits
                WHERE kind='board_image_fetch' AND key=%s""", (str(ada.token_id),))
            assert row and row['attempts'] >= 1

    settings['check'] = reserved
    with caplog.at_level('DEBUG'):
        post = await ada.call('board_post', args)
    assert 'example-test-secret' not in caplog.text
    assert await ada.call('board_post', args) == post
    await ada.call('board_post', {**args, 'text': 'Changed'}, error=True)
    assert len(requests) == 1
    read = await ada.call('board_read', {'thread_id': post['thread_id']})
    assert read['posts'][0]['blocks'] == [
        {'title': '', **LINKS}, {'kind': 'image', 'alt': '', 'caption': BLOCK['caption'], 'stored': True}]
    async with ctx.pool.connection() as conn:
        await conn.execute("""UPDATE agent_oauth_limits SET attempts=19
            WHERE kind='board_image_fetch' AND key=%s""", (str(ada.token_id),))
    settings.update(status=302, headers={'location': 'https://other.example/image'})
    error = await ada.call('board_post', {**args, 'source_ref': 'redirect'}, error=True)
    assert 'redirects' in error['error']['message'] and len(requests) == 2
    error = await ada.call('board_post', {**args, 'source_ref': 'rate'}, error=True)
    assert '20 picture fetch attempts' in error['error']['message'] and len(requests) == 2
    for address in ('https://127.0.0.1/picture', 'https://mixed.example/picture'):
        await ben.call('board_post', {**args, 'blocks': [{**BLOCK, 'image': {'url': address}}]}, error=True)
    assert len(requests) == 2


async def test_image_write_rechecks_access_and_replay_after_fetch(app, client, task, picture_transport):
    settings, requests = picture_transport
    ctx = app.state.ctx
    ctx.workspace.config.pages.outside_images = True
    ada, ben = await client(), await client('ben')
    thread = await ada.call('board_post', {'ticket_url': TICKET, 'text': 'Report'})
    args = {'thread_id': thread['thread_id'], 'text': 'Picture', 'blocks': [BLOCK], 'source_ref': 'after-fetch'}

    async def restrict():
        await task(ticket=TICKET, owner=ada.person_id)

    settings['check'] = restrict
    assert (await ben.call('board_post', args, error=True))['error']['message'] == board.NOT_FOUND
    settings.pop('check')
    settings['delay'] = .03
    a, b = await asyncio.gather(ada.call('board_post', args), ada.call('board_post', args))
    assert a == b and len(requests) in (2, 3)
    async with ctx.pool.connection() as conn:
        assert (await one(conn, 'SELECT count(*) AS n FROM page_files'))['n'] == 1


async def test_mirror_acl_storage_limits_and_redaction(app, client, monkeypatch):
    ctx = app.state.ctx
    ada = await client()
    calls = []

    async def mirror(ctx, image):
        calls.append(image)
        return {**image, 'ref': 'a' * 40}, PNG, 'image/png'

    monkeypatch.setattr(images, 'read', mirror)
    args = {'title': 'Report', 'text': 'Picture', 'blocks': [MIRROR]}
    error = await ada.call('board_post', args, error=True)
    assert error['error']['message'].startswith('Pictures from this repository') and not calls
    ctx.workspace.config.board.shareable_image_repos = ['example/project']
    await ada.call('board_post', {**args, 'blocks': [MIRROR] * 5}, error=True)
    preview = {'kind': 'preview', 'name': 'Agent', 'text': 'Details', 'image': MIRROR['image']}
    error = await ada.call('board_post', {**args, 'blocks': [preview]}, error=True)
    assert 'Put the picture in its own image block.' in error['error']['message'] and not calls

    for limit in ('BOARD_STORE_BYTES', 'STORE_BYTES'):
        with monkeypatch.context() as patch:
            patch.setattr(outside, limit, len(PNG) - 1)
            error = await ada.call('board_post', args, error=True)
            assert 'The picture store is full.' in error['error']['message']
            async with ctx.pool.connection() as conn:
                assert not await one(conn, 'SELECT 1 FROM board_posts')
                assert not await one(conn, 'SELECT 1 FROM board_threads')
    post = await ada.call('board_post', args)
    async with ctx.pool.connection() as conn:
        row = await one(conn, 'SELECT blocks FROM board_posts WHERE id=%s', (post['post_id'],))
        assert 'image' not in row['blocks'][0]
        nested = {'kind': 'preview', 'name': 'Agent', 'text': 'Old',
                  'image': {**MIRROR['image'], 'sha': 'secret', 'mirror_path': '/private/mirror'}}
        await conn.execute('UPDATE board_posts SET blocks=%s WHERE id=%s',
                           (Jsonb(row['blocks'] + [nested]), post['post_id']))
    read = await ada.call('board_read', {'thread_id': post['thread_id']})
    assert read['posts'][0]['blocks'] == [
        {'kind': 'image', 'alt': '', 'caption': '', 'stored': True},
        {'kind': 'preview', 'name': 'Agent', 'text': 'Old'}]


async def test_retention_skips_reopen_lock_and_ages_from_close(app, client, picture_transport):
    ctx = app.state.ctx
    ctx.workspace.config.pages.outside_images = True
    ada = await client()
    args = {'text': 'Picture', 'blocks': [BLOCK]}
    old, recent, keep = [await ada.call('board_post', {**args, 'title': title})
                         for title in ('Old', 'Recent', 'Reopen now')]
    for row in (old, recent, keep):
        await ada.call('board_resolve', {'thread_id': row['thread_id']})
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE board_threads SET closed_at=now()-interval '91 days' WHERE id=ANY(%s)",
                           ([old['thread_id'], keep['thread_id']],))
        await conn.execute("UPDATE page_files SET created_at=now()-interval '100 days'")
    async with ctx.pool.connection() as conn:
        person = await people.get(conn, 'ada')
        await board.human_resolve(conn, workspace=ctx.workspace, person=person, thread_id=keep['thread_id'])
        await asyncio.wait_for(outside.cleanup(ctx), 3)
    await outside.cleanup(ctx)
    await ada.call('board_resolve', {'thread_id': old['thread_id'], 'state': 'open'})
    for row, expected in ((old, False), (recent, True), (keep, True)):
        read = await ada.call('board_read', {'thread_id': row['thread_id']})
        assert read['posts'][0]['blocks'][0]['stored'] is expected


async def test_public_picture_link_keeps_its_write_time_choice(app, client, picture_transport):
    ctx = app.state.ctx
    _, requests = picture_transport
    ctx.workspace.config.pages.outside_images = False
    ada = await client()
    args = {'title': 'Link to a picture', 'text': 'Report', 'source_ref': 'link-only', 'blocks': [BLOCK]}
    post = await ada.call('board_post', args)
    ctx.workspace.config.pages.outside_images = True
    assert await ada.call('board_post', args) == post
    read = await ada.call('board_read', {'thread_id': post['thread_id']})
    assert read['posts'][0]['blocks'] == [
        {'kind': 'image', 'alt': '', 'caption': BLOCK['caption'], 'stored': False, 'link': BLOCK['image']['url']}]
    assert not requests

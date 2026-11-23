"""Task progress uses source facts and makes no model call."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from illo.channels.mcp.schemas import Progress
from illo.db import all_rows, one
from illo.ledger import kv
from illo.tasks import progress
from illo.tasks.pages import render
from tests.tasks.test_pages import ANA, make
from tests.tasks.test_pages import browser as page_browser
from tests.tasks.test_pages import ctx as page_ctx
from tests.tasks.test_tools import agent

ctx = page_ctx
browser = page_browser


@pytest.mark.parametrize('value', [
    {'extra': 1}, {'steps': [{'text': 'Read', 'state': 'bad'}]},
    {'steps': [{'text': 'x' * 121, 'state': 'todo'}]},
    {'steps': [{'text': 'Read', 'state': 'todo'}] * 13},
    {'steps': [{'text': 'Read', 'state': 'todo', 'extra': 1}]},
    {'now': 'private command'}, {'files_changed': -1}, {'files_changed': 100001},
    {'steps_done': -1}, {'steps_done': 100001}, {'steps_total': -1}, {'steps_total': 100001},
    {'files_changed': True}, {'steps_done': 1.0}, {'steps_total': '1'},
])
def test_progress_limits(value):
    with pytest.raises(ValidationError):
        Progress.model_validate(value)


def test_progress_boundaries():
    value = {'steps': [{'text': 'x' * 120, 'state': 'done'}] * 12, 'steps_total': 100000,
             'steps_done': 0, 'files_changed': 100000, 'now': 'tests'}
    assert Progress.model_validate(value).model_dump(exclude_unset=True) == value


@pytest.mark.parametrize('state,holder,live,pull,expected', [
    ('todo', 'agent:build', False, False, 'waits'),
    ('working', 'agent:build', False, False, 'waits'),
    ('todo', 'agent:build', True, False, 'works'),
    ('working', 'agent:build', True, True, 'pull'),
    ('review', 'agent:build', True, True, 'review'),
    ('done', 'agent:build', True, True, 'done'),
    ('todo', 'person:ada', False, False, None),
    ('cancelled', 'agent:build', False, True, None),
])
def test_stage(state, holder, live, pull, expected):
    assert progress.stage({'state': state, 'holder': holder}, live, pull) == expected


async def test_store_clear_own_lane_and_no_event(ctx, app, http):
    row = await make(ctx)
    build = await agent(app, http, 'build')
    ref = {'source': 'codex', 'external_id': 'example', 'task': row['id']}
    value = {'steps': [{'text': 'Read the code', 'state': 'now'}], 'steps_total': 1,
             'steps_done': 0, 'now': 'reads'}
    lane = await build.call('lane_report', {**ref, 'state': 'working', 'progress': value})
    await build.call('lane_report', {**ref, 'progress': {**value, 'now': 'tests'}})
    async with ctx.pool.connection() as conn:
        stored = await one(conn, 'SELECT * FROM lanes WHERE id=%s', (lane['lane_id'],))
        at = stored['progress_at']
        assert stored['progress']['now'] == 'tests' and at
        assert len(await all_rows(conn, 'SELECT * FROM lane_events')) == 1
    await build.call('lane_report', ref)
    async with ctx.pool.connection() as conn:
        stored = await one(conn, 'SELECT * FROM lanes WHERE id=%s', (lane['lane_id'],))
        assert stored['progress_at'] == at and stored['progress']['now'] == 'tests'
        value = await progress.read(conn, row, ANA)
        assert value['stage'] == 'works' and value['now'] == 'tests'
        # The stored lane state alone does not mean live.
        await kv.delete(conn, f"task:lane-live:{row['id']}:{lane['lane_id']}")
        assert (await progress.read(conn, row, ANA))['stage'] == 'waits'
    read = await build.call('task_get', {'id': row['id']})
    assert read['progress']['steps'] and read['page_url'].endswith(f"/t/{row['id']}")
    mine = await build.call('task_mine')
    assert mine['tasks'][0]['progress']['stage'] == 'waits'
    other = await agent(app, http, 'studio', 'ben')
    await other.call('lane_report', {**ref, 'progress': {'now': 'tests'}}, error=True)
    await build.call('lane_report', {**ref, 'progress': None})
    async with ctx.pool.connection() as conn:
        stored = await one(conn, 'SELECT * FROM lanes WHERE id=%s', (lane['lane_id'],))
        assert stored['progress'] is None and stored['progress_at']
        assert (await one(conn, 'SELECT progress IS NULL AS cleared FROM lanes WHERE id=%s', (lane['lane_id'],)))['cleared']
        assert len(await all_rows(conn, 'SELECT * FROM lane_events')) == 1


async def test_migration_twice(ctx):
    sql = Path('illo/migrations/1805_lane_progress.sql').read_text()
    async with ctx.pool.connection() as conn:
        await conn.execute(sql)
        await conn.execute(sql)
        assert await one(conn, 'SELECT progress,progress_at FROM lanes LIMIT 1') is None


@pytest.mark.parametrize('state,holder,shown', [
    ('todo', 'agent:build', True), ('working', 'agent:build', True),
    ('review', 'agent:build', False), ('done', 'agent:build', False),
    ('todo', 'person:ada', False), ('working', None, False),
])
async def test_frame_only_for_agent_work(ctx, browser, state, holder, shown):
    row = await make(ctx, holder=holder)
    async with ctx.pool.connection() as conn:
        await conn.execute('UPDATE tasks SET state=%s WHERE id=%s', (state, row['id']))
    await browser.login()
    page = await browser.http.get(f"/t/{row['id']}")
    assert ('<iframe class="progress s0"' in page.text) == shown
    assert 'http-equiv="refresh"' not in page.text
    assert "frame-src 'self'" in page.headers['content-security-policy']
    assert "frame-ancestors 'none'" in page.headers['content-security-policy']


async def test_progress_route_access(ctx, browser):
    row = await make(ctx, list_key='private')
    path = f"/t/{row['id']}/progress"
    missing = await browser.http.get(path)
    file_missing = await browser.http.get(f"/t/{row['id']}/file/0")
    assert missing.status_code == 404 and missing.text == file_missing.text
    await browser.login(user='U_ACME_BEN')
    assert (await browser.http.get(path)).status_code == 404


async def test_progress_texts_marks_refresh_and_csp(ctx, browser, app, http, fake_brain):
    row = await make(ctx, links=[{'kind': 'repo', 'url': 'https://github.com/acme-example/engine'}])
    await browser.login()
    path = f"/t/{row['id']}/progress"
    page = await browser.http.get(path)
    assert 'The agent did not start yet.' in page.text
    assert '<li class="here">Waits for the agent</li>' in page.text
    assert '<li>Pull request</li>' in page.text
    assert '<body class="frame">' in page.text
    assert '<meta http-equiv="refresh" content="15">' in page.text
    assert "frame-ancestors 'self'" in page.headers['content-security-policy']
    for key, value in render.HEADERS.items():
        if key != 'Content-Security-Policy':
            assert page.headers[key] == value
    build = await agent(app, http, 'build')
    lane = await build.call('lane_report', {'source': 'codex', 'external_id': 'frame', 'task': row['id'],
        'state': 'working', 'links': ['https://github.com/acme-example/engine/tree/repair'],
        'progress': {'steps': [{'text': '<script>alert(1)</script><!-- hidden -->', 'state': 'done'},
                               {'text': '**Read**', 'state': 'now'}, {'text': 'Check', 'state': 'todo'}],
                     'steps_done': 4, 'steps_total': 8, 'files_changed': 1, 'now': 'tests'}})
    page = await browser.http.get(path)
    assert '<li class="here">Works</li>' in page.text
    assert "The agent's own steps · 4 of 8" in page.text.replace('&#x27;', "'")
    assert '<ul class="steps">' in page.text and '<li class="now">' in page.text
    assert '&lt;script&gt;' in page.text and 'hidden' not in page.text and '**Read**' in page.text
    assert 'Now: runs the tests' in page.text and 'a few seconds ago' in page.text
    assert '1 file changed' in page.text and '<li>repair</li>' in page.text
    assert 'style=' not in page.text and '<script>' not in page.text
    main = await browser.http.get(f"/t/{row['id']}")
    assert f'<iframe class="progress s3" src="{path}" title="Progress" loading="lazy"></iframe>' in main.text
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE lanes SET last_signal_at=now()-interval '16 minutes' WHERE id=%s", (lane['lane_id'],))
    page = await browser.http.get(path)
    assert 'No signal for 16 minutes.' in page.text and 'Now:' not in page.text
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE tasks SET state='review',waits_for='person:ada' WHERE id=%s", (row['id'],))
    page = await browser.http.get(path)
    assert 'The state of the task changed.' in page.text
    assert f'<a href="/t/{row["id"]}" target="_top">Read the page again</a>' in page.text
    assert 'http-equiv="refresh"' not in page.text and 'Your review' in page.text
    assert not fake_brain.requests


async def test_task_create_page_url(ctx, app, http):
    build = await agent(app, http, 'build')
    row = await build.call('task_create', {'list': 'launches', 'title': 'Read the work'})
    assert row['page_url'] == f"https://illo.example.com/t/{row['id']}"


@pytest.mark.parametrize('n,words', [(0, 'a few seconds'), (1, '1 minute'), (5, '5 minutes'),
                                     (60, '1 hour'), (180, '3 hours')])
def test_age(n, words):
    assert progress.age(datetime.now(UTC) - timedelta(minutes=n)) == words


async def test_live_key_and_private_lane_text(ctx):
    from illo.ledger import lanes, people

    row = await make(ctx)
    async with ctx.pool.connection() as conn:
        ben = await people.get(conn, 'ben')
        lane = await lanes.upsert(conn, source='codex', external_id='private', title='Private',
            owner_person_id=ben['id'], visibility='private', state='working',
            progress={'steps': [{'text': 'Private step', 'state': 'now'}], 'now': 'reads'})
        await kv.set(conn, f"task:lane-live:{row['id']}:{lane['id']}", True)
        value = await progress.read(conn, row, ANA)
        assert value == {'stage': 'works', 'at': None}
        await kv.delete(conn, f"task:lane-live:{row['id']}:{lane['id']}")
        await kv.set(conn, f"task:lane-live:{row['id']}:999", True)
        assert (await progress.read(conn, row, ANA))['stage'] == 'works'


async def test_pull_from_lane_and_review_labels(ctx):
    from illo.ledger import lanes, tasks

    row = await make(ctx)
    async with ctx.pool.connection() as conn:
        lane = await lanes.upsert(conn, source='codex', external_id='linked', title='Work',
            links=['https://github.com/acme-example/engine/pull/1'])
        await kv.set(conn, f"task:lane:{row['id']}:{lane['id']}", True)
        assert (await progress.read(conn, row, ANA))['stage'] == 'pull'
        row['state'], row['waits_for'] = 'review', 'person:ben'
        document = await render.progress_document(conn, row, {'key': 'ada'})
        assert '<li class="here">Review</li>' in document and 'Your review' not in document
        row['state'] = 'done'
        assert '<li class="here">Done</li>' in await render.progress_document(conn, row, {'key': 'ada'})
        assert await tasks.get(conn, row['id'], viewer=ANA)


async def test_ticket_lane_does_not_mean_agent_started(ctx):
    from illo.ledger import lanes

    url = 'https://github.com/acme-example/engine/issues/1'
    row = await make(ctx, links=[{'kind': 'ticket', 'url': url}])
    async with ctx.pool.connection() as conn:
        await lanes.upsert(conn, source='github', external_id='ticket', title='Ticket',
                           state='open', links=[{'kind': 'github', 'url': url}])
        document = await render.progress_document(conn, row, {'key': 'ada'})
        assert 'The agent did not start yet.' in document
        assert '<li>Pull request</li>' not in document

"""The board shares real page access, decisions, and bounded ledger reads."""

import re
import shutil
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from illo.asks import cards, service
from illo.channels.slack import tasks as slack_tasks
from illo.db import one
from illo.ledger import ask_workflow, asks, digest_snapshot, items, people, tasks
from illo.tasks import pages, views
from illo.tasks.pages import board, render, signin
from illo.workspace import PagesConfig, TaskList
from tests.asks.test_request_pages import ASK, login
from tests.mcp.client import AgentClient
from tests.tasks.test_pages import ANA, BASE, fields, make, write
from tests.tasks.test_pages import browser as browser
from tests.tasks.test_pages import mirror as mirror
from tests.tasks.test_tools import agent


@pytest.fixture
async def ctx(app, monkeypatch):
    ctx = app.state.ctx
    await ctx.scheduler.close()
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.config.pages = PagesConfig(enabled=True, url=BASE)
    ctx.workspace.lists += [TaskList(key='private', name='Private', owner='ada')]
    monkeypatch.setattr(slack_tasks, 'HOLD_FOR_PAGE', 0)
    return ctx


def column(response, key):
    return response.text.split(f'<section class="column" id="{key}">', 1)[1].split(
        '<section class="column" id="', 1)[0]


def frame_fields(response, frame_id):
    return fields(SimpleNamespace(text=response.text.split(f'id="{frame_id}">', 1)[1].split('</article>', 1)[0]))


async def test_signin_static_empty_board(ctx, browser):
    response = await browser.http.get('/web/board')
    assert response.status_code == 302
    assert response.headers['location'] == '/web/signin?next=%2Fweb%2Fboard'
    sign_in = await browser.http.get(response.headers['location'])
    assert '<form method="post"><button class="btn go">Get a code</button></form>' in sign_in.text
    script = await browser.http.get('/web/board.js')
    assert script.status_code == 200 and script.headers['content-type'].startswith('text/javascript')
    assert script.headers['cache-control'] == 'no-store'
    assert "['work', 'threads', 'done']" in script.text
    response = await browser.login(next='/web/board')
    assert response.status_code == 303 and response.headers['location'] == '/web/board'
    page = await browser.http.get('/web/board')
    for key, title, line in [('needs', 'Needs you', 'Nothing waits for you.'),
                             ('work', 'In work', 'No task is in work.'),
                             ('threads', 'Threads', 'No open thread.'),
                             ('done', 'Done today', 'Nothing closed in the last 24 hours.')]:
        assert f'>{title}</h2>' in column(page, key) and line in column(page, key)
    assert page.headers['cache-control'] == 'no-store'
    assert page.text.count('<script') == 1 and '<script src="/web/board.js" defer></script>' in page.text
    assert not re.search(r'\sstyle\s*=', page.text)
    for label, text in [('Zoom out', '−'), ('Zoom in', '+'), ('Show all', 'Fit')]:
        assert f'aria-label="{label}">{text}</button>' in page.text
    assert signin.next_path('/web/board#t17') == '/web/board#t17'
    assert signin.next_path('/web/board#a17') == '/web/board#a17'
    for path in ['/web/board?next=//evil.example', '/web/board#bad', '/web/board//evil.example', '/web/board%0a']:
        assert signin.next_path(path) == ''


async def test_board_csp_allows_same_origin_refresh(ctx, browser):
    await login(ctx, browser.http, 'ada')
    page = await browser.http.get('/web/board')
    assert page.status_code == 200
    policy = page.headers['content-security-policy']
    directives = {directive.strip() for directive in policy.split(';')}
    assert {"default-src 'none'", "script-src 'self'", "connect-src 'self'"} <= directives
    assert "'unsafe-inline'" not in policy


@pytest.mark.parametrize('source', ['board', 'other'])
async def test_task_blocks_forms_approval_and_redirect(ctx, browser, source):
    row = await make(ctx, returned=True)
    await write(ctx, row)
    await login(ctx, browser.http, 'ada')
    page = await browser.http.get('/web/board')
    needs = column(page, 'needs')
    assert '1 thing · the oldest waits' in needs
    for text in ['Waits for you · today', 'In review', 'Holder:', 'Review the work',
                 'The work is ready.', 'Approve', 'Send back…', 'name="from" value="board"']:
        assert text in needs
    assert f'href="/t/{row["id"]}"' in needs
    response = await browser.http.post(f'/t/{row["id"]}/decide', data={
        **frame_fields(page, f't{row["id"]}'), 'action': 'done', 'from': source})
    assert response.status_code == 303
    assert response.headers['location'] == (f'/web/board#t{row["id"]}' if source == 'board' else f'/t/{row["id"]}')
    async with ctx.pool.connection() as conn:
        assert (await tasks.get(conn, row['id'], viewer=ANA))['state'] == 'done'
    page = await browser.http.get('/web/board')
    assert '1 task in the last 24 hours' in column(page, 'done')
    assert 'Ada approved it at ' in column(page, 'done')
    task_page = await browser.http.get(f'/t/{row["id"]}')
    assert '<a href="/web/board">Board</a>' in task_page.text
    assert 'script-src' not in task_page.headers['content-security-policy']


async def test_send_back_and_message_use_task_routes(ctx, browser):
    row = await make(ctx, returned=True)
    await write(ctx, row)
    await login(ctx, browser.http, 'ada')
    page = await browser.http.get('/web/board')
    result = await browser.http.post(f'/t/{row["id"]}/decide', data={
        **frame_fields(page, f't{row["id"]}'), 'action': 'reopen', 'reason': 'Use the short plan.', 'from': 'board'})
    assert result.headers['location'] == f'/web/board#t{row["id"]}'
    async with ctx.pool.connection() as conn:
        await conn.execute('UPDATE tasks SET waits_for=%s WHERE id=%s', ('person:ada', row['id']))
    page = await browser.http.get('/web/board')
    assert 'Message the holder…' in column(page, 'needs')
    result = await browser.http.post(f'/t/{row["id"]}/decide', data={
        **frame_fields(page, f't{row["id"]}'), 'action': 'message', 'reason': 'Check the final step.', 'from': 'board'})
    assert result.headers['location'] == f'/web/board#t{row["id"]}'
    async with ctx.pool.connection() as conn:
        assert await one(conn, "SELECT id FROM task_events WHERE task_id=%s AND text='Check the final step.'", (row['id'],))


@pytest.mark.parametrize('source,other', [('board', False), ('other', False), ('board', True)])
async def test_ask_blocks_short_proof_answer_and_redirect(ctx, app, http, browser, source, other):
    agent = await AgentClient.create(app, http)
    ask = await agent.call('ask_create', {**ASK, 'owner': 'ada',
        'blocks': [{'kind': 'text', 'text': 'Read each step.'}],
        'proof': {'text': 'The first line.\nThe hidden second line.', 'one': 1, 'two': 2, 'three': 'The fourth entry.'}})
    await login(ctx, browser.http, 'ada')
    page = await browser.http.get('/web/board')
    needs = column(page, 'needs')
    assert 'Waits for you · today' in needs and 'From:' in needs
    assert 'Read each step.' in needs and 'The first line.' in needs
    assert 'The hidden second line.' not in needs and 'The fourth entry.' not in needs
    assert 'Open the page for all of it' in needs and 'Other answer…' in needs
    request_page = await browser.http.get(f'/web/a/{ask["ask_id"]}')
    assert 'The fourth entry.' in request_page.text and '<a href="/web/board">Board</a>' in request_page.text
    assert 'script-src' not in request_page.headers['content-security-policy']
    data = {'text': 'Use the short plan.'} if other else {'option': 'yes'}
    result = await browser.http.post(f'/web/a/{ask["ask_id"]}/answer', data={
        **frame_fields(page, f'a{ask["ask_id"]}'), **data, 'from': source})
    assert result.status_code == 303
    assert result.headers['location'] == (f'/web/board#a{ask["ask_id"]}' if source == 'board' else f'/web/a/{ask["ask_id"]}')
    async with ctx.pool.connection() as conn:
        assert (await ask_workflow.page_read(conn, ask['ask_id'], agent.person_id))['state'] == 'answered'


async def test_private_access_and_saved_proof(ctx, app, http, browser):
    visible = await make(ctx, returned=True, title='The visible task')
    hidden = await make(ctx, returned=True, list_key='private', title='The private task')
    async with ctx.pool.connection() as conn:
        ben = await people.get(conn, 'ben')
        await conn.execute("UPDATE tasks SET waits_for='person:ben' WHERE id=ANY(%s)", ([visible['id'], hidden['id']],))
        ada = await people.get(conn, 'ada')
        await asks.create(conn, owner_person_id=ada['id'], requester_person_id=ada['id'], question='The private request?')
        await items.upsert(conn, source_ref='saved:77', title='The private note', body='The private text.',
                           visibility='private', owner_person_id=ada['id'])
        await asks.create(conn, owner_person_id=ben['id'], requester_person_id=ben['id'], question='Read the note?',
                          proof={'note': 'saved:77'})
    await login(ctx, browser.http)
    page = await browser.http.get('/web/board')
    assert 'The visible task' in page.text
    assert 'The private task' not in page.text and 'The private request?' not in page.text
    assert 'You cannot see this saved item.' in page.text
    assert 'The private note' not in page.text and 'The private text.' not in page.text
    assert (await browser.http.get(f'/t/{hidden["id"]}')).status_code == 404


async def test_work_progress_done_events_and_order(ctx, browser):
    older = await make(ctx, title='The older work')
    newer = await make(ctx, title='The newer work', holder='person:ben')
    closed = await make(ctx, title='The closed task')
    old_done = await make(ctx, title='The old closed task')
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE tasks SET waits_for=NULL,updated_at=now()-interval '2 hours' WHERE id=%s", (older['id'],))
        await conn.execute("UPDATE tasks SET waits_for=NULL WHERE id=%s", (newer['id'],))
        await conn.execute("UPDATE tasks SET state='done',done_at=now() WHERE id=%s", (closed['id'],))
        await conn.execute("INSERT INTO task_events(task_id,actor,kind) VALUES (%s,'code','done')", (closed['id'],))
        await conn.execute("UPDATE tasks SET state='done',done_at=now()-interval '25 hours' WHERE id=%s", (old_done['id'],))
    await login(ctx, browser.http, 'ada')
    page = await browser.http.get('/web/board')
    work = column(page, 'work')
    assert 'Agents hold 1 task, people hold 1' in work
    assert work.index('The newer work') < work.index('The older work')
    assert 'Ben holds it.' in work and 'Waits for the agent' in work and ' of 0 steps' not in work
    assert '<iframe' not in page.text
    done = column(page, 'done')
    assert 'Closed at ' in done and 'The old closed task' not in page.text


async def test_work_stage_now_steps_and_private_lane(ctx, app, http, browser):
    row = await make(ctx)
    build = await agent(app, http, 'build')
    lane = await build.call('lane_report', {'source': 'codex', 'external_id': 'example', 'task': row['id'],
        'state': 'working', 'progress': {'now': 'tests', 'steps_total': 4, 'steps_done': 2,
                                        'steps': [{'text': 'Check each step', 'state': 'now'}]}})
    await login(ctx, browser.http, 'ada')
    page = await browser.http.get('/web/board')
    work = column(page, 'work')
    assert '>Works</p>' in work and 'Now: runs the tests' in work and '2 of 4 steps' in work
    async with ctx.pool.connection() as conn:
        ben = await people.get(conn, 'ben')
        await conn.execute("UPDATE lanes SET visibility='private',owner_person_id=%s WHERE id=%s",
                           (ben['id'], lane['lane_id']))
    work = column(await browser.http.get('/web/board'), 'work')
    assert 'Now: runs the tests' not in work and '2 of 4 steps' not in work


async def test_timezone_date_and_wait_period(ctx, browser, monkeypatch):
    fixed = datetime(2026, 11, 20, 1, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz)

    monkeypatch.setattr(board, 'datetime', Clock)
    row = await make(ctx, returned=True)
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE people SET timezone='America/New_York' WHERE key='ada'")
        # A new message does not reset the current wait.
        await conn.execute("UPDATE task_events SET at=%s WHERE task_id=%s AND kind='returned'",
                           (fixed - timedelta(days=6), row['id']))
        await conn.execute("INSERT INTO task_events(task_id,actor,kind,text,data) VALUES (%s,'agent:build','message','A later message.',%s)",
                           (row['id'], Jsonb({'waits_for': 'person:ada'})))
    await login(ctx, browser.http, 'ada')
    page = await browser.http.get('/web/board')
    assert '<span>Thu 19 Nov</span>' in page.text
    assert '1 thing · the oldest waits 6 days' in column(page, 'needs')


async def test_wait_order_mixed_limit_and_person_links(ctx, browser):
    now = datetime.now(UTC)
    rows = [await make(ctx, returned=True, title=f'Task {n}') for n in range(31)]
    async with ctx.pool.connection() as conn:
        person = await people.get(conn, 'ada')
        for n, row in enumerate(rows):
            await conn.execute('UPDATE task_events SET at=%s WHERE task_id=%s', (now - timedelta(days=n+1), row['id']))
        ask = await asks.create(conn, owner_person_id=person['id'], question='The oldest request?')
        await conn.execute("UPDATE asks SET created_at=now()-interval '40 days' WHERE id=%s", (ask['id'],))
    await login(ctx, browser.http, 'ada')
    page = await browser.http.get('/web/board')
    needs = column(page, 'needs')
    ids = re.findall(r'<article class="f" id="([ta][0-9]+)">', needs)
    assert len(ids) == 30 and ids[0] == f'a{ask["id"]}' and ids[1] == f't{rows[-1]["id"]}'
    assert '32 things · the oldest waits 40 days' in needs
    assert '2 more. Open the task list in Slack to see them.' in needs
    ctx.workspace.config.pages.open = 'board'
    async with ctx.pool.connection() as conn:
        assert await pages.person_path(conn, ctx.workspace, person, rows[-1]['id']) == f'/web/board#t{rows[-1]["id"]}'
        assert await pages.person_path(conn, ctx.workspace, person, rows[0]['id']) == f'/t/{rows[0]["id"]}'
        assert await pages.person_path(conn, ctx.workspace, person, ask['id'], ask=True) == f'/web/board#a{ask["id"]}'


@pytest.mark.parametrize('setting', ['page', 'board'])
async def test_card_signin_brief_and_agent_links(ctx, app, http, slack_client, setting):
    ctx.workspace.config.pages.open = setting
    row = await make(ctx, returned=True)
    async with ctx.pool.connection() as conn:
        person = await people.get(conn, 'ada')
        other = await people.get(conn, 'ben')
        destination = f'/web/board#t{row["id"]}' if setting == 'board' else f'/t/{row["id"]}'
        key = await signin.link(conn, ctx, person, row['id'])
        assert (await signin.request(conn, key))['next_path'] == destination
        key = await signin.link(conn, ctx, other, row['id'])
        assert (await signin.request(conn, key))['next_path'] == f'/t/{row["id"]}'
        brief = await digest_snapshot.build(conn, person, datetime.now(UTC), ctx.workspace)
        assert brief['needs_you'][0]['link'] == BASE + destination
        result = await views.full(conn, ctx.workspace, row, ANA, ctx=SimpleNamespace(app=ctx))
        assert result['page_url'] == BASE + destination
        assert (await pages.read(conn, ctx, ANA, row['id']))['url'] == BASE + destination
        result = await views.full(conn, ctx.workspace, row, tasks.Viewer('ada', 'agent:build'), ctx=SimpleNamespace(app=ctx))
        assert result['page_url'] == BASE + f'/t/{row["id"]}'
        ask = await asks.create(conn, owner_person_id=person['id'], question='Read this plan?')
        data = await service.prepare_card(ctx, conn, await ask_workflow.card_data(conn, ask))
        expected = f'/web/board#a{ask["id"]}' if setting == 'board' else f'/web/a/{ask["id"]}'
        assert data['page_url'] == BASE + expected
        assert 'web-open-ask:' in str(cards.ask_card(data))
        key = await signin.link(conn, ctx, person, ask['id'], ask=True)
        assert (await signin.request(conn, key))['next_path'] == expected
    agent = await AgentClient.create(app, http)
    created = await agent.call('ask_create', {**ASK, 'owner': 'ada'})
    assert created['page_url'] == BASE + f'/web/a/{created["ask_id"]}'
    await slack_tasks.tick(ctx)
    assert any(call['method'] == 'chat_postMessage' and call.get('text', '').startswith('Task: ')
               and BASE + destination in call['text'] for call in slack_client.calls)
    assert any(call['method'] == 'chat_postMessage' and BASE + destination in str(call.get('blocks', []))
               for call in slack_client.calls)
    async with ctx.pool.connection() as conn:
        await tasks.done(conn, row['id'], workspace=ctx.workspace, viewer=ANA)
        assert await pages.person_path(conn, ctx.workspace, person, row['id']) == f'/t/{row["id"]}'


@pytest.mark.parametrize('full', [False, True])
async def test_column_limits_and_query_budget(ctx, browser, monkeypatch, full):
    waiting = [await make(ctx, returned=True, title=f'Wait {n}') for n in range(31)]
    for row in waiting:
        await write(ctx, row)
    if full:
        for n in range(31):
            row = await make(ctx, title=f'Work {n}')
            closed = await make(ctx, title=f'Done {n}')
            async with ctx.pool.connection() as conn:
                await conn.execute('UPDATE tasks SET waits_for=NULL WHERE id=%s', (row['id'],))
                await tasks.done(conn, closed['id'], workspace=ctx.workspace, viewer=ANA)
    await login(ctx, browser.http, 'ada')
    queries = []
    original = AsyncConnection.execute

    async def count(conn, query, *args, **kwargs):
        queries.append(query)
        return await original(conn, query, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(AsyncConnection, 'execute', count)
        response = await browser.http.get('/web/board')
    assert response.status_code == 200
    assert len(queries) <= (162 if full else 72), len(queries)
    assert response.text.count('<article class="f"') == (90 if full else 30)
    for key in ['needs', 'work', 'done'] if full else ['needs']:
        assert column(response, key).count('<article class="f"') == 30
        assert '1 more. Open the task list in Slack to see them.' in column(response, key)
    if full:
        assert 'Agents hold 31 tasks, people hold 0' in column(response, 'work')
        assert '31 tasks in the last 24 hours' in column(response, 'done')
    assert board.HEADERS['Content-Security-Policy'].startswith(render.HEADERS['Content-Security-Policy'])


async def test_request_query_budget_reuses_saved_reads(ctx, browser, monkeypatch):
    async with ctx.pool.connection() as conn:
        person = await people.get(conn, 'ada')
        await items.upsert(conn, source_ref='saved:77', title='The saved note', body='Read the complete note.',
                           visibility='private', owner_person_id=person['id'])
        for n in range(31):
            await asks.create(conn, owner_person_id=person['id'], question=f'Read note {n}?',
                              proof={'notes': ['saved:77'] * 39})
    await login(ctx, browser.http, 'ada')
    queries = []
    original = AsyncConnection.execute

    async def count(conn, query, *args, **kwargs):
        queries.append(query)
        return await original(conn, query, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(AsyncConnection, 'execute', count)
        response = await browser.http.get('/web/board')
    assert response.status_code == 200
    assert response.text.count('<article class="f"') == 30
    assert 'The saved note' in response.text and 'Read the complete note.' not in response.text
    assert len(queries) <= 102, len(queries)
    assert sum('FROM items WHERE' in str(query) for query in queries) == 30


def reply_values(response, path, *, text='Reply'):
    from html import unescape

    form = response.text.split(f'<form method="post" action="{path}/reply">', 1)[1].split('</form>', 1)[0]
    return {**{key: unescape(value) for key, value in re.findall(r'name="([^"]+)" value="([^"]*)"', form)},
            'text': text}


async def test_thread_security_no_get_writes_and_post_ack(ctx, app, http, browser):
    from illo.board.policy import board_pages_ready

    assert board_pages_ready(ctx)
    ben = await AgentClient.create(app, http, 'ben')
    text = '<script>alert(1)</script> <img src=x onerror=alert(2)> [bad](javascript:alert(3))\n<!-- illo:wake hidden -->'
    post = await ben.call('board_post', {'title': '<Hostile title>', 'text': text, 'mentions': ['person:ada']})
    thread_id, post_id = post['thread_id'], post['post_id']
    path = f'/web/board/t/{thread_id}'

    no_session = await browser.http.get(path)
    assert no_session.status_code == 302 and 'signin' in no_session.headers['location']
    fragment = await browser.http.get(path + '/posts')
    assert fragment.status_code == 401 and not fragment.content
    assert (await browser.http.get(f'{path}/file/{post_id}/0')).status_code == 404

    await login(ctx, browser.http, 'ada')
    page = await browser.http.get(path, headers={'sec-fetch-site': 'cross-site'})
    fragment = await browser.http.get(path + '/posts?after=0')
    assert page.status_code == fragment.status_code == 200
    for response in (await browser.http.get('/web/board'), page):
        csp = response.headers['content-security-policy']
        assert "default-src 'none'" in csp and "script-src 'self'; connect-src 'self'" in csp
        assert 'unsafe-inline' not in csp and not re.search(r'\sstyle\s*=', response.text)
    for response in (page, fragment):
        assert response.headers['cache-control'] == 'no-store'
        assert '&lt;script&gt;' in response.text and '&lt;img ' in response.text
        assert '<script>alert' not in response.text and 'href="javascript:' not in response.text
        assert 'illo:wake hidden' not in response.text
        assert f'id="p{post_id}" data-unread' in response.text
    assert '<template id="next">' in fragment.text
    assert '<template id="status">' in fragment.text and '<template id="reply-to">' in fragment.text
    assert f'data-after="{post_id}"' in page.text
    assert (await browser.http.get(path + '/seen')).status_code == 405
    async with ctx.pool.connection() as conn:
        assert not await one(conn, 'SELECT 1 FROM board_mention_acks')
    values = {'csrf': signin.csrf('test-browser-ada'), 'upto': str(post_id)}
    for action in ('reply', 'reopen', 'seen'):
        route = path + '/' + action
        assert (await browser.http.post(route, data={})).status_code == 403
        assert (await browser.http.post(route, data=values, headers={'sec-fetch-site': 'cross-site'})).status_code == 403
        assert (await browser.http.post(route, data=values, headers={'origin': 'https://other.example'})).status_code == 403
    missing = await browser.http.get('/web/board/t/9223372036854775808')
    for route in (path + '/posts?after=9223372036854775808', f'{path}/file/{post_id}/01', f'{path}/file/{post_id}/-1',
                  f'{path}/file/{post_id}/6', f'{path}/file/{post_id}/0', f'{path}/file/9223372036854775808/0'):
        refused = await browser.http.get(route)
        assert (refused.status_code, refused.content, refused.headers['cache-control']) == (404, missing.content, 'no-store')
    assert (await browser.http.post(path + '/seen', data={**values, 'upto': '999999'})).content == missing.content
    assert (await browser.http.post(path + '/seen', data=values)).status_code == 204
    assert 'data-unread' not in (await browser.http.get(path)).text
    async with ctx.pool.connection() as conn:
        ack = await one(conn, 'SELECT reader_key,target FROM board_mention_acks')
        assert ack == {'reader_key': 'person:ada', 'target': 'person:ada'}
    ctx.workspace.config.pages.enabled = False
    assert not board_pages_ready(ctx)
    assert (await browser.http.get(path)).status_code == 404


async def test_board_thread_unicode_retry_reopen_and_refusal(ctx, app, http, browser):
    import asyncio

    ben = await AgentClient.create(app, http, 'ben')
    question = await ben.call('board_post', {'title': 'Question', 'kind': 'question', 'text': 'How?',
                                           'mentions': ['person:ada']})
    await login(ctx, browser.http, 'ada')
    thread, post = question['thread_id'], question['post_id']
    path = f'/web/board/t/{thread}'
    home = await browser.http.get('/web/board')
    assert f'id="b{thread}"' in home.text
    frame = SimpleNamespace(text=home.text.split(f'id="b{thread}">', 1)[1].split('</article>', 1)[0])
    assert 'mentioned you.' in frame.text
    values = reply_values(frame, path, text='😀' * 4001)
    refused = await browser.http.post(path + '/reply', data=values)
    assert refused.status_code == 200 and values['text'] in refused.text and 'Write 1 to 4,000 characters.' in refused.text
    too_large = {**values, 'text': '😀' * 6000}
    assert (await browser.http.post(path + '/reply', data=too_large)).status_code == 403
    values['text'] = '😀' * 4000
    responses = await asyncio.gather(*(browser.http.post(path + '/reply', data=values) for _ in range(2)))
    assert [r.status_code for r in responses] == [303, 303]
    location = responses[0].headers['location']
    assert location == responses[1].headers['location']
    assert location.startswith(f'{path}?sent={values["draft"]}#p')
    response = await browser.http.get(location)
    assert f'data-sent="{values["draft"]}"' in response.text
    assert f'id="b{thread}"' not in (await browser.http.get('/web/board')).text
    generated_id = int(location.split('#p')[1])
    marked = await browser.http.get(f'/web/board?sent_thread={thread}&sent={generated_id}')
    assert f'data-sent-thread="{thread}" data-sent="{values["draft"]}"' in marked.text
    assert 'data-sent' not in (await browser.http.get(f'/web/board?sent_thread={thread}&sent={post}')).text
    async with ctx.pool.connection() as conn:
        reply = await one(conn, 'SELECT kind,text,reply_to_post_id FROM board_posts WHERE is_human')
        assert reply == {'kind': 'answer', 'text': values['text'], 'reply_to_post_id': post}
    other = await ben.call('board_post', {'title': 'Other', 'text': 'Start'})
    other_path = f'/web/board/t/{other["thread_id"]}'
    blocked = await browser.http.post(other_path + '/reply', data={**values, 'reply_to': ''})
    assert 'This reply key belongs to another thread.' in blocked.text
    assert (await browser.http.post(other_path + '/reply', data=values)).status_code == 404
    async with ctx.pool.connection() as conn:
        await conn.execute("""INSERT INTO agent_oauth_limits(kind,key,attempts)
            VALUES ('board_human',%s,30) ON CONFLICT(kind,key) DO UPDATE SET attempts=30""",
                           (str((await people.get(conn, 'ada'))['id']),))
    rate_data = reply_values(await browser.http.get(other_path), other_path, text='Keep these words')
    rate = await browser.http.post(other_path + '/reply', data=rate_data)
    assert 'You sent 30 replies in one hour. Wait a few minutes.' in rate.text and 'Keep these words</textarea>' in rate.text
    async with ctx.pool.connection() as conn:
        await conn.execute("DELETE FROM agent_oauth_limits WHERE kind='board_human'")
    await ben.call('board_resolve', {'thread_id': other['thread_id']})
    closed = await browser.http.post(other_path + '/reply', data=rate_data)
    assert 'The thread is resolved. Reopen it to reply.' in closed.text and 'Keep these words</textarea>' in closed.text
    reopened = await browser.http.post(other_path + '/reopen', data={'csrf': values['csrf']})
    assert reopened.status_code == 303 and reopened.headers['location'] == other_path
    assert 'This thread is resolved.' not in (await browser.http.get(other_path)).text
    await ben.call('board_resolve', {'thread_id': other['thread_id'], 'state': 'archived'})
    assert 'This thread is archived. It is read-only.' in (await browser.http.get(other_path)).text
    archived = await browser.http.post(other_path + '/reopen', data={'csrf': values['csrf']})
    assert archived.status_code != 303 and f'action="{other_path}/reply"' not in archived.text


@pytest.mark.parametrize('back', ['thread', 'other', 'overflow', 'url'])
async def test_inline_ask_return_binds_committed_post(ctx, app, http, browser, back):
    from tests.board.test_board import DECISION

    ada = await AgentClient.create(app, http, 'ada')
    post = await ada.call('board_post', {'title': 'Dates', **DECISION})
    other = await ada.call('board_post', {'title': 'Other', 'text': 'Start'})
    path = f'/web/board/t/{post["thread_id"]}'
    await login(ctx, browser.http, 'ada')
    nonowner = await browser.http.get(path)
    assert 'Waits for ' in nonowner.text and 'Other answer…' not in nonowner.text
    await login(ctx, browser.http, 'ben')
    view = await browser.http.get(path)
    assert 'Other answer…' in view.text and f'name="back" value="thread:{post["thread_id"]}"' in view.text
    home = await browser.http.get('/web/board')
    assert f'id="a{post["ask_id"]}"' in home.text and f'id="b{post["thread_id"]}"' not in home.text
    assert f'<a href="{path}">Board: Dates</a>' in home.text
    values = {'thread': f'thread:{post["thread_id"]}', 'other': f'thread:{other["thread_id"]}',
              'overflow': 'thread:9223372036854775808', 'url': 'https://example.com'}
    ask_path = f'/web/a/{post["ask_id"]}'
    response = await browser.http.post(ask_path + '/answer', data={**fields(view), 'option': 'first', 'back': values[back]})
    assert response.status_code == 303
    async with ctx.pool.connection() as conn:
        committed = await one(conn, "SELECT id FROM board_posts WHERE thread_id=%s AND kind='decision'", (post['thread_id'],))
    expected = f'{path}#post-{committed["id"]}' if back == 'thread' else ask_path
    assert response.headers['location'] == expected
    html = (await browser.http.get(path)).text
    assert f'id="post-{committed["id"]}"' in html and 'Other answer…' not in html


async def test_threads_group_with_late_private_links(ctx, app, http, browser):
    from tests.board.test_board import TICKET

    ada = await AgentClient.create(app, http, 'ada')
    ben = await AgentClient.create(app, http, 'ben')
    team = await ada.call('board_post', {'title': 'Visible shared report', 'text': 'Report'})
    private = await ada.call('board_post', {'title': 'Private report', 'visibility': 'private', 'text': 'Report'})
    late = await ada.call('board_post', {'ticket_url': TICKET, 'title': 'Ticket report', 'text': 'Report'})
    await ben.call('board_post', {'title': 'Unsorted thread', 'text': 'Report'})
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE board_threads SET group_key='shared-report' WHERE id=ANY(%s)",
                           ([row['thread_id'] for row in (team, private, late)],))
        await conn.execute("""INSERT INTO tasks(list_key,title,brief,created_by,owner_person_id,visibility)
            VALUES ('bugs','Private task','','person:ada',%s,'private')""", (ada.person_id,))
        await conn.execute("INSERT INTO task_tickets(url,task_id) SELECT %s,id FROM tasks", (TICKET,))
    await login(ctx, browser.http, 'ben')
    invisible = await browser.http.get(f'/web/board/t/{late["thread_id"]}')
    missing = await browser.http.get('/web/board/t/999999')
    assert invisible.status_code == 404 and invisible.content == missing.content
    view = column(await browser.http.get('/web/board'), 'threads')
    assert 'Shared report<span>1</span>' in view and view.index('Shared report') < view.index('Other threads')
    assert 'Private report' not in view and 'Ticket report' not in view and 'Unsorted thread' in view
    await login(ctx, browser.http, 'ada')
    view = column(await browser.http.get('/web/board'), 'threads')
    assert 'Shared report<span>3</span>' in view and 'Private report' in view and 'Ticket report' in view
    public = await browser.http.get(f'/web/board/t/{team["thread_id"]}')
    assert 'Private report' not in public.text and 'Ticket report' not in public.text
    owner_view = await browser.http.get(f'/web/board/t/{late["thread_id"]}')
    assert '<li>Private</li>' in owner_view.text and 'Private report' in owner_view.text
    async with ctx.pool.connection() as conn:
        other = await one(conn, """INSERT INTO tasks(list_key,title,brief,created_by,owner_person_id,visibility)
            VALUES ('bugs','Another private task','','person:ben',%s,'private') RETURNING id""", (ben.person_id,))
        await conn.execute('INSERT INTO task_tickets(url,task_id) VALUES (%s,%s)', (TICKET + '#extra', other['id']))
    assert (await browser.http.get(f'/web/board/t/{late["thread_id"]}')).content == missing.content
    assert signin.next_path(f'/web/board#b{late["thread_id"]}') == f'/web/board#b{late["thread_id"]}'


async def test_thread_and_board_frame_render_links_and_stored_file(ctx, app, http, browser, mirror, monkeypatch):
    from illo.tasks.pages import images
    from tests.board.test_m4_files import LINKS

    repository, _, _, data = mirror
    image = {'kind': 'image', 'image': {'repo': 'acme-example/engine', 'ref': 'main', 'path': 'picture.png'}}
    ben = await AgentClient.create(app, http, 'ben')
    await login(ctx, browser.http, 'ada')
    plain = await ben.call('board_post', {'title': 'Links', 'text': 'See the report.', 'blocks': [LINKS],
                                        'mentions': ['person:ada']})
    ctx.workspace.config.board.shareable_image_repos = [image['image']['repo']]
    picture = await ben.call('board_post', {'title': 'Picture', 'text': 'See the report.', 'blocks': [image, LINKS],
                                          'mentions': ['person:ada']})
    private = await ben.call('board_post', {'title': 'Private', 'text': 'See the report.', 'blocks': [image],
                                          'visibility': 'private'})
    shutil.rmtree(repository)

    async def no_mirror(*args, **kwargs):
        pytest.fail('Page views cannot read a mirror')

    monkeypatch.setattr(images, 'read', no_mirror)
    for post in (plain, picture):
        path = f'/web/board/t/{post["thread_id"]}'
        for route in (path, path + '/posts?after=0', '/web/board'):
            rendered = (await browser.http.get(route)).text
            assert '<ul class="links">' in rendered and 'href="https://example.com/report"' in rendered
            assert 'class="body bars"' not in rendered
    image_url = f'/web/board/t/{picture["thread_id"]}/file/{picture["post_id"]}/0'
    response = await browser.http.get(image_url)
    assert response.content == data and response.headers['content-type'] == 'image/png'
    assert response.headers['content-disposition'] == 'inline'
    assert response.headers['content-security-policy'] == "default-src 'none'"
    assert 'no-store' in response.headers['cache-control']
    missing = await browser.http.get(image_url[:-1] + '1')
    hidden_url = f'/web/board/t/{private["thread_id"]}/file/{private["post_id"]}/0'
    for url in (hidden_url, image_url.replace(f'/file/{picture["post_id"]}/', f'/file/{private["post_id"]}/')):
        invisible = await browser.http.get(url)
        assert invisible.status_code == missing.status_code == 404
        assert invisible.content == missing.content and invisible.headers['cache-control'] == 'no-store'


async def test_fragment_page_size_and_board_confirmation(ctx, app, http, browser):
    ben = await AgentClient.create(app, http, 'ben')
    initial = await ben.call('board_post', {'title': 'Long thread', 'text': 'Start', 'mentions': ['person:ada']})
    thread = initial['thread_id']
    path = f'/web/board/t/{thread}'
    await login(ctx, browser.http, 'ada')
    home = await browser.http.get('/web/board')
    frame = SimpleNamespace(text=home.text.split(f'id="b{thread}">', 1)[1].split('</article>', 1)[0])
    values = reply_values(frame, path)
    first = await browser.http.post(path + '/reply', data=values)
    assert first.status_code == 303 and first.headers['location'].startswith(path)
    newest = await ben.call('board_post', {'thread_id': thread, 'text': 'New mention', 'mentions': ['person:ada']})
    replay = await browser.http.post(path + '/reply', data=values)
    sent = int(first.headers['location'].split('#p')[1])
    assert replay.headers['location'] == f'/web/board?sent_thread={thread}&sent={sent}#b{thread}'
    marked = await browser.http.get(replay.headers['location'])
    assert f'data-sent-thread="{thread}" data-sent="{values["draft"]}"' in marked.text
    async with ctx.pool.connection() as conn:
        await conn.execute("""INSERT INTO board_posts(thread_id,actor,kind,text)
            SELECT %s,'agent:ben','message','Update' FROM generate_series(1,51)""", (thread,))
    fragment = await browser.http.get(f'{path}/posts?after={newest["post_id"]}')
    ids = re.findall(r'<li class="post [^"]+" id="p([0-9]+)"', fragment.text)
    assert len(ids) == 50 and '<template id="next" data-more>' in fragment.text
    last = await browser.http.get(f'{path}/posts?after={ids[-1]}')
    assert last.text.count('<li class="post ') == 1 and 'data-more' not in last.text


async def test_board_live_refresh_preserves_drafts_in_chromium(ctx, app, http, browser):
    import os

    pw = pytest.importorskip('playwright.async_api')
    ben = await AgentClient.create(app, http, 'ben')
    initial = await ben.call('board_post', {'title': 'Live thread', 'text': 'Start', 'mentions': ['person:ada']})
    thread_id = initial['thread_id']
    path = f'/web/board/t/{thread_id}'
    key = f'illo-draft:ada:{thread_id}'
    await login(ctx, browser.http, 'ada')
    responses = []
    fragment_override = None

    async def respond(route):
        request = route.request
        headers = await request.all_headers()
        response = await browser.http.request(request.method, request.url, headers=headers, content=request.post_data_buffer)
        responses.append((request.method, request.url, response.status_code))
        body = response.content
        headers = dict(response.headers)
        if '/posts?after=' in request.url and fragment_override:
            body = fragment_override
            headers.pop('content-length', None)
        await route.fulfill(status=response.status_code, headers=headers, body=body)

    async with pw.async_playwright() as playwright:
        executable = os.getenv('ILLO_TEST_CHROMIUM') or playwright.chromium.executable_path
        if not os.path.isfile(executable):
            pytest.skip('Install Chromium with playwright install chromium to run this test.')
        chromium = await playwright.chromium.launch(executable_path=executable)
        session = await chromium.new_context()
        await session.route(BASE + '/**', respond)
        page = await session.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.clock.install()
        try:
            await page.goto(BASE + path)
            composer = page.locator('#reply textarea')
            await composer.fill('Keep this draft')
            await composer.evaluate('(element) => element.setSelectionRange(5, 5)')
            first = await ben.call('board_post', {'thread_id': thread_id, 'text': 'First update'})
            second = await ben.call('board_post', {'thread_id': thread_id, 'text': 'Second update'})
            async with page.expect_response(lambda response: '/posts?after=' in response.url):
                await page.clock.fast_forward(5000)
            await pw.expect(page.locator(f'#p{first["post_id"]}')).to_be_visible()
            await pw.expect(page.locator(f'#p{second["post_id"]}')).to_be_visible()
            await pw.expect(composer).to_have_value('Keep this draft')
            assert await composer.evaluate('(element) => element.selectionStart') == 5
            assert await page.locator('#reply details').evaluate('(element) => element.open')

            fragment_override = (b'<template id="next"><div id="foreign"></div></template>'
                                 b'<template id="status"><p>Broken poll</p></template>'
                                 b'<template id="reply-to"></template>')
            async with page.expect_response(lambda response: '/posts?after=' in response.url):
                await page.clock.fast_forward(5000)
            assert await page.locator('#foreign').count() == 0
            assert await page.get_by_text('Broken poll').count() == 0
            fragment_override = None

            before = len(responses)
            await page.evaluate("""() => {
                Object.defineProperty(document, 'hidden', {configurable: true, value: true});
                document.dispatchEvent(new Event('visibilitychange'));
            }""")
            await page.clock.fast_forward(25000)
            assert len(responses) == before
            await page.reload()
            await pw.expect(composer).to_have_value('Keep this draft')
            await page.goto(BASE + f'/web/board#b{thread_id}')
            frame = page.locator(f'#b{thread_id}')
            frame_text = frame.locator('textarea')
            await pw.expect(frame_text).to_have_value('Keep this draft')
            for width, delay in ((1280, 30000), (375, 60000)):
                await page.set_viewport_size({'width': width, 'height': 800})
                title = f'New visible thread at {width}'
                await ben.call('board_post', {'title': title, 'text': 'Board update'})
                async with page.expect_response(lambda response: response.url == BASE + '/web/board'):
                    await page.clock.fast_forward(delay)
                await pw.expect(page.locator('#threads').get_by_text(title)).to_be_visible()
                await pw.expect(frame_text).to_have_value('Keep this draft')

            await frame.get_by_role('button', name='Send').click()
            await page.wait_for_url(re.compile(re.escape(BASE + path) + r'\?sent=.+#p[0-9]+$'))
            assert await page.evaluate('(key) => localStorage.getItem(key)', key) is None
            await pw.expect(page.get_by_text('Keep this draft')).to_be_visible()
            async with ctx.pool.connection() as conn:
                await conn.execute('DELETE FROM web_sessions')
            async with page.expect_response(lambda response: '/posts?after=' in response.url and response.status == 401):
                await page.clock.fast_forward(5000)
            await pw.expect(page.get_by_role('link', name='Sign in again to see new posts.')).to_be_visible()
            assert not errors
        finally:
            await chromium.close()

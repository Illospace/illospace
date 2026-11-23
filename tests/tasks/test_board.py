"""The board shares real page access, decisions, and bounded ledger reads."""

import re
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


async def test_signin_static_empty_board_and_csp(ctx, browser):
    response = await browser.http.get('/web/board')
    assert response.status_code == 302
    assert response.headers['location'] == '/web/signin?next=%2Fweb%2Fboard'
    sign_in = await browser.http.get(response.headers['location'])
    assert '<form method="post"><button class="btn go">Get a code</button></form>' in sign_in.text
    script = await browser.http.get('/web/board.js')
    assert script.status_code == 200 and script.headers['content-type'].startswith('text/javascript')
    assert script.headers['cache-control'] == 'no-store'
    assert len(script.text.splitlines()) <= 250
    response = await browser.login(next='/web/board')
    assert response.status_code == 303 and response.headers['location'] == '/web/board'
    page = await browser.http.get('/web/board')
    for key, title, line in [('needs', 'Needs you', 'Nothing waits for you.'),
                             ('work', 'In work', 'No task is in work.'),
                             ('done', 'Done today', 'Nothing closed in the last 24 hours.')]:
        assert f'>{title}</h2>' in column(page, key) and line in column(page, key)
    assert "script-src 'self'" in page.headers['content-security-policy']
    assert page.headers['cache-control'] == 'no-store'
    assert page.text.count('<script') == 1 and '<script src="/web/board.js" defer></script>' in page.text
    assert not re.search(r'\sstyle\s*=', page.text)
    for label, text in [('Zoom out', '−'), ('Zoom in', '+'), ('Show all', 'Fit')]:
        assert f'aria-label="{label}">{text}</button>' in page.text
    assert signin.next_path('/web/board#t17') == '/web/board#t17'
    assert signin.next_path('/web/board#a17') == '/web/board#a17'
    for path in ['/web/board?next=//evil.example', '/web/board#bad', '/web/board//evil.example', '/web/board%0a']:
        assert signin.next_path(path) == ''


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
    assert len(queries) <= (160 if full else 70), len(queries)
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
    assert len(queries) <= 100, len(queries)
    assert sum('FROM items WHERE' in str(query) for query in queries) == 30

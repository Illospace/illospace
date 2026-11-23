"""Task pages use real Postgres, fake Slack, and local image repositories."""

import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from html import unescape

import httpx
import pytest
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from illo.asks import cards
from illo.channels.slack import tasks as slack_tasks
from illo.db import all_rows, one
from illo.ledger import digest_snapshot, people, runs, tasks
from illo.tasks import pages
from illo.tasks.pages import images, markdown, render, signin
from illo.tasks.pages.schema import Write
from illo.tools.registry import ToolContext
from illo.workspace import PagesConfig, TaskList
from tests.mcp.test_oauth import Flow
from tests.tasks.test_tools import agent

BASE = 'https://illo.example.com'
ANA = tasks.Viewer('ada', 'person:ada', human=True)
BUILD = tasks.Viewer('ada', 'agent:build')
TEXT = [{'kind': 'text', 'title': 'Note', 'text': 'The work is ready.'}]


@pytest.fixture
async def ctx(app, monkeypatch):
    ctx = app.state.ctx
    # Most tests want the review card at once. The tests of the wait for the page set the time again.
    monkeypatch.setattr(slack_tasks, 'HOLD_FOR_PAGE', 0)
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.config.pages = PagesConfig(enabled=True, url=BASE)
    ctx.workspace.lists += [TaskList(key='private', name='Private', owner='ada')]
    # The shared example uses a historical fixture key. The page displays the simple example name.
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE people SET name='Ana' WHERE key='ada'")
    return ctx


class Browser(Flow):
    async def start(self, next='/t/1'):
        # Only a POST makes a sign-in request. The page with the code follows.
        self.page = await self.http.post('/web/signin', params={'next': next})
        if self.page.status_code == 303:
            self.page = await self.http.get(self.page.headers['location'])
        return self.page

    async def login(self, *, next='/t/1', user='U_ACME_ADA', choice='approve'):
        await self.start(next=next)
        await self.dm(user=user)
        await self.click(choice=choice, user=user)
        return await self.http.get('/web/signin')


@pytest.fixture
async def browser(ctx, app, slack_client):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        yield Browser(app, client, slack_client)


async def make(ctx, *, returned=False, **extra):
    async with ctx.pool.connection() as conn:
        row = await tasks.create(conn, workspace=ctx.workspace, viewer=ANA, list_key=extra.pop('list_key', 'launches'),
                                 title=extra.pop('title', 'Review the work'), holder=extra.pop('holder', 'agent:build'), **extra)
        if returned:
            row = await tasks.write_result(conn, row['id'], workspace=ctx.workspace, viewer=BUILD,
                append='The result', message='Please review the result.', returned=True)
    return row


async def write(ctx, row, blocks=TEXT):
    async with ctx.pool.connection() as conn:
        return await pages.write(conn, ctx, ANA, {'task': row['id'], 'blocks': blocks})


def fields(response):
    return {k: unescape(v) for k, v in re.findall(r'name="(csrf|seen)" value="([^"]*)"', response.text)}


@pytest.mark.parametrize('value', ['https://key@illo.example.com', 'https://illo.example.com/?x=1',
                                  'https://illo.example.com/#x', 'http://illo.example.com', '//illo.example.com',
                                  'https://illo.example.com/?', 'https://illo.example.com/#'])
def test_setting_rejects_bad_base(value):
    with pytest.raises(ValidationError):
        PagesConfig(url=value)


def test_setting_base_and_next():
    for value in ['https://illo.example.com/', 'http://127.0.0.1:8000/', 'http://localhost:8000', 'http://[::1]']:
        assert PagesConfig(url=value).url == value.rstrip('/')
    assert PagesConfig().enabled is False
    for value in ['//evil.example/t/1', 'https://evil.example', '/list', '/t/../bad', '/t/%2f%2fevil.example', '/t/1\r\nLocation: bad']:
        assert signin.next_path(value) == ''
    assert signin.next_path('/t/3') == '/t/3'


async def test_disabled_routes_tools_and_links(ctx, browser, app, http):
    row = await make(ctx)
    ctx.workspace.config.pages.enabled = False
    for path, method in [('/web/signin', 'get'), ('/web/app.css', 'get'), ('/web/signout', 'post'),
                         (f'/t/{row["id"]}', 'get'), (f'/t/{row["id"]}/file/0', 'get'), (f'/t/{row["id"]}/decide', 'post')]:
        response = await getattr(http, method)(path)
        assert response.status_code == 404
        assert response.headers['content-security-policy'] == render.HEADERS['Content-Security-Policy']
    build = await agent(app, http, 'build')
    assert 'task_page' not in {tool['name'] for tool in (await build.rpc('tools/list')).json()['result']['tools']}
    await build.call('task_page', {'task': row['id'], 'blocks': TEXT}, error=True)
    async with ctx.pool.connection() as conn:
        person = await people.get(conn, 'ada')
        run = await runs.create(conn, principal='person:ada', person_id=person['id'], profile='person_dm', provider="fake", tool_limit=12, model='fake')
        tc = ToolContext(conn, run, ctx)
        assert not {'page_read', 'page_write'} & {t['name'] for t in await ctx.registry.list_tools_for(tc)}
        assert (await ctx.registry.call(tc, 'page_write', {'task': row['id'], 'blocks': TEXT}))['decision'] == 'deny'
    assert digest_snapshot.task_link(ctx.workspace, row) == digest_snapshot.thread_link(row)
    assert slack_tasks.title_link(ctx, row) == row['title']


async def test_signin_full_flow_same_browser_only_and_signout(browser, ctx, fake_brain, app):
    row = await make(ctx)
    response = await browser.start(next=f'/t/{row["id"]}')
    assert response.status_code == 200 and 'http-equiv="refresh"' in response.text
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'SameSite=lax' in cookie and 'Secure' in cookie and 'Path=/' in cookie
    code = re.search(r'connect [A-Z2-9]{4}-[A-Z2-9]{4}', response.text)[0]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as other:
        other_page = await other.get('/web/signin', params={'next': f'/t/{row["id"]}'})
        assert code not in other_page.text
        await browser.dm()
        assert 'browser wants to sign in' in browser.last_card()['text']
        await browser.click(user='U_ACME_BEN')
        assert (await browser.http.get('/web/signin')).status_code == 200
        await browser.click()
        assert (await other.get('/web/signin')).status_code == 200
        assert (await other.get(f'/t/{row["id"]}')).status_code == 302
        response = await browser.http.get('/web/signin')
        assert response.status_code == 303 and response.headers['location'] == f'/t/{row["id"]}'
        page = await browser.http.get(response.headers['location'])
        assert page.status_code == 200 and 'Ana' in page.text
    assert not fake_brain.requests
    async with ctx.pool.connection() as conn:
        stored = await one(conn, 'SELECT * FROM web_sessions')
        token = browser.http.cookies.get(signin.SESSION_COOKIE)
        assert token not in str(stored) and stored['token_hash'] == signin.digest(token)
        assert timedelta(days=29) < stored['expires_at'] - datetime.now(UTC) <= timedelta(days=30)
        events = await all_rows(conn, 'SELECT * FROM events')
        assert code not in str(events)
    bad = await browser.http.post('/web/signout', data={})
    assert bad.status_code == 403
    response = await browser.http.post('/web/signout', data={'csrf': fields(page)['csrf']})
    assert response.status_code == 303
    assert (await browser.http.get(f'/t/{row["id"]}')).status_code == 302
    async with ctx.pool.connection() as conn:
        assert not await one(conn, 'SELECT * FROM web_sessions')


@pytest.mark.parametrize('failure', ['deny', 'expired', 'bot', 'inactive', 'used', 'unknown', 'channel'])
async def test_signin_refusals(browser, ctx, failure, fake_brain):
    await browser.start()
    changes = {}
    if failure in {'expired', 'inactive'}:
        async with ctx.pool.connection() as conn:
            if failure == 'expired':
                await conn.execute("UPDATE web_signins SET expires_at=now()-interval '1 second'")
            else:
                await conn.execute("UPDATE people SET active=false WHERE key='ada'")
    if failure == 'bot':
        changes['bot_id'] = 'B_APP'
    if failure == 'channel':
        changes.update(channel='C_ACME_TEAM', channel_type='channel')
    if failure == 'unknown':
        changes['text'] = 'connect ZZZZ-ZZZZ'
    await browser.dm(**changes)
    if failure in {'deny', 'used'}:
        await browser.click(choice='deny' if failure == 'deny' else 'approve')
        await browser.dm()
        assert 'invalid, used, or expired' in browser.slack.calls[-1]['text']
    response = await browser.http.get('/web/signin')
    assert response.status_code == (303 if failure == 'used' else 200)
    if failure != 'used':
        assert not browser.http.cookies.get(signin.SESSION_COOKIE)
    assert not fake_brain.requests


async def test_app_signature_repeated_delivery_and_expired_click(browser, ctx):
    await browser.start()
    code = re.search(r'connect [A-Z2-9]{4}-[A-Z2-9]{4}', browser.page.text)[0]
    await browser.dm(text=code + '\n*Sent using* <@U123>')
    assert 'An app sent this code in your name.' in browser.last_card()['text']
    count = len(browser.slack.calls)
    browser.seq -= 1
    await browser.dm(text=code + '\n*Sent using* <@U123>')
    assert len(browser.slack.calls) == count
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE web_signins SET expires_at=now()-interval '1 second'")
    await browser.click()
    async with ctx.pool.connection() as conn:
        assert (await one(conn, 'SELECT decision FROM web_signins'))['decision'] == 'pending'


async def test_signin_limits_and_cleanup(ctx, browser, monkeypatch):
    monkeypatch.setattr(signin, 'STARTS_PER_HOST', 1)
    assert (await browser.start()).status_code == 200
    assert (await browser.start()).status_code == 429
    async with ctx.pool.connection() as conn:
        await conn.execute("DELETE FROM agent_oauth_limits WHERE kind LIKE 'web_%%'")
    monkeypatch.setattr(signin, 'MAX_PENDING', 1)
    assert (await browser.start()).status_code == 429
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE web_signins SET expires_at=now()-interval '1 second'")
        await conn.execute("INSERT INTO web_sessions(token_hash,person_id,expires_at) SELECT 'expired',id,now()-interval '1 second' FROM people LIMIT 1")
    await signin.cleanup(ctx)
    async with ctx.pool.connection() as conn:
        assert not await one(conn, 'SELECT * FROM web_signins')
        assert not await one(conn, 'SELECT * FROM web_sessions')


@pytest.mark.parametrize('next', ['//evil.example', 'https://evil.example/t/1', '/list', '/t/%2f%2fevil.example'])
async def test_next_does_not_redirect_outside(browser, next):
    result = await browser.login(next=next)
    assert result.headers['location'] == '/web/signin'
    assert 'You are signed in' in (await browser.http.get('/web/signin')).text


async def test_visibility_and_inactive_session(browser, ctx):
    private = await make(ctx, list_key='private')
    result = await browser.http.get(f'/t/{private["id"]}')
    assert result.status_code == 302 and result.headers['location'] == '/web/signin?next=%2Ft%2F' + str(private['id'])
    await browser.login(user='U_ACME_BEN')
    unseen = await browser.http.get(f'/t/{private["id"]}')
    missing = await browser.http.get('/t/999999')
    assert unseen.status_code == missing.status_code == 404 and unseen.text == missing.text
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE people SET active=false WHERE key='ben'")
    assert (await browser.http.get(f'/t/{private["id"]}')).status_code == 302


def test_markdown_headings_lists_tables_and_cut():
    html = markdown.render('# Title #\ntext\n### Part\n- a\n  - a1\n    more\n  1. n1\n- b\n\n|h|i|\n|-|-|\n|<b>|2|\n\n---\n```\n# code\n- code\n```')
    assert html == ('<h3>Title</h3><p>text</p><h4>Part</h4><ul><li>a<ul><li>a1<br>more</li></ul><ol><li>n1</li></ol></li><li>b</li></ul>'
                    '<div class="table"><table><tr><th>h</th><th>i</th></tr><tr><td>&lt;b&gt;</td><td>2</td></tr></table></div>'
                    '<hr><pre><code># code\n- code</code></pre>')
    assert markdown.render('#no heading\n- a\n\n      - deep') == '<p>#no heading</p><ul><li>a</li></ul><ul><li>deep</li></ul>'
    assert markdown.render('# C#\n## Part ##\n# ') == '<h3>C#</h3><h3>Part</h3><p>#</p>'
    assert markdown.render('|a||\n|-|-|\n||2|') == ('<div class="table"><table><tr><th>a</th><th></th></tr>'
                                                  '<tr><td></td><td>2</td></tr></table></div>')
    assert markdown.cut('short') == ('short', '')
    long = 'first\n\n```\n' + 'x\n\n' * 800 + '```\n\nlast part'
    head, rest = markdown.cut(long)
    assert head.endswith('```') and rest == 'last part'
    assert markdown.cut('x' * 5000) == ('x' * 5000, '')


def test_markdown_subset_and_hidden_text():
    html = markdown.render('**bold** *italic* `code`\nnext\n\n- one\n- two\n\n1. first\n\n> quote\n\n```html\n<script>x</script>\n```\n\n[x](https://example.com) https://example.com/a\n<!-- hidden --> <img src=x>')
    for expected in ['<strong>bold</strong>', '<em>italic</em>', '<code>code</code>', '<br>next', '<ul>', '<ol>', '<blockquote>', '<pre><code>', '&lt;script&gt;', 'rel="noopener noreferrer"']:
        assert expected in html
    assert '<script' not in html and '<img' not in html and 'hidden' not in html
    assert 'href="javascript:' not in markdown.render('[bad](javascript:alert(1))')


@pytest.mark.parametrize('block', [
    {'kind': 'text', 'text': 'x'*6001}, {'kind': 'text', 'title': 'x'*81, 'text': ''},
    {'kind': 'preview', 'name': 'a', 'label': 'x'*81, 'text': ''},
    {'kind': 'checks', 'items': []}, {'kind': 'checks', 'items': [{'claim': 'x', 'state': 'bad'}]},
    {'kind': 'checks', 'items': [{'claim': 'x', 'state': 'yes', 'source_url': 'javascript:bad'}]},
    {'kind': 'checks', 'items': [{'claim': 'x', 'state': 'yes', 'checked_at': '2026-11-18'}]},
    {'kind': 'bars', 'title': '', 'rows': [{'label': '', 'value': float('nan')}]},
    {'kind': 'bars', 'title': '', 'rows': [{'label': '', 'value': -1}]},
    {'kind': 'html', 'text': '<script>'},
])
def test_block_schema_limits(block):
    with pytest.raises(ValidationError):
        Write.model_validate({'task': 1, 'blocks': [block]})
    with pytest.raises(ValidationError):
        Write.model_validate({'task': 1, 'blocks': TEXT * 13})


async def test_render_all_blocks_escape_fields_headers_and_version(browser, ctx, monkeypatch):
    attack = '<script>alert("x")</script>'
    row = await make(ctx, title=attack, brief=attack, returned=True)
    async def missing(*args, **kwargs):
        raise ValueError('missing')
    monkeypatch.setattr(images, 'read', missing)
    blocks = [
        {'kind': 'text', 'title': attack, 'text': attack + '\n<!-- marker -->'},
        {'kind': 'preview', 'name': attack, 'label': attack, 'text': attack},
        {'kind': 'checks', 'title': attack, 'items': [{'claim': attack, 'state': 'yes', 'source_url': 'https://example.com/"bad'}]},
        {'kind': 'bars', 'title': attack, 'rows': [{'label': attack[:40], 'value': 4, 'unit': '<bad>'}]},
    ]
    await write(ctx, row, blocks)
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE tasks SET result=%s,result_version=result_version+1,links=%s WHERE id=%s", (attack, Jsonb([
            {'url': 'javascript:alert(1)', 'title': attack, 'kind': 'evidence'},
            {'url': 'https://example.com/" onclick="bad', 'title': attack, 'kind': 'evidence'}]), row['id']))
        await tasks._event(conn, row, 'agent:build', 'message', attack + '<!-- secret marker -->')
        page = await one(conn, 'SELECT * FROM task_pages WHERE task_id=%s', (row['id'],))
        assert page['blocks'][2]['items'][0]['checked_at']
        # Insert a formerly valid image reference. A missing mirror is a normal read state.
        await conn.execute("UPDATE task_pages SET blocks=blocks || %s WHERE task_id=%s", (Jsonb([{'kind': 'image', 'image': {'repo': 'acme-example/engine', 'ref': 'f'*40, 'path': 'x.png'}, 'caption': attack, 'alt': attack}]), row['id']))
    await browser.login()
    response = await browser.http.get(f'/t/{row["id"]}')
    assert response.status_code == 200
    assert '<script' not in response.text and ' style=' not in response.text and '<style' not in response.text
    assert '&lt;script&gt;' in response.text and 'marker' not in response.text
    assert 'href="javascript:' not in response.text and '" onclick=' not in response.text
    for name, value in render.HEADERS.items():
        assert response.headers[name] == value
    for expected in ['class="block preview"', 'class="checks"', '<progress', 'class="missing"', 'This page can show older work.', 'The brief', 'Returned work', 'History (']:
        assert expected in response.text
    css = await browser.http.get('/web/app.css')
    assert css.headers['cache-control'] == 'no-store' and '--radius:20px' in css.text
    assert (await browser.http.get(f'/t/{row["id"]}/file/4')).status_code == 404


async def test_replace_and_tool_profiles(ctx, app, http):
    row = await make(ctx)
    first = await write(ctx, row)
    second = await write(ctx, row, [])
    assert second['blocks'] == [] and second['result_version'] == row['result_version']
    assert second['written_at'] > first['written_at']
    for profile, writes, reads in [('person_dm', True, True), ('routine', True, True), ('team_channel', False, True), ('tainted', False, False), ('outsider', False, False)]:
        async with ctx.pool.connection() as conn:
            person = await people.get(conn, 'ada')
            run = await runs.create(conn, principal='outsider' if profile == 'outsider' else 'person:ada', person_id=person['id'] if profile != 'outsider' else None, profile='person_dm' if profile == 'outsider' else profile, provider="fake", tool_limit=12, model='fake')
            tc = ToolContext(conn, run, ctx)
            names = {t['name'] for t in await ctx.registry.list_tools_for(tc)}
            assert ('page_write' in names) is writes
            assert ('page_read' in names) is reads
    build = await agent(app, http, 'build')
    studio = await agent(app, http, 'studio', 'ben')
    assert 'task_page' in str((await build.rpc('tools/list')).json())
    await studio.call('task_page', {'task': row['id'], 'blocks': TEXT}, error=True)
    value = await build.call('task_page', {'task': row['id'], 'blocks': TEXT})
    assert value['url'] == BASE + f'/t/{row["id"]}'
    assert value['blocks'] == TEXT
    private = await make(ctx, list_key='private')
    async with ctx.pool.connection() as conn:
        with pytest.raises(tasks.TaskError):
            await pages.write(conn, ctx, tasks.Viewer('ben', 'person:ben', human=True), {'task': private['id'], 'blocks': TEXT})


@pytest.fixture
def mirror(ctx, tmp_path):
    root = tmp_path / 'mirrors'
    path = root / 'acme-example/engine'
    path.mkdir(parents=True)
    ctx.config.repos_dir = root
    ctx.workspace.config.sandbox.repositories = ['acme-example/engine']
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    def git(*args):
        return subprocess.check_output(['git', '-C', str(path), *args], env=env, stderr=subprocess.DEVNULL).decode().strip()
    git('init', '-b', 'main')
    git('config', 'user.email', 'ana@example.com')
    git('config', 'user.name', 'Ana')
    data = b'\x89PNG\r\n\x1a\n' + b'fake image bytes'
    (path / 'picture.png').write_bytes(data)
    (path / 'plain.txt').write_text('Not an image')
    (path / 'large.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'x' * images.MAX_BYTES)
    git('add', '.')
    git('commit', '-m', 'Add test images')
    sha = git('rev-parse', 'HEAD')
    return path, git, sha, data


async def test_images_pin_commit_and_require_visibility(ctx, browser, app, mirror):
    path, git, sha, data = mirror
    image = {'repo': 'acme-example/engine', 'ref': 'main', 'path': 'picture.png'}
    row = await make(ctx, list_key='private')
    page = await write(ctx, row, [{'kind': 'image', 'image': image, 'caption': 'Picture', 'alt': 'The picture'}])
    assert page['blocks'][0]['image']['ref'] == sha
    (path / 'picture.png').write_bytes(b'GIF89aChanged')
    git('add', '.')
    git('commit', '-m', 'Change the picture')
    route = f'/t/{row["id"]}/file/0'
    assert (await browser.http.get(route)).status_code == 404
    await browser.login()
    result = await browser.http.get(route)
    assert result.content == data and result.headers['content-type'] == 'image/png'
    assert result.headers['content-security-policy'] == "default-src 'none'"
    assert result.headers['cache-control'] == 'private, no-store'
    assert result.headers['content-disposition'] == 'inline'
    assert result.headers['x-content-type-options'] == 'nosniff'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as http:
        other = Browser(app, http, browser.slack)
        await other.login(user='U_ACME_BEN')
        assert (await other.http.get(route)).status_code == 404
    # A pinned object can disappear after a mirror refresh. Reads must fail closed.
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE task_pages SET blocks=jsonb_set(blocks,'{0,image,ref}',%s) WHERE task_id=%s", (Jsonb('f'*40), row['id']))
    assert 'class="missing"' in (await browser.http.get(f'/t/{row["id"]}')).text
    assert (await browser.http.get(route)).status_code == 404


@pytest.mark.parametrize('change', [{'path': '../picture.png'}, {'path': '/picture.png'}, {'path': '-picture.png'},
                                     {'path': 'bad\x00.png'}, {'repo': 'other/project'}, {'path': 'plain.txt'},
                                     {'path': 'large.png'}, {'path': 'missing.png'}, {'ref': '--help'}])
async def test_image_refusals(ctx, mirror, change):
    row = await make(ctx)
    image = {'repo': 'acme-example/engine', 'ref': 'main', 'path': 'picture.png', **change}
    with pytest.raises((ValidationError, tasks.TaskError)):
        await write(ctx, row, [{'kind': 'image', 'image': image, 'caption': '', 'alt': ''}])
    async with ctx.pool.connection() as conn:
        assert not await one(conn, 'SELECT * FROM task_pages')


@pytest.mark.parametrize('data,expected', [(b'GIF87aabc', 'image/gif'), (b'\xff\xd8\xffx', 'image/jpeg'),
                                          (b'RIFFabcdWEBPx', 'image/webp')])
def test_image_magic(data, expected):
    assert images.media_type(data) == expected


async def test_decision_guards_stale_result_and_send_back(browser, ctx):
    row = await make(ctx, returned=True)
    await write(ctx, row)
    await browser.login()
    route = f'/t/{row["id"]}'
    form = fields(await browser.http.get(route))
    for extra in [{'csrf': ''}, {'csrf': 'wrong'}, {'csrf': 'é'}]:
        response = await browser.http.post(route + '/decide', data={**form, 'action': 'done', **extra})
        assert response.status_code == 403
    response = await browser.http.post(route + '/decide', data={**form, 'action': 'done'}, headers={'Origin': 'https://evil.example'})
    assert response.status_code == 403
    await write(ctx, row, [{'kind': 'text', 'text': 'A new page'}])
    response = await browser.http.post(route + '/decide', data={**form, 'action': 'done'})
    assert 'The work changed after you opened the page. Read it again.' in response.text
    form = fields(response)
    for reason in ['', ' ' , 'x'*4001]:
        response = await browser.http.post(route + '/decide', data={**form, 'action': 'reopen', 'reason': reason})
        assert response.status_code == 200
    response = await browser.http.post(route + '/decide', data={**form, 'action': 'reopen', 'reason': 'Use the shorter text.'}, headers={'Origin': BASE})
    assert response.status_code == 303
    page = await browser.http.get(route)
    assert 'You sent this back on' in page.text and 'Use the shorter text.' in page.text
    async with ctx.pool.connection() as conn:
        current = await tasks.get(conn, row['id'], viewer=ANA)
        assert current['state'] == 'todo' and current['waits_for'] == 'agent:build'
        event = await one(conn, "SELECT * FROM task_events WHERE task_id=%s AND kind='reopened'", (row['id'],))
        wake = await one(conn, 'SELECT * FROM task_wakes WHERE event_id=%s', (event['id'],))
        assert event['actor'] == 'person:ada' and event['text'] == 'Use the shorter text.'
        assert wake['holder'] == 'agent:build' and wake['reason'] == 'task.reopened'
    # A stale result or state cannot reuse an old form.
    response = await browser.http.post(route + '/decide', data={**form, 'action': 'done'})
    assert 'The work changed' in response.text


async def test_approve_has_same_event_parent_and_card_updates(browser, ctx):
    parent = await make(ctx, title='The whole')
    row = await make(ctx, parent_id=parent['id'], returned=True)
    await slack_tasks.tick(ctx)
    await browser.login()
    route = f'/t/{row["id"]}'
    response = await browser.http.post(route + '/decide', data={**fields(await browser.http.get(route)), 'action': 'done'})
    assert response.status_code == 303
    async with ctx.pool.connection() as conn:
        current = await tasks.get(conn, row['id'], viewer=ANA)
        assert current['state'] == 'done' and current['done_by'] == 'person:ada'
        event = await one(conn, "SELECT * FROM task_events WHERE task_id=%s AND kind='done'", (row['id'],))
        assert event['actor'] == 'person:ada' and event['data']['force'] is False
        assert (await tasks.get(conn, parent['id'], viewer=ANA))['state'] == 'review'
    assert any(call['method'] == 'chat_update' and call.get('blocks') and call['blocks'][-1].get('text', {}).get('text') == 'Done.' for call in ctx.slack_client.calls)
    assert 'You approved this on' in (await browser.http.get(route)).text


async def test_failed_checks_require_reason(browser, ctx, monkeypatch):
    row = await make(ctx, returned=True)
    async def proof(*args, **kwargs):
        return [{'name': 'test', 'ok': False, 'text': 'The proof is absent.'}]
    monkeypatch.setattr(tasks, 'proof', proof)
    await browser.login()
    route = f'/t/{row["id"]}'
    form = fields(await browser.http.get(route))
    response = await browser.http.post(route + '/decide', data={**form, 'action': 'done'})
    assert 'The proof is absent.' in response.text and 'Confirm anyway…' in response.text
    response = await browser.http.post(route + '/decide', data={**form, 'action': 'force', 'reason': ''})
    assert response.status_code == 200
    response = await browser.http.post(route + '/decide', data={**form, 'action': 'force', 'reason': 'I checked the work myself.'})
    assert response.status_code == 303
    async with ctx.pool.connection() as conn:
        event = await one(conn, "SELECT * FROM task_events WHERE task_id=%s AND kind='done'", (row['id'],))
        assert event['data']['force'] is True and event['text'] == 'I checked the work myself.'


async def test_unallowed_reviewer_has_no_buttons(browser, ctx):
    row = await make(ctx, returned=True)
    await browser.login(user='U_ACME_BEN')
    route = f'/t/{row["id"]}'
    response = await browser.http.get(route)
    assert '>Approve<' not in response.text and 'Send back…' not in response.text
    assert (await browser.http.post(route + '/decide', data={**fields(response), 'action': 'done'})).status_code == 403


async def test_message_uses_existing_wake_path(browser, ctx):
    row = await make(ctx)
    await browser.login()
    route = f'/t/{row["id"]}'
    response = await browser.http.get(route)
    assert 'Message the holder…' in response.text
    response = await browser.http.post(route + '/decide', data={**fields(response), 'action': 'message', 'reason': 'Please check the proof.'})
    assert response.status_code == 303
    async with ctx.pool.connection() as conn:
        event = await one(conn, "SELECT * FROM task_events WHERE task_id=%s AND kind='message'", (row['id'],))
        assert event['text'] == 'Please check the proof.' and event['data']['wake'] is True
        assert await one(conn, 'SELECT * FROM task_wakes WHERE event_id=%s', (event['id'],))


async def test_links_briefs_cards_and_reminders(ctx):
    row = await make(ctx, returned=True)
    assert digest_snapshot.task_link(ctx.workspace, row) == BASE + f'/t/{row["id"]}'
    await slack_tasks.tick(ctx)
    cards = [call for call in ctx.slack_client.calls if call['method'] == 'chat_postMessage' and call.get('blocks')]
    assert BASE + f'/t/{row["id"]}' in str(cards[-1]['blocks'])
    async with ctx.pool.connection() as conn:
        await tasks._event(conn, row, 'code', 'reminded', 'Please review.', {'to': 'person:ada', 'source': 'wait_check'})
    ctx.workspace.config.tasks.reminder_window = (0, 24)
    await slack_tasks.tick(ctx)
    assert any('Please review.' in c.get('text', '') and BASE + f'/t/{row["id"]}' in c['text'] for c in ctx.slack_client.calls)
    ctx.workspace.config.pages.enabled = False
    assert digest_snapshot.task_link(ctx.workspace, row) == digest_snapshot.thread_link(row)


async def test_browser_send_back_reaches_the_agent_ticket(browser, ctx, github_client):
    from illo.tasks import wake

    row = await make(ctx, list_key='bugs')
    await wake.tick(ctx)
    async with ctx.pool.connection() as conn:
        await tasks.return_work(conn, row['id'], workspace=ctx.workspace, viewer=BUILD, message='Please check the work.', ctx=ctx)
    await browser.login()
    route = f'/t/{row["id"]}'
    form = fields(await browser.http.get(route))
    assert (await browser.http.post(route + '/decide', data={**form, 'action': 'reopen', 'reason': 'Use CSV.\nKeep the date.'})).status_code == 303
    await wake.tick(ctx)
    comments = [call['body'] for call in github_client.calls if call['method'] == 'comment_issue']
    assert any(f"Task {row['id']}: Use CSV.\nKeep the date." in body and '<!-- illo:wake:' in body for body in comments)
    assert 'Illo' in (await browser.http.get(route)).text


async def test_wait_check_can_write_only_its_bound_page(ctx, fake_brain):
    from illo.deep import worker
    from illo.tasks import release
    from tests.tasks.test_wake_release import age
    from tests.tool_results import read_result

    row = await make(ctx, returned=True)
    other = await make(ctx)
    await age(ctx, row['id'], 40)
    ctx.workspace.config.tasks.reminder_window = (0, 24)
    await release.tick(ctx)
    fake_brain.script = [
        {'name': 'page_read', 'arguments': {'task': row['id']}},
        {'name': 'page_write', 'arguments': {'task': row['id'], 'blocks': TEXT}},
        {'name': 'page_write', 'arguments': {'task': other['id'], 'blocks': TEXT}},
        {'name': 'wait_result', 'arguments': {'outcome': 'person', 'message': 'Review the returned work on its page.'}},
    ]
    deep = await worker.tick(ctx)
    assert deep['state'] == 'done'
    results = [read_result(value) for value in fake_brain.results]
    assert [result['decision'] for result in results] == ['allow', 'allow', 'deny', 'allow']
    assert results[1]['result']['url'] == BASE + f'/t/{row["id"]}'
    async with ctx.pool.connection() as conn:
        assert await one(conn, 'SELECT * FROM task_pages WHERE task_id=%s', (row['id'],))
        assert not await one(conn, 'SELECT * FROM task_pages WHERE task_id=%s', (other['id'],))


async def test_session_expiry_local_http_and_every_html_header(ctx, browser):
    ctx.config.web_allow_http = True
    response = await browser.start(next='//evil.example')
    assert 'Secure' not in response.headers['set-cookie']
    for name, value in render.HEADERS.items():
        assert response.headers[name] == value
    await browser.dm()
    await browser.click()
    response = await browser.http.get('/web/signin')
    assert response.status_code == 303
    for name, value in render.HEADERS.items():
        assert response.headers[name] == value
    response = await browser.http.get('/web/signin')
    assert 'You are signed in' in response.text
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE web_sessions SET expires_at=clock_timestamp()-interval '1 second'")
    assert (await browser.http.get('/t/1')).status_code == 302


async def test_page_read_keeps_the_whole_block_list(ctx):
    row = await make(ctx)
    blocks = [{'kind': 'text', 'text': 'x' * 6000} for _ in range(12)]
    await write(ctx, row, blocks)
    async with ctx.pool.connection() as conn:
        person = await people.get(conn, 'ada')
        run = await runs.create(conn, principal='person:ada', person_id=person['id'], profile='person_dm', provider="fake", tool_limit=12, model='fake')
        result = await ctx.registry.call(ToolContext(conn, run, ctx), 'page_read', {'task': row['id']})
        assert result['decision'] == 'allow' and len(result['result']['blocks']) == 12
        assert result['result']['blocks'][-1]['text'] == 'x' * 6000


@pytest.mark.parametrize('kind,write', [('request', True), ('check', True), ('scout', False), ('brief', False)])
async def test_page_tool_deep_grants(ctx, kind, write):
    from illo.tools.checks import scope

    run = {'principal': 'person:ada' if kind == 'request' else 'team', 'person_id': 1 if kind == 'request' else None,
           'profile': 'person_dm' if kind == 'request' else 'routine', 'deep_kind': kind,
           'tainted': False, 'personal_read': False, 'pages_enabled': True}
    names = {t['name'] for t in ctx.registry.list_tools(run)}
    assert 'page_read' in names and ('page_write' in names) is write
    assert scope({**run, 'web_read': True}, ctx.registry.tools['page_write']).outcome == ('allow' if kind == 'check' else 'deny')
    assert 'page_write' not in {t['name'] for t in ctx.registry.list_tools({**run, 'tainted': True})}


async def test_changed_result_and_history_collapse(ctx, browser):
    row = await make(ctx, returned=True)
    await browser.login()
    route = f'/t/{row["id"]}'
    form = fields(await browser.http.get(route))
    async with ctx.pool.connection() as conn:
        await conn.execute('UPDATE tasks SET result_version=result_version+1 WHERE id=%s', (row['id'],))
        for _ in range(3):
            await tasks._event(conn, row, 'code', 'reminded', 'Please review.', {'to': 'person:ada'})
            await tasks._event(conn, row, 'code', 'message', 'The check ran.')
    response = await browser.http.post(route + '/decide', data={**form, 'action': 'done'})
    assert 'The work changed after you opened the page. Read it again.' in response.text
    assert response.text.count('Please review.') == 1 and 'Reminders: 3' in response.text
    async with ctx.pool.connection() as conn:
        assert (await tasks.get(conn, row['id'], viewer=ANA))['state'] == 'review'


async def test_reading_the_signin_page_makes_no_request(browser, ctx):
    # A link preview or a crawler only reads. It must not use the start limit or make a row.
    for _ in range(3):
        response = await browser.http.get('/web/signin', params={'next': '/t/1'})
        assert response.status_code == 200 and 'Get a code' in response.text
        assert 'connect ' not in response.text and 'set-cookie' not in response.headers
    assert (await browser.http.get('/t/1', follow_redirects=True)).status_code == 200
    async with ctx.pool.connection() as conn:
        assert not await one(conn, 'SELECT id FROM web_signins')
        assert not await one(conn, "SELECT 1 FROM agent_oauth_limits WHERE kind LIKE 'web_%%'")


async def test_signin_start_from_another_origin_is_refused(browser, ctx):
    response = await browser.http.post('/web/signin', headers={'Origin': 'https://evil.example'})
    assert response.status_code == 403
    async with ctx.pool.connection() as conn:
        assert not await one(conn, 'SELECT id FROM web_signins')


async def test_a_browser_that_hides_its_origin_can_use_the_forms(browser, ctx):
    # Real browsers sent "Origin: null" with each form while the policy was "no-referrer", and each form failed.
    assert render.HEADERS['Referrer-Policy'] == 'same-origin'
    hidden = {'Origin': 'null', 'Sec-Fetch-Site': 'same-origin'}
    assert (await browser.http.post('/web/signin', headers=hidden)).status_code == 303
    for site in ('cross-site', 'same-site'):
        refused = await browser.http.post('/web/signin', headers={'Origin': BASE, 'Sec-Fetch-Site': site})
        assert refused.status_code == 403
    assert (await browser.http.post('/web/signin', headers={'Origin': 'null'})).status_code == 403


async def press_open(ctx, slack_client, task_id, key='ada'):
    async with ctx.pool.connection() as conn:
        person = await people.get(conn, key)
    before = len(slack_client.calls)
    await ctx.slack_actions.dispatch(ctx, person, {'action_id': f'web-open:{task_id}', 'trigger_id': 'T1'})
    views = [c for c in slack_client.calls[before:] if c['method'] == 'views_open']
    return views[0]['view']['blocks'][0]['text']['text'] if views else None


def link_of(text):
    return unescape(re.search(r'<(https://[^|>]+)\|', text)[1]).removeprefix(BASE)


async def test_open_button_gives_a_link_that_signs_in_one_time(browser, ctx, slack_client, app):
    row = await make(ctx, returned=True)
    card = cards.task_return_card({**row, 'page_url': pages.url(ctx, row['id'])}, 'Returned', [])
    assert [b['action_id'] for b in card[-1]['elements']] == [
        f"web-open:{row['id']}", f"task:{row['id']}:done", f"task:{row['id']}:back"]
    assert [b.get('style') for b in card[-1]['elements']] == ['primary', None, None]
    assert len(cards.task_return_card(row, 'Returned', [])[-1]['elements']) == 2
    text = await press_open(ctx, slack_client, row['id'])
    assert 'as Ana' in text and 'one time' in text
    path = link_of(text)
    assert path.startswith('/web/signin?key=')
    key = path.split('=', 1)[1]

    # Reading the link changes nothing. A link preview can read it many times.
    for _ in range(2):
        shown = await browser.http.get(path)
        assert shown.status_code == 200 and 'Sign in as Ana' in shown.text and 'set-cookie' not in shown.headers
        # The stylesheet request and the form request must not carry the address, which holds the key.
        assert shown.headers['referrer-policy'] == 'strict-origin'
    assert (await browser.http.post('/web/signin', data={'key': key}, headers={'Sec-Fetch-Site': 'cross-site'})).status_code == 403
    done = await browser.http.post('/web/signin', data={'key': key}, headers={'Sec-Fetch-Site': 'same-origin'})
    assert done.status_code == 303 and done.headers['location'] == f"/t/{row['id']}"
    assert signin.SESSION_COOKIE in done.headers['set-cookie']
    assert (await browser.http.get(done.headers['location'])).status_code == 200

    # The key is used. Another browser gets nothing from it.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as other:
        ended = await other.get(path)
        assert ended.status_code == 410 and ended.headers['referrer-policy'] == 'strict-origin'
        again = await other.post('/web/signin', data={'key': key})
        assert again.status_code == 410 and 'set-cookie' not in again.headers
        assert (await other.get('/web/signin', params={'key': 'x' * 43})).status_code == 410


async def test_link_does_not_change_a_signed_in_browser(browser, ctx, slack_client):
    row = await make(ctx, returned=True)
    assert (await browser.login(next=f"/t/{row['id']}")).status_code in {302, 303}
    cookie = browser.http.cookies.get(signin.SESSION_COOKIE)
    own = link_of(await press_open(ctx, slack_client, row['id']))
    other = link_of(await press_open(ctx, slack_client, row['id'], key='ben'))
    for path in (own, other):
        response = await browser.http.get(path)
        assert response.status_code == 303 and response.headers['location'] == f"/t/{row['id']}"
        assert 'set-cookie' not in response.headers and browser.http.cookies.get(signin.SESSION_COOKIE) == cookie
    async with ctx.pool.connection() as conn:
        rows = await all_rows(conn, """SELECT p.key,s.continued_at IS NOT NULL AS used FROM web_signins s
            JOIN people p ON p.id=s.person_id WHERE s.pairing_hash<>'' AND s.slack_ts IS NULL ORDER BY s.id""")
    # The key of the signed-in person ends. The key of the other person stays for that person.
    assert [(r['key'], r['used']) for r in rows] == [('ada', True), ('ben', False)]


async def test_open_button_refusals_and_limit(browser, ctx, slack_client, monkeypatch):
    private = await make(ctx, list_key='private', holder='person:ada')
    assert await press_open(ctx, slack_client, private['id'], key='ben') is None
    assert await press_open(ctx, slack_client, 999_999) is None
    row = await make(ctx, returned=True)
    ctx.workspace.config.pages.enabled = False
    assert await press_open(ctx, slack_client, row['id']) is None
    off = await browser.http.get('/web/signin', params={'key': 'x' * 43})
    assert off.status_code == 404 and off.headers['referrer-policy'] == 'strict-origin'
    ctx.workspace.config.pages.enabled = True
    monkeypatch.setattr(signin, 'LINKS_PER_HOUR', 1)
    assert 'Open the page' in await press_open(ctx, slack_client, row['id'])
    assert 'too many links' in await press_open(ctx, slack_client, row['id'])
    # An ended link and an inactive person get the same answer.
    monkeypatch.setattr(signin, 'LINKS_PER_HOUR', 9)
    path = link_of(await press_open(ctx, slack_client, row['id']))
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE people SET active=false WHERE key='ada'")
    assert (await browser.http.get(path)).status_code == 410
    refused = await browser.http.post('/web/signin', data={'key': path.split('=', 1)[1]})
    assert refused.status_code == 410 and 'set-cookie' not in refused.headers


async def test_agent_page_on_returned_work_tells_the_reviewer_one_time(ctx, slack_client):
    row = await make(ctx, returned=True)
    await slack_tasks.tick(ctx)
    start = len(slack_client.calls)

    async def notices():
        await slack_tasks.tick(ctx)
        return [c for c in slack_client.calls[start:] if c['method'] == 'chat_postMessage'
                and 'ready for your review' in c['text']]

    # A person who writes the page knows it. An agent that writes it must tell the reviewer.
    await write(ctx, row)
    assert not await notices()
    async with ctx.pool.connection() as conn:
        await pages.write(conn, ctx, BUILD, {'task': row['id'], 'blocks': TEXT + TEXT})
    sent = await notices()
    assert sent and all(f"/t/{row['id']}|" in c['text'] for c in sent)
    assert all(c['blocks'][-1]['elements'][0]['action_id'] == f"web-open:{row['id']}" for c in sent)
    # The same blocks again are no news. A check with no time gets the present time on each write.
    checks = [*TEXT, {'kind': 'checks', 'title': 'Checks', 'items': [{'claim': 'The release passed', 'state': 'yes'}]}]
    async with ctx.pool.connection() as conn:
        await pages.write(conn, ctx, BUILD, {'task': row['id'], 'blocks': TEXT + TEXT})
        await pages.write(conn, ctx, BUILD, {'task': row['id'], 'blocks': checks})
    assert len(await notices()) == 2 * len(sent)
    async with ctx.pool.connection() as conn:
        await pages.write(conn, ctx, BUILD, {'task': row['id'], 'blocks': checks})
    assert len(await notices()) == 2 * len(sent)
    # A page on a task in work has no reviewer yet.
    working = await make(ctx)
    async with ctx.pool.connection() as conn:
        await pages.write(conn, ctx, BUILD, {'task': working['id'], 'blocks': TEXT})
    assert len(await notices()) == 2 * len(sent)


async def test_returned_work_gets_one_look_that_fills_the_page(ctx, fake_brain, monkeypatch):
    from illo.deep import worker
    from illo.tasks import release, waits

    monkeypatch.setattr(slack_tasks, 'HOLD_FOR_PAGE', 360)
    row = await make(ctx, returned=True)
    await write(ctx, row)
    async with ctx.pool.connection() as conn:
        assert (await pages.read(conn, ctx, ANA, row['id']))['stale'] is False
        # A return through a ticket adds no result version. The page is still older than the work.
        await tasks._event(conn, row, 'agent:build', 'returned', 'The ticket is closed as completed.')
        assert (await pages.read(conn, ctx, ANA, row['id']))['stale'] is True
    for _ in range(2):
        await release.tick(ctx)
    async with ctx.pool.connection() as conn:
        deep = await all_rows(conn, "SELECT * FROM deep_runs WHERE kind='check'")
    # One look for this wait, at once: no silence limit. The job names the page tools.
    assert len(deep) == 1 and deep[0]['context']['wait']['round'] == waits.AT_RETURN
    assert deep[0]['context']['wait']['task_id'] == row['id'] and '`stale` is true' in deep[0]['job']
    def cards():
        return [c for c in ctx.slack_client.calls if c['method'] == 'chat_postMessage' and c.get('blocks')]

    # The review card waits for the look, so that one message opens the page with the work on it.
    await slack_tasks.tick(ctx)
    assert not cards()
    fake_brain.script = [{'name': 'page_write', 'arguments': {'task': row['id'], 'blocks': TEXT + TEXT}},
                         {'name': 'wait_result', 'arguments': {'outcome': 'person', 'message': 'Review the new draft. An agent cannot approve it.'}}]
    assert (await worker.tick(ctx))['state'] == 'done'
    await slack_tasks.tick(ctx)
    async with ctx.pool.connection() as conn:
        page = await pages.read(conn, ctx, ANA, row['id'])
        events = await all_rows(conn, 'SELECT * FROM task_events WHERE task_id=%s ORDER BY id', (row['id'],))
    assert page['stale'] is False and len(page['blocks']) == 2
    # This look sends no reminder. The silence limit still decides the first reminder.
    assert not [e for e in events if e['kind'] == 'reminded']
    assert events[-1]['kind'] == 'message' and events[-1]['data']['source'] == 'wait_check'
    # One card: Illo's words say what to review, and the first button opens the page.
    await slack_tasks.tick(ctx)
    assert len(cards()) == 1
    card = cards()[0]['blocks']
    assert 'Review the new draft. An agent cannot approve it.' in str(card) and 'The ticket is closed' not in str(card)
    assert card[-1]['elements'][0]['action_id'] == f"web-open:{row['id']}"
    # With pages off, a returned task gets no look before the silence limit.
    ctx.workspace.config.pages.enabled = False
    other = await make(ctx, returned=True)
    await release.tick(ctx)
    async with ctx.pool.connection() as conn:
        assert not await one(conn, "SELECT id FROM deep_runs WHERE context->'wait'->>'task_id'=%s", (str(other['id']),))


async def test_review_card_does_not_wait_long_for_the_page(ctx, monkeypatch):
    monkeypatch.setattr(slack_tasks, 'HOLD_FOR_PAGE', 360)
    row = await make(ctx, returned=True)

    def cards():
        return [c for c in ctx.slack_client.calls if c['method'] == 'chat_postMessage' and c.get('blocks')]

    await slack_tasks.tick(ctx)
    assert not cards()
    # No look came in time. The card goes out with the holder's words.
    monkeypatch.setattr(slack_tasks, 'HOLD_FOR_PAGE', 0)
    await slack_tasks.tick(ctx)
    assert len(cards()) == 1 and 'Please review the result.' in str(cards()[0]['blocks'])
    # The look ends late. The card gets Illo's words, and one message says that the page is ready.
    async with ctx.pool.connection() as conn:
        await tasks._event(conn, row, 'code', 'message', 'Review the new draft.', {'source': 'wait_check'})
    for _ in range(2):
        await slack_tasks.tick(ctx)
    update = [c for c in ctx.slack_client.calls if c['method'] == 'chat_update' and c.get('blocks')][-1]
    assert 'Review the new draft.' in str(update['blocks']) and 'Please review the result.' not in str(update['blocks'])
    ready = [c for c in ctx.slack_client.calls if c['method'] == 'chat_postMessage' and 'now shows the returned work' in c['text']]
    assert len(ready) == 1 and ready[0]['blocks'][-1]['elements'][0]['action_id'] == f"web-open:{row['id']}"


async def test_cancelled_look_ends_the_wait_of_the_card(ctx, monkeypatch):
    from illo.tasks import release

    monkeypatch.setattr(slack_tasks, 'HOLD_FOR_PAGE', 360)
    await make(ctx, returned=True)
    await release.tick(ctx)
    await slack_tasks.tick(ctx)
    cards = lambda: [c for c in ctx.slack_client.calls if c['method'] == 'chat_postMessage' and c.get('blocks')]  # noqa: E731
    assert not cards()
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE deep_runs SET state='cancelled' WHERE kind='check'")
    await slack_tasks.tick(ctx)
    assert len(cards()) == 1


async def test_logs_drop_the_address_of_a_link(app):
    import logging

    from illo.tasks.pages.web import LinkLogFilter

    def record(text):
        return logging.LogRecord('uvicorn.access', logging.INFO, '', 0, text, (), None)

    for query in ('key=abc', 'next=%2Ft%2F1&key=abc', '%6bey=abc'):
        assert not LinkLogFilter().filter(record(f'1.2.3.4 - "GET /web/signin?{query} HTTP/1.1" 200'))
    assert LinkLogFilter().filter(record('1.2.3.4 - "POST /web/signin HTTP/1.1" 303'))
    assert LinkLogFilter().filter(record('1.2.3.4 - "GET /t/1 HTTP/1.1" 200'))
    assert all(any(isinstance(f, LinkLogFilter) for f in logging.getLogger(name).filters)
               for name in ('uvicorn.access', 'httpx', 'httpcore'))


def test_long_notice_stays_whole_as_plain_text(ctx):
    row = {'id': 7, 'title': 'Review the work'}
    short = slack_tasks.notice(ctx, row, 'Read it.')
    assert [b['type'] for b in short['blocks']] == ['section', 'actions']
    assert short['blocks'][0]['text']['text'] == short['text'] == f'Read it.\n<{BASE}/t/7|Review the work>'
    assert short['blocks'][-1]['elements'][0]['action_id'] == 'web-open:7'
    # A section cuts its text at 3,000 characters. A long notice has no blocks, so Slack shows all of it.
    assert slack_tasks.notice(ctx, row, '&amp;' * 1200) == {'text': '&amp;' * 1200 + f'\n<{BASE}/t/7|Review the work>'}
    ctx.workspace.config.pages.enabled = False
    assert slack_tasks.notice(ctx, row, 'Read it.') == {'text': 'Read it.'}


async def test_slow_slack_ends_the_link_and_asks_for_a_new_press(ctx, slack_client, monkeypatch):
    row = await make(ctx, returned=True)

    async def expired(**kwargs):
        raise RuntimeError('expired_trigger_id')

    monkeypatch.setattr(slack_client, 'views_open', expired)
    async with ctx.pool.connection() as conn:
        person = await people.get(conn, 'ada')
    await ctx.slack_actions.dispatch(ctx, person, {'action_id': f"web-open:{row['id']}", 'trigger_id': 'T1',
                                                   'channel': 'D_ACME_ADA', 'thread_ts': '1.2'})
    said = slack_client.calls[-1]
    assert said['method'] == 'chat_postEphemeral' and said['user'] == 'U_ACME_ADA' and said['thread_ts'] == '1.2'
    assert 'Press Open the page again' in said['text'] and 'key=' not in said['text']
    async with ctx.pool.connection() as conn:
        assert not await one(conn, 'SELECT id FROM web_signins')


async def test_ended_signin_offers_a_new_code_for_the_same_task(browser, ctx):
    await browser.start(next='/t/7')
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE web_signins SET expires_at=now()-interval '1 second'")
    ended = await browser.http.get('/web/signin')
    assert 'The sign-in ended' in ended.text and 'action="/web/signin?next=%2Ft%2F7"' in ended.text
    assert 'connect ' in (await browser.start(next='/t/7')).text


def test_wait_check_names_the_page_tools_only_when_pages_are_on():
    from illo.tasks import waits

    row, person = {'id': 3, 'waits_for': 'person:ada'}, {'name': 'Ana'}
    period = {'since_event_id': 1, 'since': datetime.now(UTC), 'needed': 'you hold it'}
    off = waits.request(row, period, person, datetime.now(UTC), 0)['job']
    on = waits.request(row, period, person, datetime.now(UTC), 0, pages=True)['job']
    assert 'page_' not in off and '{page}' not in off
    assert 'connected.\n\nThen call `wait_result`' in off
    assert on == off.replace('Then call `wait_result`', waits.PAGE + 'Then call `wait_result`', 1)


async def test_destination_survives_a_lost_request(browser, ctx):
    posted = await browser.http.post('/web/signin', params={'next': '/t/7'})
    assert posted.status_code == 303 and posted.headers['location'] == '/web/signin?next=%2Ft%2F7'
    await signin.cleanup(ctx)
    async with ctx.pool.connection() as conn:
        await conn.execute('DELETE FROM web_signins')
    again = await browser.http.get(posted.headers['location'])
    assert 'Get a code' in again.text and 'action="/web/signin?next=%2Ft%2F7"' in again.text
    browser.http.cookies.clear()
    again = await browser.http.get(posted.headers['location'])
    assert 'action="/web/signin?next=%2Ft%2F7"' in again.text


async def test_signed_in_person_makes_no_new_request(browser, ctx):
    await browser.login()
    token = browser.http.cookies.get(signin.SESSION_COOKIE)
    async with ctx.pool.connection() as conn:
        before = await one(conn, "SELECT count(*) AS n FROM agent_oauth_limits WHERE kind LIKE 'web_start%%'")
    posted = await browser.http.post('/web/signin', params={'next': '/t/1'})
    assert posted.status_code == 303 and posted.headers['location'] == '/t/1'
    assert (await browser.http.post('/web/signin')).status_code == 200
    assert (await browser.http.get('/web/signin', params={'restart': '1'})).status_code == 200
    assert browser.http.cookies.get(signin.SESSION_COOKIE) == token
    async with ctx.pool.connection() as conn:
        after = await one(conn, "SELECT count(*) AS n FROM agent_oauth_limits WHERE kind LIKE 'web_start%%'")
        assert after == before and (await one(conn, 'SELECT count(*) AS n FROM web_signins'))['n'] == 1


async def test_restart_parameter_on_a_read_makes_nothing(browser, ctx):
    response = await browser.http.get('/web/signin', params={'restart': '1'})
    assert response.status_code == 200 and 'Get a code' in response.text
    async with ctx.pool.connection() as conn:
        assert not await one(conn, 'SELECT id FROM web_signins')


async def test_page_render_does_not_read_the_picture_data(ctx, monkeypatch):
    calls = []

    def fake(ctx, image, data=True):
        calls.append(data)
        return image, b'' if data else None, 'image/png' if data else None

    monkeypatch.setattr(images, '_read', fake)
    block = {'kind': 'image', 'image': {'repo': 'r', 'ref': 'a' * 40, 'path': 'x.png'}, 'caption': 'c', 'alt': 'a'}
    html = await render.block_html(ctx, '/t/1/file', 0, block, {'timezone': 'UTC'})
    assert calls == [False] and '/t/1/file/0' in html
    await images.read(ctx, block['image'])
    assert calls == [False, True]


async def test_bars_use_progress_elements(ctx):
    block = {'kind': 'bars', 'title': 'T', 'rows': [{'label': 'x' * 40, 'value': 2, 'unit': ''}, {'label': 'short', 'value': 0.5, 'unit': 'ms'}]}
    html = await render.block_html(ctx, '/t/1/file', 0, block, {'timezone': 'UTC'})
    assert '<progress max="2" value="2"></progress><span>2</span>' in html
    assert '<span>short</span><progress max="2" value="0.5"></progress><span>0.5 ms</span>' in html
    assert 'x' * 40 in html and '<svg' not in html and 'style' not in html


async def test_page_words_for_the_wait(browser, ctx):
    row = await make(ctx, returned=True)
    await browser.login()
    text = (await browser.http.get(f'/t/{row["id"]}')).text
    assert 'Waits for you · today' in text and '<li>In review</li>' in text
    assert '<p class="ask">Returned by ' in text
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE task_events SET at=at-interval '25 hours' WHERE task_id=%s", (row['id'],))
        await conn.execute("UPDATE tasks SET created_at=created_at-interval '25 hours' WHERE id=%s", (row['id'],))
    assert 'Waits for you · 1 day<' in (await browser.http.get(f'/t/{row["id"]}')).text


async def test_schema_refusal_names_the_fields_and_no_values(ctx):
    row = await make(ctx)
    secret = 'VALUE-THAT-MUST-NOT-APPEAR'
    blocks = [{'kind': 'preview', 'label': 'x', 'text': secret, 'extra': secret},
              {'kind': secret, 'text': 'x'},
              {'kind': 'checks', 'items': [{'claim': 'c', 'state': secret}]}]
    async with ctx.pool.connection() as conn:
        person = await people.get(conn, 'ada')
        run = await runs.create(conn, principal='person:ada', person_id=person['id'], profile='person_dm', provider="fake", tool_limit=12, model='fake')
        result = await ctx.registry.call(ToolContext(conn, run, ctx), 'page_write', {'task': row['id'], 'blocks': blocks})
        stored = await one(conn, "SELECT reason FROM audit WHERE run_id=%s AND tool='page_write'", (run['id'],))
    reason = result['reason']
    assert result['decision'] == 'deny' and reason.startswith('The tool arguments do not match its schema: ')
    assert 'blocks.0.preview.name: Field required' in reason and 'blocks.0.preview.?: Extra inputs are not permitted' in reason
    assert 'blocks.1: Use one of the kinds in the schema' in reason and 'blocks.2.checks.items.0.state' in reason
    assert secret not in reason and stored['reason'] == reason


def test_small_block_fields_are_optional():
    page = Write.model_validate({'task': 1, 'blocks': [
        {'kind': 'preview', 'name': 'Example Co', 'text': 'Hello', 'title': 'Post'},
        {'kind': 'image', 'image': {'repo': 'r', 'ref': 'main', 'path': 'a.png'}},
        {'kind': 'bars', 'rows': [{'label': 'a', 'value': 1}]}]}).model_dump()
    assert page['blocks'][0]['label'] == '' and page['blocks'][1]['caption'] == '' and page['blocks'][2]['title'] == ''


async def test_optional_titles_render(ctx, monkeypatch):
    async def ok(ctx, image, data=True):
        return image, None, None
    monkeypatch.setattr(images, 'read', ok)
    ref = {'repo': 'r', 'ref': 'a' * 40, 'path': 'x.png'}
    html = await render.block_html(ctx, '/t/1/file', 0, {'kind': 'image', 'title': 'Picture', 'image': ref, 'caption': '', 'alt': ''}, {'timezone': 'UTC'})
    assert '<h2>Picture</h2>' in html and 'figcaption' not in html
    html = await render.block_html(ctx, '/t/1/file', 0, {'kind': 'preview', 'title': 'Post', 'name': 'Ex', 'text': 't'}, {'timezone': 'UTC'})
    assert '<h2>Post</h2>' in html and '<small></small>' in html


@pytest.mark.parametrize('blocks,expected', [
    ([{'kind': 'checks', 'items': [{'claim': 'c', 'state': 'yes', 'checked_at': 'SECRET_TOKEN_123'}]}],
     'blocks.0.checks.items.0.checked_at: The value is not valid'),
    ([{'kind': 'text', 'text': 'ok', 'SECRET_TOKEN_123': 'hidden'}], 'blocks.0.text.?: Extra inputs are not permitted'),
    ([{'kind': 'image', 'image': {'repo': 'r', 'ref': 'main', 'path': '../SECRET_TOKEN_123'}}],
     'blocks.0.image.image.?.path: The value is not valid'),
])
def test_schema_reason_never_repeats_caller_text(blocks, expected):
    from illo.tools.registry import schema_reason

    with pytest.raises(ValidationError) as caught:
        Write.model_validate({'task': 1, 'blocks': blocks})
    reason = schema_reason(caught.value, Write)
    assert expected in reason and 'SECRET_TOKEN_123' not in reason


def test_schema_reason_has_a_length_limit():
    from illo.tools.registry import MAX_REASON_CHARS, schema_reason

    with pytest.raises(ValidationError) as caught:
        Write.model_validate({'task': 1, 'blocks': [], **{'A' * 13000 + str(n): 0 for n in range(9)}})
    reason = schema_reason(caught.value, Write)
    assert len(reason) <= MAX_REASON_CHARS and 'AAAA' not in reason and '(and 4 more)' in reason

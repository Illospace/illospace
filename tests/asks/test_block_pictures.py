"""Request blocks and picture files keep the same access boundary."""

import asyncio
import base64
import struct
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from illo.connectors import addresses
from illo.db import all_rows, one
from illo.ledger import asks, people
from illo.tasks.pages import outside
from illo.tasks.pages.schema import ImageUrl
from tests.asks import test_request_pages as requests_tests
from tests.asks.test_request_pages import ASK, login, posts, read
from tests.mcp.client import AgentClient
from tests.tasks.test_pages import ANA, make


async def write(ctx, row, blocks):
    """Write a task page as the agent door does: get the pictures first, then write."""
    from illo.tasks import pages
    from illo.tasks.pages.schema import Write

    args = Write.model_validate({'task': row['id'], 'blocks': blocks}).model_dump()
    files = await outside.prepare(ctx, args['blocks'])
    async with ctx.pool.connection() as conn:
        return await pages._write(conn, ctx, ANA, args, files)

ctx = requests_tests.ctx
browser = requests_tests.browser

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5HsAAAAASUVORK5CYII=')
BLOCK = {'kind': 'image', 'image': {'url': 'https://example.com/plan.png'}, 'caption': 'The proposed picture.'}


class Body(httpx.AsyncByteStream):
    def __init__(self, data, *, delay=0):
        self.data, self.delay = data, delay

    async def __aiter__(self):
        for position in range(0, len(self.data), 65536):
            if self.delay:
                await asyncio.sleep(self.delay)
            yield self.data[position:position + 65536]


@pytest.fixture
async def picture_transport(monkeypatch):
    requests = []
    settings = {'data': PNG, 'status': 200, 'headers': {'content-type': 'image/png'}}

    async def resolve(host, port, **kwargs):
        values = ['93.184.216.34', '10.0.0.1'] if host == 'mixed.example' else ['93.184.216.34']
        return [(2, 1, 6, '', (value, port)) for value in values]

    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)

    async def handle(request):
        requests.append(request)
        assert request.method == 'GET' and request.url.scheme == 'https' and request.url.port in {None, 443}
        assert request.headers['user-agent'] == 'Illo'
        assert 'authorization' not in request.headers and 'cookie' not in request.headers
        assert request.url.host == '93.184.216.34'
        assert request.extensions['sni_hostname'] == request.headers['host']
        if check := settings.get('check'):
            await check()
        if settings.get('delay'):
            await asyncio.sleep(settings['delay'])
        status = 404 if request.url.path == settings.get('fail_path') else settings['status']
        return httpx.Response(status, headers=settings['headers'],
                              stream=Body(settings['data'], delay=settings.get('body_delay', 0)))

    monkeypatch.setattr(outside, 'PublicTransport', lambda inner: addresses.PublicTransport(httpx.MockTransport(handle)))
    return settings, requests


async def counts(ctx):
    async with ctx.pool.connection() as conn:
        return [(await one(conn, f'SELECT count(*) AS n FROM {table}'))['n'] for table in ('asks', 'page_files')]


async def test_request_blocks_precede_proof(ctx, app, http, browser, slack_client):
    agent = await AgentClient.create(app, http)
    blocks = [{'kind': 'text', 'title': 'Read this', 'text': 'The complete plan.'},
              {'kind': 'preview', 'title': 'Reply', 'name': 'Ada', 'text': 'We can send the pictures tomorrow.'},
              {'kind': 'checks', 'title': 'Facts', 'items': [
                  {'claim': 'The file exists.', 'state': 'yes', 'source_url': 'https://example.com/plan'},
                  {'claim': 'The check is open.', 'state': 'open'}]},
              {'kind': 'bars', 'title': 'Counts', 'rows': [{'label': 'Pictures', 'value': 4}]}]
    result = await agent.call('ask_create', {**ASK, 'blocks': blocks, 'proof': {'text': 'The other material.'}})
    row = await read(ctx, result['ask_id'])
    assert row['blocks'][2]['items'][0]['checked_at'] and row['blocks'][2]['items'][1]['checked_at'] is None
    await login(ctx, browser.http)
    text = (await browser.http.get(f"/web/a/{row['id']}")).text
    places = [text.index(part) for part in ('<h2>Read this</h2>', '<h2>Reply</h2>', '<h2>Facts</h2>',
                                          '<h2>Counts</h2>', 'The other material.')]
    assert places == sorted(places)
    assert '<progress' in text and 'checked ' in text and 'https://example.com/plan' in text
    assert '<script' not in text and 'style=' not in text
    from illo.asks import service

    await service.tick(ctx)
    actions = [block for block in posts(slack_client)[0]['blocks'] if block['type'] == 'actions'][0]
    assert actions['elements'][0]['text']['text'] == 'Open the page'
    assert 'The complete plan.' not in str(posts(slack_client))


async def test_blocks_need_pages(ctx, app, http):
    agent = await AgentClient.create(app, http)
    ctx.workspace.config.pages.enabled = False
    result = await agent.call('ask_create', {**ASK, 'blocks': [{'kind': 'text', 'text': 'Read this.'}]}, error=True)
    assert result['error']['message'] == 'This team has no pages. Put the text in proof.text and the addresses in proof.links.'
    assert await counts(ctx) == [0, 0]


async def test_address_off_makes_no_request_and_keeps_write_choice(ctx, app, http, browser, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('The transport must not be called.')

    monkeypatch.setattr(outside, 'PublicTransport', forbidden)
    agent = await AgentClient.create(app, http)
    result = await agent.call('ask_create', {**ASK, 'blocks': [BLOCK], 'source_ref': 'picture-off'})
    assert result['notes'] == ['Pictures from an address are off for this team. Block 1 shows as a link.']
    ctx.workspace.config.pages.outside_images = True
    again = await agent.call('ask_create', {**ASK, 'blocks': [BLOCK], 'source_ref': 'picture-off'})
    assert again == result
    await login(ctx, browser.http)
    response = await browser.http.get(f"/web/a/{result['ask_id']}")
    assert '>Picture: plan.png</a>' in response.text and BLOCK['caption'] in response.text and '<img' not in response.text
    assert (await browser.http.get(f"/web/a/{result['ask_id']}/file/0")).status_code == 404
    task = await make(ctx)
    ctx.workspace.config.pages.outside_images = False
    written = await write(ctx, task, [BLOCK])
    assert written['notes'] == result['notes']
    assert 'image_link' not in written['blocks'][0]
    ctx.workspace.config.pages.outside_images = True
    response = await browser.http.get(f"/t/{task['id']}")
    assert '>Picture: plan.png</a>' in response.text and '<img' not in response.text


async def test_picture_file_access_and_isolation(ctx, app, http, browser, picture_transport):
    ctx.workspace.config.pages.outside_images = True
    agent = await AgentClient.create(app, http)
    first = await agent.call('ask_create', {**ASK, 'blocks': [BLOCK], 'source_ref': 'first'})
    second = await agent.call('ask_create', {**ASK, 'source_ref': 'second'})
    path = f"/web/a/{first['ask_id']}"
    assert len(picture_transport[1]) == 1
    row = await read(ctx, first['ask_id'])
    assert row['blocks'][0]['image'] == BLOCK['image']
    assert 'notes' not in first
    for person in ('ben', 'ada', 'chloe', None):
        browser.http.cookies.clear()
        if person:
            await login(ctx, browser.http, person)
        response = await browser.http.get(path + '/file/0')
        if person in {'ben', 'ada'}:
            assert response.status_code == 200 and response.content == PNG
            for key, value in {'content-type': 'image/png', 'x-content-type-options': 'nosniff',
                               'content-security-policy': "default-src 'none'", 'cache-control': 'private, no-store',
                               'content-disposition': 'inline'}.items():
                assert response.headers[key] == value
            page = await browser.http.get(path)
            assert f'<img src="{path}/file/0" alt="">' in page.text
            for other in (f"/web/a/{second['ask_id']}/file/0", f"/t/{first['ask_id']}/file/0", path + '/file/-1'):
                assert (await browser.http.get(other)).status_code == 404
        else:
            assert response.status_code == 404
    ctx.workspace.config.pages.outside_images = False
    await login(ctx, browser.http)
    assert '<img' in (await browser.http.get(path)).text
    async with ctx.pool.connection() as conn:
        await conn.execute('DELETE FROM page_files')
    page = await browser.http.get(path)
    assert 'The picture is not available.' in page.text and '<img' not in page.text


@pytest.mark.parametrize('case,reason', [
    ('private', outside.ADDRESS), ('localhost', outside.ADDRESS), ('mapped', outside.ADDRESS),
    ('mixed', outside.ADDRESS), ('port', outside.ADDRESS),
    ('redirect', 'The address redirects. Give the final address.'), ('404', 'The server answered 404.'),
    ('slow', 'The server did not answer in 10 seconds.'), ('large', 'The file is larger than 5 MB.'),
    ('html', outside.TYPE), ('svg', outside.TYPE), ('header', outside.TYPE),
    ('wide', 'The picture is larger than 8,000 pixels on one side.'), ('full', outside.FULL),
])
async def test_refusals_write_nothing(ctx, app, http, picture_transport, monkeypatch, case, reason):
    ctx.workspace.config.pages.outside_images = True
    settings, requests = picture_transport
    block = {**BLOCK, 'image': dict(BLOCK['image'])}
    hosts = {'private': 'https://10.0.0.1/a.png', 'localhost': 'https://localhost/a.png',
             'mapped': 'https://[::ffff:93.184.216.34]/a.png', 'mixed': 'https://mixed.example/a.png',
             'port': 'https://example.com:444/a.png'}
    if case in hosts:
        block['image']['url'] = hosts[case]
    elif case == 'redirect':
        settings.update(status=302, headers={'location': 'https://example.com/final.png'})
    elif case == '404':
        settings['status'] = 404
    elif case == 'slow':
        assert outside.TIMEOUT == 10
        monkeypatch.setattr(outside, 'TIMEOUT', 0.03)
        settings.update(data=PNG + b'\x00' * 65536, delay=0.015, body_delay=0.015)
    elif case == 'large':
        settings.update(data=PNG + b'x' * (outside.MAX_BYTES + 1 - len(PNG)), headers={'content-length': '1'})
    elif case in {'html', 'svg', 'header'}:
        settings['data'] = {'html': b'<html>Not a picture</html>', 'svg': b'<svg></svg>', 'header': PNG[:20]}[case]
    elif case == 'wide':
        settings['data'] = PNG[:16] + struct.pack('>II', 9000, 1) + PNG[24:]
    elif case == 'full':
        monkeypatch.setattr(outside, 'STORE_BYTES', 0)
    agent = await AgentClient.create(app, http)
    result = await agent.call('ask_create', {**ASK, 'blocks': [{'kind': 'text', 'text': 'Read this.'}, block]}, error=True)
    assert result['error']['message'] == str(outside.refusal(1, reason))
    assert await counts(ctx) == [0, 0]
    if case in hosts:
        assert not requests


async def test_task_picture_write_replace_and_lock_order(ctx, app, http, browser, picture_transport):
    ctx.workspace.config.pages.outside_images = True
    task = await make(ctx)
    settings, requests = picture_transport

    async def unlocked():
        async with ctx.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL lock_timeout='100ms'")
                await conn.execute('SELECT pg_advisory_xact_lock(12000001)')
                await conn.execute('SELECT id FROM tasks WHERE id=%s FOR UPDATE NOWAIT', (task['id'],))

    settings['check'] = unlocked
    result = await write(ctx, task, [BLOCK])
    assert result['blocks'][0]['image'] == BLOCK['image']
    await login(ctx, browser.http, 'ada')
    path = f"/t/{task['id']}"
    assert (await browser.http.get(path + '/file/0')).content == PNG
    assert f'<img src="{path}/file/0"' in (await browser.http.get(path)).text
    await write(ctx, task, [])
    assert (await browser.http.get(path + '/file/0')).status_code == 404
    assert await counts(ctx) == [0, 0] and len(requests) == 1
    build = await AgentClient.create(app, http)
    await build.call('task_page', {'task': task['id'], 'blocks': [BLOCK]})
    assert (await browser.http.get(path + '/file/0')).content == PNG


@pytest.mark.parametrize('url', ['http://example.com/a.png', 'https://u:p@example.com/a.png',
                                 'https://@example.com/a.png', 'https://example.com/a\n.png',
                                 'https://example.com/\x7fa.png', 'https://example.com/' + 'x' * 2000])
def test_image_url_schema(url):
    with pytest.raises(ValidationError):
        ImageUrl(url=url)


async def test_migration_is_repeatable(ctx):
    migration = Path('illo/migrations/1804_ask_blocks.sql').read_text()
    async with ctx.pool.connection() as conn:
        await conn.execute(migration)
        await conn.execute(migration)
        row = await one(conn, "SELECT column_default,is_nullable FROM information_schema.columns WHERE table_name='asks' AND column_name='blocks'")
        assert row['column_default'] == "'[]'::jsonb" and row['is_nullable'] == 'NO'


@pytest.mark.parametrize('kind,data,expected', [
    ('image/png', PNG, (1, 1)),
    ('image/gif', b'GIF89a' + struct.pack('<HH', 20, 30) + b'\x00' * 3, (20, 30)),
    ('image/jpeg', b'\xff\xd8\xff\xe0\x00\x04xx\xff\xc0\x00\x0b\x08\x00\x1e\x00\x14\x01\x01\x11\x00', (20, 30)),
    ('image/webp', b'RIFF' + struct.pack('<I', 22) + b'WEBPVP8 ' + struct.pack('<I', 10)
     + b'\x00\x00\x00\x9d\x01\x2a' + struct.pack('<HH', 20, 30), (20, 30)),
    ('image/webp', b'RIFF' + struct.pack('<I', 18) + b'WEBPVP8L' + struct.pack('<I', 5)
     + b'\x2f' + struct.pack('<I', 19 | (29 << 14)) + b'\x00', (20, 30)),
    ('image/webp', b'RIFF' + struct.pack('<I', 22) + b'WEBPVP8X' + struct.pack('<I', 10)
     + b'\x00' * 4 + b'\x13\x00\x00\x1d\x00\x00', (20, 30)),
])
def test_picture_header_dimensions(kind, data, expected):
    assert outside.dimensions(data, kind) == expected
    with pytest.raises(ValueError, match=outside.TYPE):
        outside.dimensions(data[:12], kind)


async def test_cleanup_uses_ask_creation_and_task_change(ctx):
    async with ctx.pool.connection() as conn:
        person = await people.get(conn, 'ada')
        for n, (state, days) in enumerate([('open', 91), ('cancelled', 91), ('cancelled', 89)]):
            ask = await asks.create(conn, owner_person_id=person['id'], question='Read this?', source_ref=f'cleanup-{n}')
            await conn.execute("UPDATE asks SET state=%s,created_at=now()-%s*interval '1 day' WHERE id=%s", (state, days, ask['id']))
            await outside.replace(conn, [(0, 'image/png', PNG)], ask_id=ask['id'])
    for state, days in [('working', 91), ('done', 91), ('cancelled', 91), ('done', 89)]:
        task = await make(ctx)
        async with ctx.pool.connection() as conn:
            await conn.execute("UPDATE tasks SET state=%s,updated_at=now()-%s*interval '1 day' WHERE id=%s", (state, days, task['id']))
            await outside.replace(conn, [(0, 'image/png', PNG)], task_id=task['id'])
    await outside.cleanup(ctx)
    async with ctx.pool.connection() as conn:
        files = await all_rows(conn, 'SELECT * FROM page_files ORDER BY id')
        assert len(files) == 4
        assert [file['ask_id'] for file in files[:2]] == [1, 3]
        assert [file['task_id'] for file in files[2:]] == [1, 4]


async def test_preview_pictures_keep_order_and_ignore_content_type(ctx, app, http, browser, picture_transport):
    ctx.workspace.config.pages.outside_images = True
    settings, requests = picture_transport
    settings['headers'] = {'content-type': 'text/html', 'content-length': '999999999'}
    agent = await AgentClient.create(app, http)
    blocks = [BLOCK, {'kind': 'preview', 'name': 'Ada', 'text': 'The reply.',
                      'image': {'url': 'https://example.com/reply.png'}}]
    result = await agent.call('ask_create', {**ASK, 'blocks': blocks})
    assert [request.url.path for request in requests] == ['/plan.png', '/reply.png']
    await login(ctx, browser.http)
    path = f"/web/a/{result['ask_id']}"
    text = (await browser.http.get(path)).text
    assert f'<img src="{path}/file/0"' in text and f'<img src="{path}/file/1"' in text
    assert (await browser.http.get(path + '/file/1')).headers['content-type'] == 'image/png'
    settings['fail_path'] = '/reply.png'
    failure = await agent.call('ask_create', {**ASK, 'blocks': blocks}, error=True)
    assert 'block 2: The server answered 404.' in failure['error']['message']
    assert await counts(ctx) == [1, 2]


async def test_two_writes_cannot_exceed_store_limit(ctx, app, http, picture_transport, monkeypatch):
    ctx.workspace.config.pages.outside_images = True
    monkeypatch.setattr(outside, 'STORE_BYTES', len(PNG))
    agents = [await AgentClient.create(app, http, label=f'Example agent {n}') for n in range(2)]

    async def create(agent):
        response = await agent.tool_response('ask_create', {**ASK, 'blocks': [BLOCK]})
        return response['result']

    results = await asyncio.gather(*(create(agent) for agent in agents))
    assert sorted(result['isError'] for result in results) == [False, True]
    refused = next(result for result in results if result['isError'])
    assert refused['structuredContent']['error']['message'] == str(outside.refusal(0, outside.FULL))
    assert await counts(ctx) == [1, 1]


async def test_process_fetch_limit(picture_transport):
    settings, requests = picture_transport
    active = maximum = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            entered.set()
        await release.wait()
        active -= 1

    settings['check'] = hold
    pending = [asyncio.create_task(outside.fetch(f'https://example.com/{n}.png')) for n in range(3)]
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert len(requests) == 2 and maximum == 2
    finally:
        release.set()
        await asyncio.gather(*pending)
    assert len(requests) == 3 and maximum == 2


async def test_own_model_page_makes_no_request(ctx, app, http, rpc, rpc_token, picture_transport, browser):
    ctx.workspace.config.pages.outside_images = True
    task = await make(ctx)
    run, token = rpc_token
    settings, requests = picture_transport
    response = await rpc(token, 'tools/call', {'name': 'page_write', 'arguments': {'task': task['id'], 'blocks': [BLOCK]}})
    assert response.status_code == 200 and not response.json()['result']['isError'], response.text
    assert 'Illo does not get a picture from an address for its own pages. Block 1 shows as a link.' in response.text
    assert requests == []
    async with ctx.pool.connection() as conn:
        assert not await one(conn, 'SELECT 1 FROM page_files WHERE task_id=%s', (task['id'],))


async def test_failed_task_write_keeps_old_blocks_and_files(ctx, picture_transport, monkeypatch):
    ctx.workspace.config.pages.outside_images = True
    task = await make(ctx)
    first = await write(ctx, task, [BLOCK])
    monkeypatch.setattr(outside, 'STORE_BYTES', 0)
    with pytest.raises(ValueError, match='The picture store is full'):
        await write(ctx, task, [BLOCK, {'kind': 'text', 'text': 'A new note.'}])
    async with ctx.pool.connection() as conn:
        from illo.tasks import pages

        current = await pages.read(conn, ctx, ANA, task['id'])
        assert current == first
        stored = await outside.read(conn, 0, task_id=task['id'])
        assert bytes(stored['data']) == PNG


async def test_request_repository_picture_uses_stored_copy(ctx, app, http, browser, monkeypatch):
    from illo.tasks.pages import images

    ref = {'repo': 'acme-example/widget', 'ref': 'main', 'path': 'plan.png'}

    async def read_picture(ctx, image, *, data=True):
        assert image == ref and data
        return {**ref, 'ref': 'a' * 40}, PNG, 'image/png'

    monkeypatch.setattr(images, 'read', read_picture)
    agent = await AgentClient.create(app, http)
    made = await agent.call('ask_create', {**ASK, 'blocks': [{**BLOCK, 'image': ref}]})

    async def forbidden(*args, **kwargs):
        pytest.fail('The ask reads its stored copy.')

    monkeypatch.setattr(images, 'read', forbidden)
    await login(ctx, browser.http)
    path = f"/web/a/{made['ask_id']}"
    assert f'<img src="{path}/file/0"' in (await browser.http.get(path)).text
    assert (await browser.http.get(path + '/file/0')).content == PNG
    async with ctx.pool.connection() as conn:
        await conn.execute('DELETE FROM page_files')
    assert 'The picture is not available.' in (await browser.http.get(path)).text

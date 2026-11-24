"""Private HTML routes. No model text becomes executable markup."""

import hmac
import logging
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from illo.channels.slack import tasks as slack_tasks
from illo.db import one
from illo.ledger import audit, tasks
from illo.ledger.tokens import digest
from illo.tasks import pages
from illo.tasks.pages import ask_page, board, board_thread, images, outside, render, signin


def html(body, status=200, headers=render.HEADERS):
    return HTMLResponse(body, status_code=status, headers=headers)


# The address of a sign-in link holds its key. A page at that address must not send the address
# to the stylesheet request or to the form request, so these pages send only the origin.
LINK_HEADERS = {**render.HEADERS, 'Referrer-Policy': 'strict-origin'}


def missing(message="The task was not found."):
    return html(render.document("<main><h1>" + escape(message) + "</h1></main>"), 404)


def redirect(path, status=303):
    return RedirectResponse(path, status_code=status, headers=render.HEADERS)


def cookie(ctx, response, name, value, seconds):
    response.set_cookie(name, value, max_age=seconds, httponly=True, samesite='lax',
                        path='/', secure=not ctx.config.web_allow_http)


def origin_ok(ctx, request):
    # A browser sets Sec-Fetch-Site by itself, and a page cannot change it. It is correct also when the
    # browser hides the origin ("Origin: null"), which a strict referrer policy or a privacy setting causes.
    site = request.headers.get('sec-fetch-site')
    if site and site != 'same-origin':
        return False
    origin = request.headers.get('origin')
    if origin is None or (origin == 'null' and site == 'same-origin'):
        return True
    base = urlsplit(pages.base_url(ctx))
    return origin == f'{base.scheme}://{base.netloc}'


async def form_data(request, *, limit=32_000):
    # Do not need a multipart parser or a new dependency for a small form.
    if request.headers.get('content-type', '').split(';')[0] != 'application/x-www-form-urlencoded':
        return {}
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            return {}
        body.extend(chunk)
    try:
        fields = parse_qs(body.decode('utf-8'), keep_blank_values=True, max_num_fields=10)
        return {k: v[0] for k, v in fields.items() if len(v) == 1}
    except (UnicodeError, ValueError):
        return {}


def signin_path(destination):
    return '/web/signin' + ('?' + urlencode({'next': destination}) if destination else '')


def start_form(destination, label, *, keep_fragment=False):
    # The browser can keep a board frame fragment through a POST to the current URL.
    action = '' if keep_fragment else f' action="{escape(signin_path(destination))}"'
    return f'<form method="post"{action}><button class="btn go">{escape(label)}</button></form>'


async def link_sign_in(ctx, conn, request, person, key):
    """Exchange the one-time key of a link from Slack. The click in Slack was the approval."""
    row = await signin.request(conn, key)
    usable = bool(row and not row['expired'] and row['decision'] == 'approved' and not row['continued_at'])
    if person:
        # A link never changes the person of a browser that is signed in. The key of that person ends here.
        if usable and row['person_id'] == person['id']:
            await conn.execute('UPDATE web_signins SET continued_at=clock_timestamp() WHERE id=%s', (row['id'],))
        return redirect((row['next_path'] if row else '') or '/web/signin')
    owner = usable and await one(conn, 'SELECT name FROM people WHERE id=%s AND active', (row['person_id'],))
    if not owner:
        return html(render.document(
            '<main class="signin"><h1>The link ended.</h1><p>A link works one time, for 10 minutes. '
            'In Slack, press <b>Open the page</b> again.</p></main>'), 410, LINK_HEADERS)
    if request.method != 'POST':
        # Only a POST uses the key. A link preview reads this page and changes nothing.
        name = escape(owner['name'])
        return html(render.document(
            f'<main class="signin"><h1>Sign in as {name}</h1>'
            f'<p>Illo made this link for {name} in Slack. Continue only if you are {name}.</p>'
            f'<form method="post" action="/web/signin"><input type="hidden" name="key" value="{escape(key)}">'
            '<button class="btn go">Continue</button></form></main>', title='Sign in to Illo'), headers=LINK_HEADERS)
    if not origin_ok(ctx, request):
        return html(render.document('<main><p>The form is invalid.</p></main>'), 403)
    approved = await signin.continue_session(conn, key)
    if not approved:
        return html(render.document('<main class="signin"><h1>The link ended.</h1></main>'), 410)
    token, destination = approved
    response = redirect(destination or '/web/signin')
    cookie(ctx, response, signin.SESSION_COOKIE, token, 30 * 86400)
    return response


async def sign_in(ctx, request):
    async with ctx.pool.connection() as conn:
        person = await signin.session(conn, request.cookies.get(signin.SESSION_COOKIE))
        destination = signin.next_path(request.query_params.get('next', ''))
        key = request.query_params.get('key', '') if request.method == 'GET' else (await form_data(request)).get('key', '')
        if key:
            return await link_sign_in(ctx, conn, request, person, key)
        if person:
            return redirect(destination) if destination else html(render.document('<main class="signin"><h1>You are signed in</h1></main>'))
        if request.method == 'POST':
            # Only a POST makes a request. A link preview or a crawler reads the page and makes nothing.
            if not origin_ok(ctx, request):
                return html(render.document('<main><p>The form is invalid.</p></main>'), 403)
            host = request.client.host if request.client else 'unknown'
            handle = await signin.start(conn, host, destination)
            if not handle:
                return html(render.document('<main class="signin"><h1>Try again later.</h1></main>'), 429)
            # Keep the destination in the address. The request cookie or its row can end first.
            response = redirect(signin_path(destination))
            cookie(ctx, response, signin.REQUEST_COOKIE, handle, 600)
            return response
        handle = request.cookies.get(signin.REQUEST_COOKIE, '')
        pending = await signin.request(conn, handle)
        if not pending:
            return html(render.document(
                '<main class="signin"><h1>Sign in to Illo</h1>'
                '<p>The fast way: in Slack, press <b>Open the page</b> on a task card from Illo.</p>'
                '<p>The other way: Illo gives you a code here. You send the code to Illo in Slack.</p>'
                + start_form(destination, 'Get a code', keep_fragment=destination == '/web/board')
                + '</main>', title='Sign in to Illo'))
        approved = await signin.continue_session(conn, handle)
        if approved:
            token, destination = approved
            response = redirect(destination or '/web/signin')
            cookie(ctx, response, signin.SESSION_COOKIE, token, 30 * 86400)
            response.delete_cookie(signin.REQUEST_COOKIE, path='/', httponly=True, samesite='lax', secure=not ctx.config.web_allow_http)
            return response
        if pending['expired'] or pending['decision'] != 'pending' or pending['continued_at']:
            return html(render.document(
                '<main class="signin"><h1>The sign-in ended.</h1><p>The code is denied, used, or expired.</p>'
                + start_form(pending['next_path'], 'Get a new code',
                             keep_fragment=destination == pending['next_path'] == '/web/board') + '</main>'))
    body = ('<main class="signin"><h1>Sign in to Illo</h1>'
            '<p>Send this line to Illo in a Slack direct message. Then press Approve on the card.</p>'
            '<p class="code">connect ' + escape(signin.pairing_line(handle)) + '</p>'
            '<p class="by">The code works for 10 minutes. This page continues when you approve.</p></main>')
    response = html(render.document(body, title='Sign in to Illo', refresh=True))
    cookie(ctx, response, signin.REQUEST_COOKIE, handle, 600)
    return response


async def task_page(ctx, request, task_id, *, file_position=None, progress=False):
    token = request.cookies.get(signin.SESSION_COOKIE, '')
    async with ctx.pool.connection() as conn:
        person = await signin.session(conn, token)
        if not person:
            return missing() if file_position is not None or progress else redirect('/web/signin?' + urlencode({'next': f'/t/{task_id}'}), 302)
        viewer = tasks.Viewer(person['key'], 'person:' + person['key'], human=True)
        row = await tasks.get(conn, task_id, viewer=viewer)
        if not row:
            return missing()
        if progress:
            return html(await render.progress_document(conn, row, person), headers=render.PROGRESS_HEADERS)
        if file_position is None:
            return html(await render.page(ctx, conn, row, person, token))
        page = await one(conn, 'SELECT * FROM task_pages WHERE task_id=%s', (task_id,))
        if not page or file_position < 0 or file_position >= len(page['blocks']):
            return missing()
        image = page['blocks'][file_position].get('image')
        if not image:
            return missing()
        if 'url' in image:
            file = await outside.read(conn, file_position, task_id=task_id)
            return file_response(file['data'], file['media_type']) if file else missing()
        try:
            _, data, content_type = await images.read(ctx, image)
        except ValueError:
            return missing()
        return file_response(data, content_type)


def file_response(data, content_type):
    return Response(data, media_type=content_type, headers={
        **render.HEADERS, 'Content-Security-Policy': "default-src 'none'",
        'Cache-Control': 'private, no-store', 'Content-Disposition': 'inline'})


async def decide(ctx, request, task_id):
    values = await form_data(request)
    token = request.cookies.get(signin.SESSION_COOKIE, '')
    async with ctx.pool.connection() as conn:
        person = await signin.session(conn, token)
        if not person:
            return redirect('/web/signin?' + urlencode({'next': f'/t/{task_id}'}), 302)
        if not origin_ok(ctx, request) or not signin.valid_csrf(token, values.get('csrf', '')):
            return html(render.document('<main><p>The form is invalid. Read the task page again.</p></main>'), 403)
        await conn.execute('SELECT pg_advisory_xact_lock(12000001)')
        viewer = tasks.Viewer(person['key'], 'person:' + person['key'], human=True)
        row = await tasks.get(conn, task_id, viewer=viewer, lock=True)
        if not row:
            return missing()
        if values.get('action') in {'done', 'reopen', 'force'} and not await slack_tasks._can_review(conn, ctx.workspace, row, person):
            return html(render.document('<main><p>This action is not available.</p></main>'), 403)
        stored = await one(conn, 'SELECT * FROM task_pages WHERE task_id=%s', (task_id,))
        if not hmac.compare_digest(render.seen(row, stored).encode(), values.get('seen', '').encode()):
            return html(await render.page(ctx, conn, row, person, token,
                message='The work changed after you opened the page. Read it again.'))
        action, reason = values.get('action'), values.get('reason', '')
        review = row['state'] == 'review' and await slack_tasks._can_review(conn, ctx.workspace, row, person)
        allowed = ((action in {'done', 'reopen', 'force'} and review)
                   or (action == 'message' and row['state'] in {'todo', 'working'}))
        if not allowed:
            return html(render.document('<main><p>This action is not available.</p></main>'), 403)
        if len(reason) > 4000 or (action in {'reopen', 'force', 'message'} and not reason.strip()):
            return html(await render.page(ctx, conn, row, person, token, message='Give a reason of 1 to 4,000 characters.'))
        try:
            common = {'workspace': ctx.workspace, 'viewer': viewer}
            if action in {'done', 'force'}:
                await tasks.done(conn, task_id, **common, force=action == 'force', reason=reason, ctx=ctx)
            elif action == 'reopen':
                await tasks.reopen(conn, task_id, **common, reason=reason)
            else:
                await tasks.post(conn, task_id, **common, text=reason)
        except tasks.TaskError as exc:
            await audit.write(conn, principal=viewer.actor, tool='task_' + str(action),
                args={'id': task_id}, decision='deny', reason=str(exc), target=f'task {task_id}')
            failed = await tasks.proof(conn, ctx.workspace, row, ctx) if action in {'done', 'force'} else None
            return html(await render.page(ctx, conn, row, person, token, message=str(exc), failed=failed))
        await audit.write(conn, principal=viewer.actor,
            tool='task_' + ('done' if action == 'force' else 'post' if action == 'message' else action),
            args={'id': task_id}, decision='allow', reason='The person acts on the task page.', target=f'task {task_id}')
    # The same sender reads committed events from either surface. The durable wake runs on its existing timer.
    await slack_tasks.tick(ctx)
    return redirect(f'/web/board#t{task_id}' if values.get('from') == 'board' else f'/t/{task_id}')


class LinkLogFilter(logging.Filter):
    def filter(self, record):
        # A log line shows the query string, and the query of a sign-in link holds its one-time key.
        # Drop each sign-in line that has a query. The order and the encoding of its fields do not matter then.
        return '/web/signin?' not in record.getMessage()


async def setup(ctx):
    for name in ('uvicorn.access', 'httpx', 'httpcore'):
        logging.getLogger(name).addFilter(LinkLogFilter())
    ctx.slack_actions.register('web-signin:', signin.action)
    ctx.slack_actions.register('web-open:', signin.open_action)
    ctx.slack_actions.register('web-open-ask:', signin.open_action)
    ctx.every(300, lambda: signin.cleanup(ctx))
    ctx.every(86400, lambda: outside.cleanup(ctx))

    async def route(request: Request):
        path = request.url.path
        if not pages.enabled(ctx):
            if path.startswith('/web/board') or path == '/web/thread.js':
                return missing(board_thread.MISSING)
            if path.startswith('/web/a/'):
                return missing('The request was not found.')
            # The address of a sign-in link holds its key, also when pages are off.
            return html(render.document('<main><h1>The task was not found.</h1></main>'), 404,
                        LINK_HEADERS if path == '/web/signin' else render.HEADERS)
        if path == '/web/board':
            token = request.cookies.get(signin.SESSION_COOKIE, '')
            async with ctx.pool.connection() as conn:
                await conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
                person = await signin.session(conn, token, touch=False)
                if not person:
                    return redirect(signin_path('/web/board'), 302)
                return html(await board.page(ctx, conn, person, token, markers=request.query_params),
                            headers=board.HEADERS)
        if 'id' in request.path_params:
            action = ('file' if 'position' in request.path_params else
                      (path.rsplit('/', 1)[1] if path.count('/') > 4 else 'page'))
            return await board_thread.route(ctx, request, action)
        if path in {'/web/board.js', '/web/thread.js'}:
            return Response(Path(__file__).with_name(path.rsplit('/', 1)[1]).read_text(),
                            media_type='text/javascript', headers=render.HEADERS)
        if path == '/web/app.css':
            return Response(Path(__file__).with_name('app.css').read_text(), media_type='text/css', headers=render.HEADERS)
        if path == '/web/signin':
            return await sign_in(ctx, request)
        if path == '/web/signout':
            token = request.cookies.get(signin.SESSION_COOKIE, '')
            values = await form_data(request)
            if not origin_ok(ctx, request) or not signin.valid_csrf(token, values.get('csrf', '')):
                return html(render.document('<main><p>The form is invalid.</p></main>'), 403)
            async with ctx.pool.connection() as conn:
                await conn.execute('DELETE FROM web_sessions WHERE token_hash=%s', (digest(token),))
            response = redirect('/web/signin')
            response.delete_cookie(signin.SESSION_COOKIE, path='/', httponly=True, samesite='lax', secure=not ctx.config.web_allow_http)
            return response
        if 'ask_id' in request.path_params:
            try:
                ask_id = int(request.path_params['ask_id'])
                position = int(request.path_params['position']) if 'position' in request.path_params else None
                if ask_id <= 0 or ask_id > 2**63-1:
                    raise ValueError
            except ValueError:
                return missing('The request was not found.')
            return await ask_page.route(ctx, request, ask_id, file_position=position)
        try:
            task_id = int(request.path_params['task_id'])
            position = int(request.path_params['position']) if 'position' in request.path_params else None
            if task_id <= 0 or task_id > 2**63-1:
                return missing()
        except ValueError:
            return missing()
        if request.method == 'POST':
            return await decide(ctx, request, task_id)
        return await task_page(ctx, request, task_id, file_position=position, progress=path.endswith('/progress'))

    for path, methods in [('/web/signin', ['GET', 'POST']), ('/web/signout', ['POST']),
                          ('/web/app.css', ['GET']), ('/web/board', ['GET']), ('/web/board.js', ['GET']), ('/web/a/{ask_id}', ['GET']),
                          ('/web/board/t/{id}', ['GET']), ('/web/board/t/{id}/posts', ['GET']),
                          ('/web/board/t/{id}/seen', ['POST']), ('/web/board/t/{id}/reply', ['POST']),
                          ('/web/board/t/{id}/reopen', ['POST']), ('/web/board/t/{id}/file/{post_id}/{position}', ['GET']),
                          ('/web/thread.js', ['GET']), ('/web/a/{ask_id}/answer', ['POST']), ('/web/a/{ask_id}/file/{position}', ['GET']), ('/t/{task_id}', ['GET']),
                          ('/t/{task_id}/progress', ['GET']), ('/t/{task_id}/file/{position}', ['GET']), ('/t/{task_id}/decide', ['POST'])]:
        ctx.app.add_api_route(path, route, methods=methods)
    ctx.board_thread_pages = True

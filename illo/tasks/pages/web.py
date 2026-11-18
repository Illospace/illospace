"""Private HTML routes. No model text becomes executable markup."""

import hmac
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
from illo.tasks.pages import images, render, signin


def html(body, status=200):
    return HTMLResponse(body, status_code=status, headers=render.HEADERS)


def missing():
    return html(render.document('<main><h1>The task was not found.</h1></main>'), 404)


def redirect(path, status=303):
    return RedirectResponse(path, status_code=status, headers=render.HEADERS)


def cookie(ctx, response, name, value, seconds):
    response.set_cookie(name, value, max_age=seconds, httponly=True, samesite='lax',
                        path='/', secure=not ctx.config.web_allow_http)


def origin_ok(ctx, request):
    # A browser sets Sec-Fetch-Site by itself, and a page cannot change it. It is correct also when the
    # browser hides the origin ("Origin: null"), which a strict referrer policy or a privacy setting causes.
    site = request.headers.get('sec-fetch-site')
    if site:
        return site == 'same-origin'
    if 'origin' not in request.headers:
        return True
    base = urlsplit(pages.base_url(ctx))
    return request.headers['origin'] == f'{base.scheme}://{base.netloc}'


async def form_data(request):
    # Do not need a multipart parser or a new dependency for a small form.
    if request.headers.get('content-type', '').split(';')[0] != 'application/x-www-form-urlencoded':
        return {}
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 32_000:
            return {}
    try:
        fields = parse_qs(body.decode('utf-8'), keep_blank_values=True, max_num_fields=10)
        return {k: v[0] for k, v in fields.items() if len(v) == 1}
    except (UnicodeError, ValueError):
        return {}


def signin_path(destination):
    return '/web/signin' + ('?' + urlencode({'next': destination}) if destination else '')


def start_form(destination, label):
    action = signin_path(destination)
    return f'<form method="post" action="{escape(action)}"><button class="btn go">{escape(label)}</button></form>'


async def sign_in(ctx, request):
    async with ctx.pool.connection() as conn:
        person = await signin.session(conn, request.cookies.get(signin.SESSION_COOKIE))
        destination = signin.next_path(request.query_params.get('next', ''))
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
                '<p>Illo gives you a code. You send the code to Illo in Slack.</p>'
                + start_form(destination, 'Get a code') + '</main>', title='Sign in to Illo'))
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
                + start_form(pending['next_path'], 'Get a new code') + '</main>'))
    body = ('<main class="signin"><h1>Sign in to Illo</h1>'
            '<p>Send this line to Illo in a Slack direct message. Then press Approve on the card.</p>'
            '<p class="code">connect ' + escape(signin.pairing_line(handle)) + '</p>'
            '<p class="by">The code works for 10 minutes. This page continues when you approve.</p></main>')
    response = html(render.document(body, title='Sign in to Illo', refresh=True))
    cookie(ctx, response, signin.REQUEST_COOKIE, handle, 600)
    return response


async def task_page(ctx, request, task_id, *, file_position=None):
    token = request.cookies.get(signin.SESSION_COOKIE, '')
    async with ctx.pool.connection() as conn:
        person = await signin.session(conn, token)
        if not person:
            return missing() if file_position is not None else redirect('/web/signin?' + urlencode({'next': f'/t/{task_id}'}), 302)
        viewer = tasks.Viewer(person['key'], 'person:' + person['key'], human=True)
        row = await tasks.get(conn, task_id, viewer=viewer)
        if not row:
            return missing()
        if file_position is None:
            return html(await render.page(ctx, conn, row, person, token))
        page = await one(conn, 'SELECT * FROM task_pages WHERE task_id=%s', (task_id,))
        if not page or file_position < 0 or file_position >= len(page['blocks']):
            return missing()
        image = page['blocks'][file_position].get('image')
        if not image:
            return missing()
        try:
            _, data, content_type = await images.read(ctx, image)
        except ValueError:
            return missing()
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
    return redirect(f'/t/{task_id}')


async def setup(ctx):
    ctx.slack_actions.register('web-signin:', signin.action)
    ctx.every(300, lambda: signin.cleanup(ctx))

    async def route(request: Request):
        if not pages.enabled(ctx):
            return missing()
        path = request.url.path
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
        try:
            task_id = int(request.path_params['task_id'])
            position = int(request.path_params['position']) if 'position' in request.path_params else None
            if task_id <= 0 or task_id > 2**63-1:
                return missing()
        except ValueError:
            return missing()
        if request.method == 'POST':
            return await decide(ctx, request, task_id)
        return await task_page(ctx, request, task_id, file_position=position)

    for path, methods in [('/web/signin', ['GET', 'POST']), ('/web/signout', ['POST']),
                          ('/web/app.css', ['GET']), ('/t/{task_id}', ['GET']),
                          ('/t/{task_id}/file/{position}', ['GET']), ('/t/{task_id}/decide', ['POST'])]:
        ctx.app.add_api_route(path, route, methods=methods)

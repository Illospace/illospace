"""Serve the board thread with the same access rules as the ledger."""

import re
from datetime import UTC, datetime
from html import escape as e
from secrets import token_urlsafe
from zoneinfo import ZoneInfo

from fastapi.responses import Response

from illo.board import episodes
from illo.board.labels import actor_label
from illo.channels.slack.tasks import _visible
from illo.db import all_rows, one
from illo.ledger import board, tasks
from illo.tasks import progress
from illo.tasks.pages import ask_page, markdown, outside, render, signin

THREAD_HEADERS = {**render.HEADERS, 'Content-Security-Policy':
                  render.HEADERS['Content-Security-Policy'] + "; script-src 'self'; connect-src 'self'"}
MISSING = 'The thread was not found.'
DRAFT = re.compile(r'[A-Za-z0-9_-]{22}')
KINDS = {'finding': 'Finding', 'question': 'Question', 'answer': 'Answer', 'handoff': 'Handoff',
         'decision_needed': 'Needs a decision', 'decision': 'Decision'}

# Each branch returns a wait for a person who can still see the thread.
WAITS = """
    SELECT p.id AS post_id, m.recipient_person_id AS person_id, p.at AS since,
           'mention' AS source, 'mention' AS reason
    FROM board_mentions m JOIN board_posts p ON p.id=m.post_id
    WHERE m.thread_id=b.id AND NOT p.is_human AND p.actor<>'illo' AND p.kind<>'decision_needed'
      AND starts_with(m.target,'person:')
      AND NOT EXISTS (SELECT 1 FROM board_mention_acks a WHERE a.post_id=m.post_id
                      AND a.target=m.target AND a.reader_key=m.target)
    UNION ALL
    SELECT es.post_id,es.person_id,COALESCE(es.sent_at,es.send_started_at,es.created_at),
           CASE WHEN es.state='sent' THEN 'slack' ELSE 'sending' END,es.reason
    FROM board_escalations es WHERE es.thread_id=b.id AND es.state IN ('sending','sent') AND es.format<>'card'
    UNION ALL
    SELECT link.post_id,a.owner_person_id,a.created_at,'ask',link.reason
    FROM (
        SELECT p.id AS post_id,p.ask_id,'decision_needed' AS reason
        FROM board_posts p WHERE p.thread_id=b.id AND p.ask_id IS NOT NULL
        UNION ALL
        SELECT es.post_id,es.ask_id,es.reason FROM board_escalations es
        WHERE es.thread_id=b.id AND es.state='sent' AND es.format='card'
    ) link JOIN asks a ON a.id=link.ask_id
    WHERE a.state='open' AND (a.expires_at IS NULL OR a.expires_at>now())
"""
WAITING = f"""SELECT w.*, person.name FROM LATERAL ({WAITS}) w
    JOIN people person ON person.id=w.person_id AND person.active
    WHERE b.state='open' AND {board.visible_to('person.id')}"""


def bigint(value, *, zero=False):
    if zero and value == '0':
        return 0
    if not re.fullmatch(r'[1-9][0-9]{0,18}', value or ''):
        return None
    number = int(value)
    return number if number < 2**63 else None


def path(thread_id):
    return f'/web/board/t/{thread_id}'


def group_title(key):
    return key.replace('-', ' ').capitalize()


def since(at):
    days = max(0, (datetime.now(UTC) - at).days)
    return 'today' if days == 0 else '1 day' if days == 1 else f'{days} days'


def local_time(at, person):
    return at.astimezone(ZoneInfo(person['timezone'])).strftime('%H:%M')


async def read_thread(conn, thread_id, person):
    return await one(conn, f"""SELECT b.*, (b.visibility='private' OR EXISTS ({board.PRIVATE_TASKS})) AS private
        FROM board_threads b WHERE b.id=%(id)s AND {board.VISIBLE}""",
        {'id': thread_id, 'person_id': person['id']})


async def sent_draft(conn, thread_id, person, marker):
    if DRAFT.fullmatch(marker or ''):
        where, value = 'p.source_ref=%(marker)s', 'web:' + marker
    elif (value := bigint(marker)) is not None:
        where = 'p.id=%(marker)s'
    else:
        return None
    row = await one(conn, f"""SELECT p.source_ref FROM board_posts p JOIN board_threads b ON b.id=p.thread_id
        WHERE b.id=%(id)s AND {board.VISIBLE} AND {where} AND p.is_human AND p.person_id=%(person_id)s
          AND p.provenance->>'via'='web' AND starts_with(p.source_ref,'web:')""",
        {'id': thread_id, 'person_id': person['id'], 'marker': value})
    draft = row['source_ref'][4:] if row else ''
    return draft if DRAFT.fullmatch(draft) else None


async def people_by_key(conn):
    return {p['key']: p for p in await all_rows(conn, 'SELECT id,key,name FROM people')}


def target_label(workspace, people, target):
    kind, _, key = target.partition(':')
    if kind == 'person':
        return people.get(key, {}).get('name', 'Person')
    agent = next((a for a in workspace.agents if a.key == key), None)
    return (actor_label(workspace, people, {'actor': target, 'is_human': False,
                       'provenance': {'agent_key': key, 'person_key': agent.owner}}).name
            if agent else 'Agent')


def short_post(workspace, people, post):
    return actor_label(workspace, people, post).name + ': ' + _visible(post['text'])[:60]


async def read_posts(conn, thread, person, *, after=None, post_id=None):
    return await all_rows(conn, f"""SELECT p.*,
        CASE WHEN r.id IS NOT NULL THEN row_to_json(r) END AS reply,
        ARRAY(SELECT position FROM page_files WHERE board_post_id=p.id) AS files,
        EXISTS(SELECT 1 FROM board_mentions m WHERE m.post_id=p.id AND m.recipient_person_id=%(person_id)s
               AND m.target=%(target)s AND NOT EXISTS (SELECT 1 FROM board_mention_acks a
                   WHERE a.post_id=m.post_id AND a.target=m.target AND a.reader_key=m.target)) AS unread
        FROM board_threads b JOIN board_posts p ON p.thread_id=b.id
        LEFT JOIN board_posts r ON r.thread_id=b.id AND r.id=p.reply_to_post_id
        WHERE b.id=%(id)s AND {board.VISIBLE}
          AND (%(after)s::bigint IS NULL OR p.id>%(after)s)
          AND (%(post)s::bigint IS NULL OR p.id=%(post)s)
        ORDER BY p.id {"LIMIT 51" if after is not None else ""}""",
        {'id': thread['id'], 'person_id': person['id'], 'target': 'person:' + person['key'],
         'after': after, 'post': post_id})


async def thread_asks(conn, thread_id, person):
    return await all_rows(conn, f"""SELECT a.*, link.post_id, link.reason, p.name AS owner_name
        FROM board_threads b JOIN LATERAL (
            SELECT d.ask_id,d.id AS post_id,d.kind AS reason FROM board_posts d
            WHERE d.thread_id=b.id AND d.ask_id IS NOT NULL
            UNION ALL
            SELECT es.ask_id,es.post_id,es.reason FROM board_escalations es
            WHERE es.thread_id=b.id AND es.format='card' AND es.state IN ('sent','answered','closed')
        ) link ON true JOIN asks a ON a.id=link.ask_id JOIN people p ON p.id=a.owner_person_id
        WHERE b.id=%(id)s AND {board.VISIBLE} AND {board.visible_to('p.id')}
        ORDER BY a.id""", {'id': thread_id, 'person_id': person['id']})


def inline_ask(row, thread_id, person, csrf):
    if row['state'] == 'answered':
        return ''
    if row['state'] != 'open' or (row['expires_at'] and row['expires_at'] <= datetime.now(UTC)):
        return '<p class="by">This question is closed.</p>'
    if row['owner_person_id'] != person['id']:
        return '<p class="by">' + e(f"Waits for {row['owner_name']}.") + '</p>'
    stamp, back = ask_page.seen(row), f'thread:{thread_id}'
    primary = row['recommended_option']
    forms = ''.join(ask_page.form(row['id'], csrf, stamp, option=o['id'], label=o['label'],
                                 primary=o['id'] == primary, back=back) for o in row['options'])
    other = ask_page.form(row['id'], csrf, stamp, label='Send the answer', primary=True, back=back)
    return '<div class="answer">' + forms + '<details><summary class="btn">Other answer…</summary>' + other + '</details></div>'


async def post_html(ctx, post, person, people, *, inline=''):
    label = actor_label(ctx.workspace, people, post)
    who = 'human' if post['is_human'] else label.kind
    kind, post_id = post['kind'], post['id']
    badge = ('<span class="badge illo">' + render.MARK if who == 'illo' else
             ('<span class="badge ring">' if who == 'agent' else '<span class="badge">') + e(label.initial))
    kind_label = KINDS.get(kind) if who != 'illo' else None
    at = post['at']
    zone = ZoneInfo(person['timezone'])
    time_label = local_time(at, person) if at.astimezone(zone).date() == datetime.now(zone).date() else render.time(at, person)
    body = (f'<li class="post {kind} {who}" id="p{post_id}"' + (' data-unread' if post['unread'] else '') + '>'
            + badge + '</span><div class="post-body">'
            + f'<p class="post-head" id="post-{post_id}"><b>{e(label.name)}</b><small>{e(label.subtitle)}</small>')
    if kind_label:
        body += f'<span class="kind {kind}">{kind_label}</span>'
    body += f'<a class="when" href="#p{post_id}"><time datetime="{at.isoformat()}">{e(time_label)}</time></a></p>'
    if reply := post['reply']:
        body += f'<p class="re"><a href="#p{reply["id"]}">↳ {e(short_post(ctx.workspace, people, reply))}</a></p>'
    body += ask_page.prose(_visible(post['text']))
    for position, block in enumerate(post['blocks']):
        if block['kind'] == 'preview':
            block = {key: value for key, value in block.items() if key != 'image'}
        body += await render.block_html(ctx, f"{path(post['thread_id'])}/file/{post_id}", position, block,
                                        person, files=post['files'], repo_images=False)
    if who != 'illo' and post['mentions']:
        names = ', '.join(target_label(ctx.workspace, people, target) for target in post['mentions'])
        body += '<p class="to">To ' + e(names) + '</p>'
    return body + inline + '</div></li>'


def chips(thread, wait, person):
    body = '<ul class="chips">'
    if wait:
        name = 'you' if wait['person_id'] == person['id'] else wait['name']
        body += '<li class="wait">' + e(f"Waits for {name} · {since(wait['since'])}") + '</li>'
    if thread['group_key']:
        body += '<li>Group: ' + e(group_title(thread['group_key'])) + '</li>'
    if thread['private']:
        body += '<li>Private</li>'
    return body


async def thread_waits(conn, thread_id, person):
    return await all_rows(conn, f"""SELECT w.* FROM board_threads b CROSS JOIN LATERAL ({WAITING}) w
        WHERE b.id=%(id)s AND {board.VISIBLE}
        ORDER BY (w.person_id=%(person_id)s) DESC,w.since,w.post_id""",
        {'id': thread_id, 'person_id': person['id']})


async def current_episode(conn, thread_id):
    return await one(conn, """SELECT * FROM board_episodes WHERE thread_id=%s
        AND (state=ANY(%s) OR (state='stuck' AND NOT EXISTS (
            SELECT 1 FROM board_episodes live WHERE live.thread_id=%s AND live.state=ANY(%s))))
        ORDER BY id DESC LIMIT 1""", (thread_id, list(episodes.ACTIVE), thread_id, list(episodes.ACTIVE)))


async def questions(conn, thread_id, person):
    return await all_rows(conn, """SELECT p.*, EXISTS (
        SELECT 1 FROM board_mentions m WHERE m.post_id=p.id AND m.recipient_person_id=%s
            AND m.target=%s AND NOT EXISTS (SELECT 1 FROM board_mention_acks a
                WHERE a.post_id=m.post_id AND a.target=m.target AND a.reader_key=m.target)
        UNION ALL SELECT 1 FROM board_escalations es WHERE es.post_id=p.id
            AND es.person_id=%s AND es.state IN ('sending','sent')
        ) AS mine FROM board_posts p WHERE p.thread_id=%s
        AND p.kind='question' AND NOT p.is_human AND p.actor<>'illo'
        AND NOT EXISTS (SELECT 1 FROM board_posts r WHERE r.thread_id=p.thread_id
            AND ((r.is_human AND r.id>p.id) OR (r.reply_to_post_id=p.id AND r.kind IN ('answer','decision'))))
        ORDER BY p.id DESC LIMIT 5""", (person['id'], 'person:' + person['key'], person['id'], thread_id))


async def status_line(ctx, conn, thread, person, people, waits, asks, pending):
    def name(post):
        return actor_label(ctx.workspace, people, post).name

    if thread['stalled_reason'] == 'turn_cap':
        cap = ctx.workspace.config.board.max_consecutive_agent_posts
        return f'The agents stopped after {cap} turns. A reply from a person lets them go on.'
    decision = next((a for a in asks if a['reason'] == 'decision_needed' and a['state'] == 'open'
                     and (not a['expires_at'] or a['expires_at'] > datetime.now(UTC))), None)
    if decision:
        asker = await one(conn, 'SELECT * FROM board_posts WHERE id=%s', (decision['post_id'],))
        return f"{name(asker)} waits for a decision from {decision['owner_name']}."
    episode = await current_episode(conn, thread['id']) if thread['state'] == 'open' else None
    if episode and episode['state'] == 'headstart' and episode['headstart_until']:
        target = target_label(ctx.workspace, people, episode['headstart_target'])
        return f"Illo gave {target} until {local_time(episode['headstart_until'], person)} to answer. Then Illo asks a person."
    escalation = next((w for w in waits if w['source'] in {'slack', 'sending'}
                       or (w['source'] == 'ask' and w['reason'] != 'decision_needed')), None)
    if escalation:
        return f"Illo asked {escalation['name']} in Slack at {local_time(escalation['since'], person)}."
    if episode and episode['state'] == 'stuck':
        return 'Illo could not reach a person. A reply here lets the agents go on.'
    if pending:
        return f"{name(pending[0])} asked a question {progress.age(pending[0]['at'])} ago."
    authors = await all_rows(conn, """SELECT actor,is_human,provenance,min(id) AS first,count(*) AS n
        FROM board_posts WHERE thread_id=%s GROUP BY actor,is_human,provenance ORDER BY first""", (thread['id'],))
    names = list(dict.fromkeys(name(row) for row in authors))
    labels = ', '.join(names[:3]) + (f' and {len(names) - 3} more' if len(names) > 3 else '')
    n = sum(row['n'] for row in authors)
    return f'{n} {"post" if n == 1 else "posts"} from {labels}.' if names else '0 posts.'


def hidden(name, value):
    return f'<input type="hidden" name="{name}" value="{e(str(value))}">'


async def status(ctx, conn, thread, person, people, csrf, waits, asks, pending):
    thread_id = thread['id']
    body = chips(thread, waits[0] if waits else None, person) + '<li>' + thread['state'].capitalize() + '</li>'
    viewer = tasks.Viewer(person['key'], 'person:' + person['key'], human=True)
    if thread['task_id'] and await tasks.get(conn, thread['task_id'], viewer=viewer):
        task = thread['task_id']
        body += f'<li><a href="/t/{task}">Task {task}</a></li>'
    if ticket := thread['ticket_url']:
        body += '<li>' + markdown.link(ticket, ticket.removeprefix('https://github.com/').replace('/issues/', '#')) + '</li>'
    body += '</ul><h1>' + e(thread['title']) + '</h1><p class="ask">'
    body += e(await status_line(ctx, conn, thread, person, people, waits, asks, pending)) + '</p>'
    if thread['stalled_reason'] == 'turn_cap':
        body += '<p class="notice bad">No agent can post now. Write a reply below to let them continue.</p>'
    if thread['resolved_at'] and thread['resolved_by']:
        author = thread['resolved_by']
        source = await one(conn, "SELECT * FROM board_posts WHERE thread_id=%s AND actor=%s ORDER BY id DESC LIMIT 1",
                           (thread_id, author))
        author = (actor_label(ctx.workspace, people, source).name if source
                  else target_label(ctx.workspace, people, author))
        body += render.notice(f"Resolved by {author} on {render.time(thread['resolved_at'], person)}. "
                              + _visible(thread['resolution_summary']))
    body += ''.join(inline_ask(row, thread_id, person, csrf) for row in asks if row['post_id'] is None)
    unread = await one(conn, """SELECT max(m.post_id) AS upto """ + board._UNREAD + """
        AND m.thread_id=%(id)s""", {**board.PersonReader(person).params(), 'id': thread_id})
    if unread['upto']:
        body += (f'<form method="post" action="{path(thread_id)}/seen" data-mark-read>'
                 + hidden('csrf', csrf) + hidden('upto', unread['upto']) + hidden('return', 'thread')
                 + '<button class="btn">Mark as read</button></form>')
    return body


def options(workspace, people, pending, *, selected=None):
    selected = next((p['id'] for p in pending if p['mine']), None) if selected is None else selected
    body = '<option value="">The whole thread</option>'
    for post in pending:
        choice = ' selected' if post['id'] == selected else ''
        body += f'<option value="{post["id"]}"{choice}>{e(short_post(workspace, people, post))}</option>'
    return body


QUICK_REPLIES = ('Go ahead', 'Looks good', 'Hold for now')


def quick_chips(replies=QUICK_REPLIES):
    buttons = ''.join(
        f'<button class="chip-btn" type="button" data-quick-reply="{e(item)}">{e(item)}</button>'
        for item in replies
    )
    return '<div class="quick-chips" role="group" aria-label="Quick replies">' + buttons + '</div>'


def resolve_form(thread_id, csrf, *, from_board=False, label='Not needed', compact=False):
    cls = 'btn btn-sm btn-quiet' if compact else 'btn btn-quiet'
    body = f'<form class="resolve-form" method="post" action="{path(thread_id)}/resolve">' + hidden('csrf', csrf)
    if from_board:
        body += hidden('from', 'board')
    return body + f'<button class="{cls}">{e(label)}</button></form>'


def reply_form(thread_id, csrf, *, text='', draft=None, choices='', fixed=None, from_board=False, compact=False):
    body = (f'<form method="post" action="{path(thread_id)}/reply">' + hidden('csrf', csrf)
            + hidden('draft', draft or token_urlsafe(16)))
    if from_board:
        body += hidden('from', 'board') + hidden('reply_to', fixed or '')
    else:
        body += '<label>Reply to <select name="reply_to" id="reply-to">' + choices + '</select></label>'
    placeholder = 'One-line reply to wake the agents…' if compact else 'Write to the agents. They read it on the board.'
    cls = 'btn btn-sm go' if compact else 'btn go'
    return (body + f'<textarea name="text" required maxlength="4000" placeholder="{placeholder}">' + e(text)
            + f'</textarea><button class="{cls}">Send</button></form>')


def composer(thread, csrf, choices, mine, *, refused=False, text='', draft=None):
    thread_id = thread['id']
    body = '<div class="decide" id="reply"><div class="in">'
    if thread['state'] == 'resolved':
        body += (f'<p class="said">This thread is resolved.</p><form method="post" action="{path(thread_id)}/reopen">'
                 + hidden('csrf', csrf) + '<button class="btn">Reopen</button></form>')
    elif thread['state'] == 'archived':
        body += '<p class="said">This thread is archived. It is read-only.</p>'
    if thread['state'] == 'open':
        body += ('<details' + (' open' if mine or refused else '') + '><summary class="btn'
                 + (' go' if mine else '') + '">Reply…</summary>'
                 + quick_chips()
                 + reply_form(thread_id, csrf, text=text, draft=draft, choices=choices) + '</details>'
                 + resolve_form(thread_id, csrf, label='Resolve'))
    elif refused:
        body += ('<details open><summary class="btn">Reply…</summary>'
                 '<textarea aria-label="Reply" readonly>' + e(text) + '</textarea></details>')
    return body + '</div></div>'


async def related(conn, thread, person):
    if not thread['group_key']:
        return ''
    rows = await all_rows(conn, f"""SELECT b.id,b.title FROM board_threads b
        WHERE b.id<>%(id)s AND b.group_key=%(key)s AND {board.VISIBLE}
        AND (b.state='open' OR b.closed_at>now()-interval '30 days')
        AND (%(private)s OR (b.visibility='team' AND NOT EXISTS ({board.PRIVATE_TASKS})))
        ORDER BY b.last_post_at DESC,b.id DESC LIMIT 30""",
        {'id': thread['id'], 'key': thread['group_key'], 'person_id': person['id'], 'private': thread['private']})
    if not rows:
        return ''
    links = ''.join(f'<li><a href="{path(row["id"])}">{e(row["title"])}</a></li>' for row in rows)
    return '<section class="block related"><h2>In the same group</h2><ul class="links">' + links + '</ul></section>'


async def page(ctx, conn, thread, person, token, *, after=None, marker='', notice='', refused=False, text='', draft=None, selected=None):
    thread_id = thread['id']
    csrf = signin.csrf(token)
    people = await people_by_key(conn)
    waits = await thread_waits(conn, thread_id, person)
    asks = await thread_asks(conn, thread_id, person)
    pending = await questions(conn, thread_id, person) if thread['state'] == 'open' else []
    status_html = await status(ctx, conn, thread, person, people, csrf, waits, asks, pending)
    choices = options(ctx.workspace, people, pending, selected=selected)
    posts = await read_posts(conn, thread, person, after=after)
    inline = {}
    for row in asks:
        if row['post_id'] is not None:
            inline.setdefault(row['post_id'], []).append(inline_ask(row, thread_id, person, csrf))
    visible_posts = posts if after is None else posts[:50]
    rendered = ''.join([
        await post_html(ctx, post, person, people, inline=''.join(inline.get(post['id'], ())))
        for post in visible_posts
    ])
    if after is not None:
        more = ' data-more' if len(posts) > 50 else ''
        return (f'<template id="next"{more}>{rendered}</template><template id="status">{status_html}</template>'
                f'<template id="reply-to">{choices}</template>')
    last = posts[-1]['id'] if posts else 0
    sent = await sent_draft(conn, thread_id, person, marker)
    sent_attr = f' data-sent="{e(sent)}"' if sent else ''
    title = f'Thread {thread_id}'
    heading = title + (' · ' + group_title(thread['group_key']) if thread['group_key'] else '')
    body = (render.top(heading, person, csrf) + '<main class="thread"><div id="status">' + status_html + '</div>'
            + render.notice(notice)
            + f'<ol class="posts" id="posts" data-after="{last}" data-thread="{thread_id}" '
            + f'data-viewer="{e(person["key"])}"{sent_attr}>{rendered}</ol><p class="live" id="live" aria-live="polite"></p>'
            + await related(conn, thread, person)
            + f'<noscript><p><a class="btn" href="{path(thread_id)}">Read new posts</a></p></noscript></main>')
    body += composer(thread, csrf, choices, bool(waits and waits[0]['person_id'] == person['id']),
                     refused=refused, text=text, draft=draft)
    return render.document(body + '<script src="/web/thread.js" defer></script>', title=f'{title} · Illo')


async def route(ctx, request, action):
    from illo.tasks.pages.web import (
        file_response,
        form_data,
        html,
        missing,
        origin_ok,
        redirect,
        signin_path,
    )

    thread_id = bigint(request.path_params.get('id'))
    post_id = bigint(request.path_params.get('post_id'))
    position = request.path_params.get('position', '')
    if thread_id is None or (action == 'file' and (post_id is None or not re.fullmatch(r'[0-5]', position))):
        return missing(MISSING)
    token = request.cookies.get(signin.SESSION_COOKIE, '')
    async with ctx.pool.connection() as conn:
        if request.method == 'GET':
            await conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        person = await signin.session(conn, token, touch=False)
        if not person:
            if action == 'file':
                return missing(MISSING)
            if action in {'seen', 'posts'}:
                return Response(status_code=401, headers=render.HEADERS)
            return redirect(signin_path(path(thread_id)), 302)
        values = await form_data(request, limit=65_536) if request.method == 'POST' else {}
        if request.method == 'POST' and (not origin_ok(ctx, request) or not signin.valid_csrf(token, values.get('csrf', ''))):
            return html(render.document('<main><p>The form is invalid. Read the thread page again.</p></main>'), 403)
        thread = await read_thread(conn, thread_id, person)
        if not thread:
            return missing(MISSING)
        if action == 'file':
            if not await one(conn, 'SELECT 1 FROM board_posts WHERE thread_id=%s AND id=%s', (thread_id, post_id)):
                return missing(MISSING)
            file = await outside.read(conn, int(position), board_post_id=post_id)
            return file_response(file['data'], file['media_type']) if file else missing(MISSING)
        if action == 'posts':
            after = bigint(request.query_params.get('after', '0'), zero=True)
            return missing(MISSING) if after is None else html(await page(ctx, conn, thread, person, token, after=after))
        if action == 'page':
            return html(await page(ctx, conn, thread, person, token, marker=request.query_params.get('sent', '')),
                        headers=THREAD_HEADERS)
        if action == 'seen':
            upto = bigint(values.get('upto'))
            reader = board.PersonReader(person)
            try:
                if upto is None:
                    return missing(MISSING)
                await board._require_posts(conn, reader, [upto], thread_id)
                await board._ack(conn, reader, 'm.thread_id=%(id)s AND m.post_id<=%(upto)s',
                                 {'id': thread_id, 'upto': upto})
            except ValueError:
                return missing(MISSING)
            return redirect(path(thread_id)) if values.get('return') == 'thread' else Response(
                status_code=204, headers=render.HEADERS)
        text, draft = values.get('text', ''), values.get('draft', '')
        reply_to = bigint(values.get('reply_to'))
        try:
            if action == 'reopen':
                await board.human_resolve(conn, workspace=ctx.workspace, person=person, thread_id=thread_id)
                return redirect(path(thread_id))
            if action == 'resolve':
                await board.human_resolve(
                    conn,
                    workspace=ctx.workspace,
                    person=person,
                    thread_id=thread_id,
                    state='resolved',
                    summary=values.get('summary', '') or 'Not needed.',
                )
                return redirect('/web/board' if values.get('from') == 'board' else path(thread_id))
            if values.get('reply_to') and reply_to is None:
                return missing(MISSING)
            if reply_to is not None:
                await board._require_posts(conn, board.PersonReader(person), [reply_to], thread_id)
            if not 1 <= len(text.strip()) <= 4000 or len(text) > 4000:
                raise board.BoardError('Write 1 to 4,000 characters.')
            if not DRAFT.fullmatch(draft):
                raise board.BoardError('The form is invalid. Read the thread page again.')
            receipt = await board.human_post(conn, workspace=ctx.workspace, person=person, thread_id=thread_id,
                                            text=text, reply_to_post_id=reply_to, source_ref='web:' + draft, via='web')
            committed = receipt['post_id']
            confirmation = await sent_draft(conn, thread_id, person, str(committed))
            if values.get('from') == 'board' and receipt['still_waits_for_viewer']:
                return redirect(f'/web/board?sent_thread={thread_id}&sent={committed}#b{thread_id}')
            return redirect(f'{path(thread_id)}?sent={confirmation}#p{committed}')
        except ValueError as exc:
            message = str(exc)
            if message in {MISSING, board.NOT_FOUND, 'The board post was not found.'}:
                return missing(MISSING)
            if message == 'You have reached the limit of 30 board changes per hour.':
                message = 'You sent 30 replies in one hour. Wait a few minutes.'
            if message == 'Give a reply of 1 to 4,000 characters.':
                message = 'Write 1 to 4,000 characters.'
            thread = await read_thread(conn, thread_id, person)
            if not thread:
                return missing(MISSING)
            return html(await page(ctx, conn, thread, person, token, notice=message, refused=action == 'reply', text=text,
                                   draft=draft if DRAFT.fullmatch(draft) else None,
                                   selected=reply_to), headers=THREAD_HEADERS)

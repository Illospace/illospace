"""The board uses the page readers and the page forms for each decision."""

from datetime import UTC, datetime, timedelta
from html import escape as e
from zoneinfo import ZoneInfo

from illo.board.labels import actor_label
from illo.channels.slack.tasks import holder_name
from illo.db import all_rows, one
from illo.ledger import ask_workflow, tasks
from illo.ledger import board as ledger
from illo.tasks import progress
from illo.tasks.pages import ask_page, board_thread, render, signin

HEADERS = board_thread.THREAD_HEADERS

async def needs(conn, person):
    viewer = tasks.Viewer(person['key'], 'person:' + person['key'], human=True)
    waiting = await tasks.search(conn, viewer=viewer, board='needs')
    asks = await ask_workflow.page_search(conn, person['id'])
    threads = await all_rows(conn, f"""SELECT b.id,b.title,b.group_key,b.stalled_reason,w.*,count(*) OVER () AS board_total,
        (b.visibility='private' OR EXISTS ({ledger.PRIVATE_TASKS})) AS private,
        COALESCE(w.post_id,(SELECT id FROM board_posts WHERE thread_id=b.id ORDER BY id DESC LIMIT 1)) AS preview_post_id
        FROM board_threads b JOIN LATERAL (
            SELECT * FROM ({board_thread.WAITING}) wait
            WHERE wait.person_id=%(person_id)s AND wait.source IN ('slack','mention')
            ORDER BY wait.since,wait.post_id LIMIT 1
        ) w ON true WHERE b.state='open' AND {ledger.VISIBLE}
        ORDER BY w.since,b.id LIMIT 30""", {'person_id': person['id']})
    frames = ([(r['wait_since'], 't', r) for r in waiting] + [(r['created_at'], 'a', r) for r in asks]
              + [(r['since'], 'b', r) for r in threads])
    frames.sort(key=lambda f: (f[0], f[1], f[2]['id']))
    total = sum(rows[0]['board_total'] if rows else 0 for rows in (waiting, asks, threads))
    return frames[:30], total


def history(row):
    return [{**event, 'at': datetime.fromisoformat(event['at'])} for event in row['board_events']]


def age(at):
    days = max(0, (datetime.now(UTC) - at).days)
    return progress.age(at) if not days else '1 day' if days == 1 else f'{days} days'


def title(row):
    return f'<h1><a href="/t/{row["id"]}">' + e(row['title']) + '</a></h1>'


async def waiting_frame(ctx, conn, kind, row, person, csrf, labels):
    if kind == 'b':
        return await thread_frame(ctx, conn, row, person, csrf, labels)
    if kind == 'a':
        data = await ask_workflow.card_data(conn, row)
        return (ask_page.request_intro(row, data, person, board=True)
                + await ask_page.request_blocks(ctx, conn, row, person)
                + await ask_page.request_proof(conn, row, person, compact=True)
                + ask_page.request_actions(row, person, csrf, ask_page.seen(row), board=True))
    stored = await one(conn, 'SELECT * FROM task_pages WHERE task_id=%s LIMIT 1', (row['id'],))
    return (await render.task_intro(ctx, conn, row, person, history(row), board=True)
            + await render.page_blocks(ctx, conn, row['id'], stored, person)
            + await render.task_actions(ctx, conn, row, person, csrf, render.seen(row, stored), board=True))


async def thread_frame(ctx, conn, row, person, csrf, people):
    thread_id = row['id']
    url = board_thread.path(thread_id)
    posts = await board_thread.read_posts(conn, row, person, post_id=row['preview_post_id']) if row['preview_post_id'] else []
    post = posts[0] if posts else None
    name = actor_label(ctx.workspace, people, post).name if post else 'An agent'
    if row['reason'] == 'turn_cap':
        cap = ctx.workspace.config.board.max_consecutive_agent_posts
        line = f'The agents stopped after {cap} turns. A reply from you lets them go on.'
    else:
        line = f"{name} {'mentioned' if row['reason'] == 'mention' else 'asks'} you."
    body = (board_thread.chips(row, row, person) + f'</ul><h1><a href="{url}">{e(row["title"])}</a></h1>'
            + '<p class="ask">' + e(line) + '</p>')
    if post:
        body += '<ol class="posts">' + await board_thread.post_html(ctx, post, person, people) + '</ol>'
    fixed = row['post_id'] if post and post['kind'] != 'decision_needed' else None
    return (body + '<div class="decide"><div class="in">'
            + board_thread.reply_form(thread_id, csrf, fixed=fixed, from_board=True)
            + f'<a class="btn" href="{url}">Open the thread</a></div></div>')


async def open_threads(conn, person):
    return await all_rows(conn, f"""WITH visible AS MATERIALIZED (
        SELECT b.* FROM board_threads b WHERE b.state='open' AND {ledger.VISIBLE}
    ), grouped AS (
        SELECT b.*,count(*) OVER (PARTITION BY b.group_key) AS group_count,
            max(b.last_post_at) OVER (PARTITION BY b.group_key) AS group_at,
            count(*) OVER () AS total, count(*) FILTER (WHERE w.person_id IS NOT NULL) OVER () AS waits,
            w.name AS wait_name,w.source AS wait_source,w.reason AS wait_reason
        FROM visible b LEFT JOIN LATERAL (
            SELECT * FROM ({board_thread.WAITING}) wait ORDER BY wait.since,wait.post_id LIMIT 1
        ) w ON true
    ), cards AS (
        SELECT *, row_number() OVER (ORDER BY group_key IS NULL, group_at DESC, group_key,
                                      last_post_at DESC,id DESC) AS card_order
        FROM grouped ORDER BY card_order LIMIT 30
    )
    SELECT c.*,last.actor,last.is_human,last.provenance,stats.posts,stats.agent_at,
           ep.state AS episode_state,ep.headstart_target,ep.headstart_until
    FROM cards c LEFT JOIN LATERAL (
        SELECT p.actor,p.is_human,p.provenance FROM board_posts p WHERE p.thread_id=c.id ORDER BY p.id DESC LIMIT 1
    ) last ON true LEFT JOIN LATERAL (
        SELECT count(*) AS posts,max(p.at) FILTER (WHERE NOT p.is_human AND p.actor<>'illo') AS agent_at
        FROM board_posts p WHERE p.thread_id=c.id
    ) stats ON true LEFT JOIN LATERAL (
        SELECT state,headstart_target,headstart_until FROM board_episodes ep
        WHERE ep.thread_id=c.id AND state IN ('headstart','stuck')
        AND (state='headstart' OR NOT EXISTS (
            SELECT 1 FROM board_episodes live WHERE live.thread_id=c.id AND live.state IN ('open','waiting','headstart','escalated')))
        ORDER BY id DESC LIMIT 1
    ) ep ON true ORDER BY c.card_order""", {'person_id': person['id']})


def thread_card(ctx, row, person, people):
    now = datetime.now(UTC)
    agent_at = row['agent_at']
    waits = row['wait_name']
    mark = ('live' if agent_at and now - agent_at < timedelta(minutes=2) else 'ring' if waits else 'still')
    name = actor_label(ctx.workspace, people, row).name if row['actor'] else 'Illo'
    if waits and (row['wait_source'] == 'mention' or row['wait_reason'] == 'decision_needed'):
        line = f'Waits for {waits}'
    elif row['stalled_reason'] == 'turn_cap':
        line = f'Stopped after {ctx.workspace.config.board.max_consecutive_agent_posts} turns'
    elif waits:
        line = f'Illo asked {waits}'
    elif row['episode_state'] == 'headstart' and row['headstart_until']:
        target = board_thread.target_label(ctx.workspace, people, row['headstart_target'])
        line = f"{target} has until {board_thread.local_time(row['headstart_until'], person)}"
    elif row['episode_state'] == 'stuck':
        line = 'Illo could not reach a person'
    elif agent_at and now - agent_at < timedelta(minutes=10):
        line = 'Agents talk'
    else:
        line = 'Quiet'
    count = row['posts']
    return (f'<article class="f"><p class="card-head"><span class="pulse {mark}"></span><b>{e(name)}</b>'
            f'<span>{e(age(row["last_post_at"]))}</span></p>'
            f'<h1><a href="{board_thread.path(row["id"])}">{e(row["title"])}</a></h1>'
            f'<div class="card-lines"><p>{count} {"post" if count == 1 else "posts"}</p><p>{e(line)}</p></div></article>')


def threads_column(ctx, threads, person, people):
    total, waits = (threads[0]['total'], threads[0]['waits']) if threads else (0, 0)
    line = f'{total} open · {waits} {"waits" if waits == 1 else "wait"} for a person' if total else 'No open thread.'
    body = '<section class="column" id="threads"><h2 class="column-title">Threads</h2><p class="column-line">' + e(line) + '</p>'
    if threads:
        grouped = threads[0]['group_key'] is not None
        previous = ''
        for row in threads:
            key = row['group_key']
            if grouped and key != previous:
                name = board_thread.group_title(key) if key else 'Other threads'
                body += f'<h3 class="group-title">{e(name)}<span>{row["group_count"]}</span></h3>'
            body += thread_card(ctx, row, person, people)
            previous = key
        if total > 30:
            body += f'<p class="column-more">{total - 30} more open threads.</p>'
    return body + '</section>'


async def work_frame(ctx, conn, row, person, viewer):
    name = await holder_name(ctx, conn, row['holder'])
    lines, steps, mark = [], '', 'still'
    if (row['holder'] or '').startswith('agent:'):
        value = await progress.read(conn, row, viewer)
        lines.append(dict(progress.STAGES).get(value['stage'], ''))
        if value.get('now') in progress.NOW:
            lines.append('Now: ' + progress.NOW[value['now']])
            mark = 'live'
        elif value['stage'] == 'waits':
            mark = 'ring'
        done = value.get('steps_done', sum(s['state'] == 'done' for s in value.get('steps') or []))
        total = value.get('steps_total', len(value.get('steps') or []))
        if total:
            steps = (f'<p class="count"><progress max="{total}" value="{min(done, total)}"></progress>'
                     f'<span>{done} of {total} steps</span></p>')
    else:
        lines.append(f'{name} holds it.')
    # A card is short: the holder and the age on one line, the title, then the state in soft words.
    body = (f'<p class="card-head"><span class="pulse {mark}"></span><b>' + e(name) + '</b><span>'
            + e('In work · ' + age(row['updated_at'])) + '</span></p>' + title(row))
    body += '<div class="card-lines">' + ''.join('<p>' + e(line) + '</p>' for line in lines if line) + '</div>'
    return body + steps


async def done_frame(ctx, conn, row, person):
    event = next((event for event in reversed(history(row)) if event['kind'] == 'done'), None)
    at = (event['at'] if event else row['done_at']).astimezone(ZoneInfo(person['timezone'])).strftime('%H:%M')
    if event and event['actor'].startswith('person:'):
        name = await holder_name(ctx, conn, event['actor'])
        line = f'{name} approved it at {at}.'
    else:
        line = f'Closed at {at}.'
    return '<span class="tick">✓</span>' + title(row) + '<p>' + e(line) + '</p>'


def column(key, label, line, frames, total, *, attributes=''):
    body = f'<section class="column" id="{key}"{attributes}><h2 class="column-title">{label}</h2><p class="column-line">'
    body += e(line) + '</p>' + ''.join(f'<article class="f" id="{key}">{body}</article>' for key, body in frames)
    if total > 30:
        body += f'<p class="column-more">{total - 30} more. Open the task list in Slack to see them.</p>'
    return body + '</section>'


async def page(ctx, conn, person, token, *, markers=None):
    csrf = signin.csrf(token)
    viewer = tasks.Viewer(person['key'], 'person:' + person['key'], human=True)
    waiting, total = await needs(conn, person)
    work = await tasks.search(conn, viewer=viewer, board='work')
    done = await tasks.search(conn, viewer=viewer, board='done')
    threads = await open_threads(conn, person)
    labels = await board_thread.people_by_key(conn) if threads or any(kind == 'b' for _, kind, _ in waiting) else {}
    date = datetime.now(ZoneInfo(person['timezone'])).strftime('%a %d %b')
    extra = '<span>' + e(date) + '</span><span class="zoom">'
    for action, label, text in [('out', 'Zoom out', '−'), ('in', 'Zoom in', '+'), ('fit', 'Show all', 'Fit')]:
        extra += f'<button class="btn" type="button" data-zoom="{action}" aria-label="{label}">{text}</button>'
    extra += '</span>'
    body = render.top('Board', person, csrf, extra=extra, board=True)
    body += '<div class="board" data-viewer="' + e(person['key']) + '"><div class="world">'
    line = (f'{total} {"thing" if total == 1 else "things"} · the oldest waits {age(waiting[0][0])}'
            if waiting else 'Nothing waits for you.')
    frames = [(f'{kind}{row["id"]}', await waiting_frame(ctx, conn, kind, row, person, csrf, labels)) for _, kind, row in waiting]
    attrs = ''
    marker_thread = board_thread.bigint(markers.get('sent_thread')) if markers else None
    if marker_thread and (draft := await board_thread.sent_draft(conn, marker_thread, person, markers.get('sent'))):
        attrs = f' data-sent-thread="{marker_thread}" data-sent="{e(draft)}"'
    body += column('needs', 'Needs you', line, frames, total, attributes=attrs)
    n, m = (work[0]['board_agents'], work[0]['board_people']) if work else (0, 0)
    line = f'Agents hold {n} {"task" if n == 1 else "tasks"}, people hold {m}' if work else 'No task is in work.'
    frames = [(f't{row["id"]}', await work_frame(ctx, conn, row, person, viewer)) for row in work]
    body += column('work', 'In work', line, frames, work[0]['board_total'] if work else 0)
    body += threads_column(ctx, threads, person, labels)
    total = done[0]['board_total'] if done else 0
    line = f'{total} {"task" if total == 1 else "tasks"} in the last 24 hours' if done else 'Nothing closed in the last 24 hours.'
    frames = [(f't{row["id"]}', await done_frame(ctx, conn, row, person)) for row in done]
    body += column('done', 'Done today', line, frames, total) + '</div></div>'
    body += '<script src="/web/board.js" defer></script>'
    return render.document(body, title='Board', body_class='board-page')

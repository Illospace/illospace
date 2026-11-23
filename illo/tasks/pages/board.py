"""The board uses the page readers and the page forms for each decision."""

from datetime import UTC, datetime
from html import escape as e
from zoneinfo import ZoneInfo

from illo.channels.slack.tasks import holder_name
from illo.db import one
from illo.ledger import ask_workflow, tasks
from illo.tasks import progress
from illo.tasks.pages import ask_page, render, signin

HEADERS = {**render.HEADERS, 'Content-Security-Policy':
           render.HEADERS['Content-Security-Policy'] + "; script-src 'self'"}


async def needs(conn, person):
    viewer = tasks.Viewer(person['key'], 'person:' + person['key'], human=True)
    waiting = await tasks.search(conn, viewer=viewer, board='needs')
    asks = await ask_workflow.page_search(conn, person['id'])
    frames = [(r['wait_since'], 't', r) for r in waiting] + [(r['created_at'], 'a', r) for r in asks]
    frames.sort(key=lambda f: (f[0], f[1], f[2]['id']))
    total = sum(rows[0]['board_total'] if rows else 0 for rows in (waiting, asks))
    return frames[:30], total


def history(row):
    return [{**event, 'at': datetime.fromisoformat(event['at'])} for event in row['board_events']]


def age(at):
    days = max(0, (datetime.now(UTC) - at).days)
    return progress.age(at) if not days else '1 day' if days == 1 else f'{days} days'


def title(row):
    return f'<h1><a href="/t/{row["id"]}">' + e(row['title']) + '</a></h1>'


async def waiting_frame(ctx, conn, kind, row, person, csrf):
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


def column(key, label, line, frames, total):
    body = f'<section class="column" id="{key}"><h2 class="column-title">{label}</h2><p class="column-line">'
    body += e(line) + '</p>' + ''.join(f'<article class="f" id="{key}">{body}</article>' for key, body in frames)
    if total > 30:
        body += f'<p class="column-more">{total - 30} more. Open the task list in Slack to see them.</p>'
    return body + '</section>'


async def page(ctx, conn, person, token):
    csrf = signin.csrf(token)
    viewer = tasks.Viewer(person['key'], 'person:' + person['key'], human=True)
    waiting, total = await needs(conn, person)
    work = await tasks.search(conn, viewer=viewer, board='work')
    done = await tasks.search(conn, viewer=viewer, board='done')
    date = datetime.now(ZoneInfo(person['timezone'])).strftime('%a %d %b')
    extra = '<span>' + e(date) + '</span><span class="zoom">'
    for action, label, text in [('out', 'Zoom out', '−'), ('in', 'Zoom in', '+'), ('fit', 'Show all', 'Fit')]:
        extra += f'<button class="btn" type="button" data-zoom="{action}" aria-label="{label}">{text}</button>'
    extra += '</span>'
    body = render.top('Board', person, csrf, extra=extra, board=True)
    body += '<div class="board"><div class="world">'
    line = (f'{total} {"thing" if total == 1 else "things"} · the oldest waits {age(waiting[0][0])}'
            if waiting else 'Nothing waits for you.')
    frames = [(f'{kind}{row["id"]}', await waiting_frame(ctx, conn, kind, row, person, csrf)) for _, kind, row in waiting]
    body += column('needs', 'Needs you', line, frames, total)
    n, m = (work[0]['board_agents'], work[0]['board_people']) if work else (0, 0)
    line = f'Agents hold {n} {"task" if n == 1 else "tasks"}, people hold {m}' if work else 'No task is in work.'
    frames = [(f't{row["id"]}', await work_frame(ctx, conn, row, person, viewer)) for row in work]
    body += column('work', 'In work', line, frames, work[0]['board_total'] if work else 0)
    total = done[0]['board_total'] if done else 0
    line = f'{total} {"task" if total == 1 else "tasks"} in the last 24 hours' if done else 'Nothing closed in the last 24 hours.'
    frames = [(f't{row["id"]}', await done_frame(ctx, conn, row, person)) for row in done]
    body += column('done', 'Done today', line, frames, total) + '</div></div>'
    body += '<script src="/web/board.js" defer></script>'
    return render.document(body, title='Board', body_class='board-page')

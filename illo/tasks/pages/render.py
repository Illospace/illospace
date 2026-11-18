"""The task page uses the shared task facts and the supplied page structure."""

import hashlib
from datetime import UTC, datetime
from html import escape as e
from zoneinfo import ZoneInfo

from illo.channels.slack.tasks import _can_review, _visible, holder_name
from illo.db import all_rows, one
from illo.ledger import digest_snapshot, tasks
from illo.tasks import list_page
from illo.tasks.pages import images, markdown, signin

HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
}


def document(body, *, title="Illo", refresh=False):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            + ('<meta http-equiv="refresh" content="3">' if refresh else '')
            + '<title>' + e(title) + '</title><link rel="stylesheet" href="/web/app.css">'
            '</head><body>' + body + '</body></html>')


def notice(text):
    return '<p class="notice">' + e(text) + '</p>' if text else ''


def seen(row, page):
    value = f"{row['result_version']}:{row['state']}:{page['written_at'] if page else ''}"
    return hashlib.sha256(value.encode()).hexdigest()


def time(at, person):
    if isinstance(at, str):
        at = datetime.fromisoformat(at.replace('Z', '+00:00'))
    return at.astimezone(ZoneInfo(person["timezone"])).strftime('%d %b, %H:%M') if at else ''


def form(task_id, csrf, stamp, action, label, *, reason=False):
    return (f'<form method="post" action="/t/{task_id}/decide">'
            f'<input type="hidden" name="csrf" value="{e(csrf)}">'
            f'<input type="hidden" name="seen" value="{e(stamp)}">'
            f'<input type="hidden" name="action" value="{e(action)}">'
            + ('<textarea name="reason" required maxlength="4000" placeholder="What must change?"></textarea>' if reason else '')
            + f'<button class="btn go">{e(label)}</button></form>')


async def block_html(ctx, task_id, position, block, person):
    kind = block['kind']
    heading = '<h2>' + e(block['title']) + '</h2>' if block.get('title') else ''
    image = ''
    if block.get('image'):
        try:
            await images.read(ctx, block['image'], data=False)
            image = f'<img src="/t/{task_id}/file/{position}" alt="{e(block.get("alt", ""))}">'
        except ValueError:
            image = '<div class="missing">The picture is not available.</div>'
    if kind == 'image':
        caption = '<figcaption>' + e(block['caption']) + '</figcaption>' if block.get('caption') else ''
        return '<figure class="block image">' + heading + image + caption + '</figure>'
    if kind == 'preview':
        name = _visible(block['name'])
        return ('<section class="block preview">' + heading + '<div class="as"><span class="badge">'
                + e(name[:1]) + '</span><div><b>' + e(name) + '</b><small>'
                + e(block.get('label', '')) + '</small></div></div><div class="text">'
                + e(_visible(block['text'])) + '</div>' + image + '</section>')
    if kind == 'text':
        body = '<div class="body prose">' + markdown.render(block['text']) + '</div>'
    elif kind == 'checks':
        body = '<ul class="checks">'
        for item in block['items']:
            state = item['state']
            source = markdown.link(item['source_url'], item['source_url']) + ' · ' if item.get('source_url') else ''
            checked = 'checked ' + time(item['checked_at'], person) if item.get('checked_at') else 'not checked'
            body += (f'<li class="{state}"><span class="dot">'
                     + {'yes': '✓', 'no': '!', 'open': '?'}[state] + '</span><div>'
                     + e(item['claim']) + '<small>' + source + e(checked) + '</small></div></li>')
        body += '</ul>'
    else:
        # A progress element needs no style attribute and no script, and its text stays readable on a phone.
        maximum = max((r['value'] for r in block['rows']), default=0) or 1
        body = '<div class="body bars">'
        for row in block['rows']:
            value = f"{row['value']:g} {row.get('unit', '')}".strip()
            body += ('<div class="bar"><span>' + e(row['label']) + '</span>'
                     f'<progress max="{maximum:g}" value="{row["value"]:g}"></progress>'
                     '<span>' + e(value) + '</span></div>')
        body += '</div>'
    return '<section class="block">' + heading + body + '</section>'


def collapse(events):
    result, reminders = [], {}
    for event in events:
        signature = (event['actor'], event['text'], event['data'].get('to'))
        if event['kind'] == 'reminded' and signature in reminders:
            index = reminders[signature]
            old, count = result[index]
            result[index] = old, count + 1
        else:
            if event['kind'] == 'reminded':
                reminders[signature] = len(result)
            result.append((event, 1))
    return result


async def page(ctx, conn, row, person, token, *, message='', failed=None):
    task_id = row['id']
    viewer = tasks.Viewer(person['key'], 'person:' + person['key'], human=True)
    stored = await one(conn, 'SELECT * FROM task_pages WHERE task_id=%s', (task_id,))
    events = await all_rows(conn, 'SELECT * FROM task_events WHERE task_id=%s ORDER BY id', (task_id,))
    since, period = digest_snapshot.wait_record(row, events)
    holder = await holder_name(ctx, conn, row['holder'])
    wait = 'you' if row['waits_for'] == viewer.actor else await holder_name(ctx, conn, row['waits_for'])
    days = max(0, (datetime.now(UTC) - since).days)
    entry = next((entry for entry in ctx.workspace.lists if entry.key == row['list_key']), None)
    csrf, stamp = signin.csrf(token), seen(row, stored)
    body = ('<header class="top"><span class="mark">i</span><span>'
            + e(f"Task {task_id} · {entry.name if entry else row['list_key']}")
            + '</span><span class="grow"></span><span>' + e(person['name']) + '</span>'
            '<form method="post" action="/web/signout">'
            f'<input type="hidden" name="csrf" value="{csrf}"><button>Sign out</button></form></header>'
            '<main><ul class="chips">')
    if row['waits_for']:
        since_words = 'today' if days == 0 else '1 day' if days == 1 else f'{days} days'
        body += '<li class="wait">' + e(f'Waits for {wait} · {since_words}') + '</li>'
    body += '<li>' + e(list_page.STATES[row['state']].capitalize()) + '</li><li>' + e('Holder: ' + holder) + '</li>'
    if row['due_at']:
        body += '<li>' + e('Due: ' + time(row['due_at'], person)) + '</li>'
    # The brief writes this reason inside a sentence. On the page it stands alone.
    ask = digest_snapshot.wait_reason(row, period, holder)
    ask = ask[:1].upper() + ask[1:]
    body += '</ul><h1>' + e(row['title']) + '</h1><p class="ask">' + e(ask) + '</p>'
    body += notice(message)
    for event in reversed(events):
        if event['kind'] in {'done', 'reopened'} and event['actor'].startswith('person:'):
            actor = 'You' if event['actor'] == viewer.actor else await holder_name(ctx, conn, event['actor'])
            verb = 'approved this' if event['kind'] == 'done' else 'sent this back'
            body += notice(f"{actor} {verb} on {time(event['at'], person)}: {_visible(event['text'])}")
            wake = next((x for x in reversed(events) if x['kind'] in {'woken', 'wake_failed'} and x['id'] > event['id']), None)
            if wake:
                body += notice(_visible(wake['text']))
            break
    if stored:
        for position, block in enumerate(stored['blocks']):
            body += await block_html(ctx, task_id, position, block, person)
        author = await holder_name(ctx, conn, stored['author']) if stored['author'] not in {'code', 'illo'} else 'Illo'
        body += '<p class="by">' + e(f"{author} wrote this page on {time(stored['written_at'], person)} for version {stored['result_version']} of the result.") + '</p>'
        if stored['result_version'] < row['result_version']:
            body += notice('The result changed after this page was written.')
    body += '<details><summary>The brief</summary><div class="prose">' + markdown.render(row['brief']) + '</div></details>'
    body += '<details open><summary>Returned work</summary><div class="prose">' + markdown.render(row['result'])
    returned = next((x for x in reversed(events) if x['kind'] == 'returned'), None)
    if returned:
        said = next((x for x in reversed(events) if x['id'] <= returned['id'] and x['actor'] == row['holder'] and x['kind'] in {'message', 'returned'} and x['text']), None)
        if said:
            body += markdown.render(said['text'])
    body += '</div><ul class="links">'
    labels = {'ticket': 'Ticket', 'repo': 'Repository', 'repository': 'Repository', 'pull': 'Pull request', 'pr': 'Pull request', 'evidence': 'Evidence', 'source': 'Source'}
    for item in row['links']:
        if list_page.safe_link(item.get('url', '')):
            label = item.get('title') or labels.get(item.get('kind'), item.get('kind', 'Evidence').capitalize())
            body += '<li>' + markdown.link(item['url'], label) + '</li>'
    if link := digest_snapshot.thread_link(row):
        body += '<li>' + markdown.link(link, 'Where it began') + '</li>'
    body += f'</ul></details><details><summary>History ({len(events)})</summary><ol class="trail">'
    for event, count in collapse(list(reversed(events[-50:]))):
        name = await holder_name(ctx, conn, event['actor']) if event['actor'] not in {'code', 'illo'} else 'Illo'
        body += '<li><time>' + e(time(event['at'], person)) + '</time> · <span class="actor">' + e(name) + '</span>'
        body += markdown.render(event['text']) + (f'<p>Reminders: {count}</p>' if count > 1 else '') + '</li>'
    body += '</ol></details></main>'
    actions = ''
    if row['state'] == 'review' and await _can_review(conn, ctx.workspace, row, person):
        actions += form(task_id, csrf, stamp, 'done', 'Approve')
        actions += '<details><summary class="btn">Send back…</summary>' + form(task_id, csrf, stamp, 'reopen', 'Send to the holder', reason=True) + '</details>'
        if failed:
            actions += '<div class="prose">' + ''.join('<p>' + e(c['text']) + '</p>' for c in failed if not c['ok']) + '</div>'
            actions += '<details open><summary class="btn">Confirm anyway…</summary>' + form(task_id, csrf, stamp, 'force', 'Confirm anyway', reason=True) + '</details>'
    elif row['state'] in {'todo', 'working'}:
        actions += '<details><summary class="btn">Message the holder…</summary>' + form(task_id, csrf, stamp, 'message', 'Send to the holder', reason=True) + '</details>'
    if actions:
        body += '<div class="decide"><div class="in">' + actions + '</div></div>'
    return document(body, title=f'Task {task_id} · Illo')

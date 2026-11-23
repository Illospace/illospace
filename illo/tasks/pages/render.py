"""The task page uses the shared task facts and the supplied page structure."""

import hashlib
from datetime import UTC, datetime
from html import escape as e
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from illo.channels.slack.tasks import _can_review, _visible, holder_name
from illo.db import all_rows, one
from illo.ledger import digest_snapshot, tasks
from illo.tasks import list_page, pages, progress
from illo.tasks.pages import images, markdown, outside, signin
from illo.tools.web import normalize_url

HEADERS = {
    "Cache-Control": "no-store",
    # Not "no-referrer": with that policy a browser sends "Origin: null" with each form of the page.
    "Referrer-Policy": "same-origin",
    "X-Robots-Tag": "noindex",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; base-uri 'none'; frame-src 'self'; frame-ancestors 'none'",
}


PROGRESS_HEADERS = {**HEADERS, 'Content-Security-Policy': HEADERS['Content-Security-Policy'].replace(
    "frame-ancestors 'none'", "frame-ancestors 'self'")}


def document(body, *, title="Illo", refresh=False, body_class=''):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            + (f'<meta http-equiv="refresh" content="{3 if refresh is True else refresh}">' if refresh else '')
            + '<title>' + e(title) + '</title><link rel="stylesheet" href="/web/app.css">'
            '</head><body' + (f' class="{e(body_class)}"' if body_class else '') + '>' + body + '</body></html>')


# The Illo mark. It takes the colour of the text, so it needs no style and no picture request.
MARK = ('<svg class="mark" viewBox="0 37 176 176" aria-hidden="true"><g transform="translate(-184 0)" fill="currentColor">'
        '<path fill-rule="evenodd" d="M 298.6 62.4 A 72 72 0 1 0 333.5 180.2 L 317.2 165.5 A 50 50 0 1 1 292.9 83.7 Z"/>'
        '<circle cx="295.8" cy="73.1" r="11"/><circle cx="325.3" cy="172.8" r="11"/>'
        '<path fill-rule="evenodd" d="M 341.1 93.8 A 72 72 0 0 1 332.7 181.1 L 316.6 166.1 A 50 50 0 0 0 322.4 105.5 Z"/>'
        '<circle cx="331.7" cy="99.7" r="11"/><circle cx="324.6" cy="173.6" r="11"/><circle cx="336" cy="64" r="17"/></g></svg>')


def top(label, person, csrf):
    return ('<header class="top">' + MARK + '<b>' + e(label) + '</b><span class="grow"></span><span>'
            + e(person['name']) + '</span><form method="post" action="/web/signout">'
            f'<input type="hidden" name="csrf" value="{e(csrf)}"><button>Sign out</button></form></header>')


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


async def block_html(ctx, file_base, position, block, person, *, files=(), repo_images=True):
    kind = block['kind']
    heading = '<h2>' + e(block['title']) + '</h2>' if block.get('title') else ''
    image = ''
    if picture := block.get('image'):
        available = False
        if 'url' in picture:
            if position in files:
                available = True
            elif block.get('image_link'):
                from illo.asks import proof

                address = str(normalize_url(picture['url']))
                _, name = proof.address(address) or (address, address)
                if not name.startswith('Picture: '):
                    name = 'Picture: ' + unquote(urlsplit(address).path.rsplit('/', 1)[-1])
                image = markdown.link(address, name)
        elif not repo_images:
            available = position in files
        else:
            try:
                await images.read(ctx, picture, data=False)
                available = True
            except ValueError:
                pass
        if available:
            image = f'<img src="{e(file_base)}/{position}" alt="{e(block.get("alt", ""))}">'
        elif not image:
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
    body = top(f"Task {task_id} · {entry.name if entry else row['list_key']}", person, csrf) + '<main><ul class="chips">'
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
    if row['state'] in {'todo', 'working'} and (row['holder'] or '').startswith('agent:'):
        value = await progress.read(conn, row, viewer)
        n = len(value.get('steps') or [])
        body += f'<iframe class="progress s{n}" src="/t/{task_id}/progress" title="Progress" loading="lazy"></iframe>'
    if stored:
        files = await outside.positions(conn, task_id=task_id)
        for position, block in enumerate(stored['blocks']):
            body += await block_html(ctx, f'/t/{task_id}/file', position, block, person, files=files)
        author = await holder_name(ctx, conn, stored['author']) if stored['author'] not in {'code', 'illo'} else 'Illo'
        body += '<p class="by">' + e(f"{author} wrote this page on {time(stored['written_at'], person)} for version {stored['result_version']} of the result.") + '</p>'
        if await pages.stale(conn, row, stored):
            body += notice('This page can show older work. The work changed after the page was written.')
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


async def progress_document(conn, row, person):
    viewer = tasks.Viewer(person['key'], 'person:' + person['key'], human=True)
    value = await progress.facts(conn, row, viewer)
    stages = [(key, 'Your review' if key == 'review' and row['waits_for'] == viewer.actor else label)
              for key, label in progress.STAGES if key != 'pull' or value['has_repo'] or value['has_pull']]
    current = next((i for i, (key, _) in enumerate(stages) if key == value['stage']), -1)
    body = '<section class="block"><h2>Progress</h2><div class="body"><ol class="stages">'
    for i, (_, label) in enumerate(stages):
        state = 'done' if i < current else 'here' if i == current else ''
        body += (f'<li class="{state}">' if state else '<li>') + e(label) + '</li>'
    body += '</ol>'
    lane = value['lane']
    # A collector sends a lane with no change each 10 minutes. A shorter limit calls a long command silent.
    if lane and not lane['closed_at'] and progress.minutes(lane['last_signal_at']) > 15:
        body += '<p>No signal for ' + str(progress.minutes(lane['last_signal_at'])) + ' minutes.</p>'
    elif lane and lane['live'] and value.get('now'):
        body += '<p class="now-line"><span>Now: ' + progress.NOW[value['now']] + '</span><span>' + progress.age(value['at']) + ' ago</span></p>'
    elif not lane and value['stage'] == 'waits':
        body += '<p>The agent did not start yet.</p>'
    body += '</div></section>'
    steps = value.get('steps') or []
    if steps:
        done = value.get('steps_done', sum(step['state'] == 'done' for step in steps))
        total = value.get('steps_total', len(steps))
        body += f'<section class="block"><h2>The agent\'s own steps · {done} of {total}</h2><ul class="steps">'
        for step in steps:
            state = step['state']
            body += f'<li class="{state}"><span class="dot">' + ('✓' if state == 'done' else '') + '</span><div>' + e(_visible(step['text'])) + '</div></li>'
        body += '</ul></section>'
    chips = []
    if value.get('files_changed') is not None:
        n = value['files_changed']
        chips.append(f'{n} file changed' if n == 1 else f'{n} files changed')
    if value['branch']:
        chips.append(_visible(value['branch']))
    if chips:
        body += '<ul class="chips">' + ''.join('<li>' + e(chip) + '</li>' for chip in chips) + '</ul>'
    active = row['state'] in {'todo', 'working'}
    if not active:
        body += f'<p>The state of the task changed. <a href="/t/{row["id"]}" target="_top">Read the page again</a></p>'
    return document(body, title='Progress', refresh=15 if active else False, body_class='frame')

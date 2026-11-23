"""Request pages use the same session and answer service as Slack."""

import hashlib
import hmac
import re
from datetime import UTC, datetime
from html import escape as e
from urllib.parse import urlencode

from illo.asks import cards, proof, service
from illo.channels.slack.tasks import _visible
from illo.ledger import ask_workflow as ledger
from illo.ledger import audit
from illo.tasks.list_page import safe_link
from illo.tasks.pages import markdown, outside, render, signin


def seen(row):
    fields = {key: row[key] for key in ('id', 'state', 'question', 'options', 'answered_at')}
    fields['answered_at'] = str(fields['answered_at'])
    return hashlib.sha256(proof.compact(fields).encode()).hexdigest()


def form(ask_id, csrf, stamp, *, option=None, label='', primary=False):
    return (f'<form method="post" action="/web/a/{ask_id}/answer">'
            f'<input type="hidden" name="csrf" value="{e(csrf)}">'
            f'<input type="hidden" name="seen" value="{e(stamp)}">'
            + (f'<input type="hidden" name="option" value="{e(option)}">' if option is not None else
               '<textarea name="text" required maxlength="4000" placeholder="Your answer"></textarea>')
            + f'<button class="{"btn go" if primary else "btn"}">{e(label)}</button></form>')


def prose(text, end=''):
    """A long text shows its first part. The reader opens the rest."""
    head, rest = markdown.cut(text)
    return ('<div class="body prose">' + markdown.render(head)
            + ('<details class="more"><summary>The rest of the text</summary>' + markdown.render(rest) + '</details>'
               if rest else '') + end + '</div>')


def address_html(value):
    url, title = proof.address(value)
    if title == url:
        title = re.sub(r'^https?://(?:www\.)?', '', url)
    # The task link rule is stricter for HTTP. Markdown also accepts external HTTP links.
    return markdown.link(url, title) if safe_link(url) or markdown.safe_url(url) else e(title)


def short(value):
    """A link, a number, or a few words: one line of the facts list, not a block of its own."""
    if proof.saved_ref(value) or isinstance(value, (dict, list)):
        return False
    return not isinstance(value, str) or bool(proof.address(value)) or (len(value) <= 80 and '\n' not in value)


async def value_html(conn, value, label, person, depth=0):
    if proof.saved_ref(value):
        item = (await proof.saved_items(conn, value, 'person:' + person['key']))[value]
        if not item:
            return '<section class="block"><h2>' + e(label) + '</h2><div class="missing">You cannot see this saved item.</div></section>'
        # The block has the title of the item. A first heading in the text says the same thing again.
        text = re.sub(r'\A\s*# [^\n]*\n?', '', item['body'])
        return ('<section class="block"><h2>' + e(item['title']) + '</h2>' + prose(text, '<p class="by">'
                + e(f"Saved item {value[6:]} · {render.time(item['source_at'] or item['created_at'], person)}")
                + '</p>') + '</section>')
    if isinstance(value, str):
        if proof.address(value):
            return '<div class="body prose"><p>' + address_html(value) + '</p></div>'
        return prose(value)
    if isinstance(value, (dict, list)) and depth < 2:
        pairs = value.items() if isinstance(value, dict) else [(None, child) for child in value]
        body = '<ul class="links">'
        for key, child in pairs:
            body += '<li>' + (e(str(key)) + ': ' if key is not None else '')
            body += await value_html(conn, child, proof.label(key) if key is not None else label, person, depth + 1)
            body += '</li>'
        return body + '</ul>'
    return '<div class="body prose"><p>' + e(proof.scalar(value)) + '</p></div>'


async def page(ctx, conn, row, person, token, *, message=''):
    data = await ledger.card_data(conn, row)
    owner = data['owner'] or {}
    mine = person['id'] == row['owner_person_id']
    csrf, stamp = signin.csrf(token), seen(row)
    body = render.top(f"Request {row['id']}", person, csrf) + '<main><ul class="chips">'
    if row['state'] == 'open':
        days = max(0, (datetime.now(UTC) - row['created_at']).days)
        since = 'today' if days == 0 else '1 day' if days == 1 else f'{days} days'
        wait = 'you' if mine else owner.get('name', 'Unknown')
        body += '<li class="wait">' + e(f'Waits for {wait} · {since}') + '</li>'
    body += '<li>' + ('Open' if row['state'] == 'open' else 'Answered' if row['state'] == 'answered' else 'Closed') + '</li>'
    body += '<li>' + e('From: ' + cards.who(data)[1]) + '</li>'
    if row['due_at']:
        body += '<li>' + e('Due: ' + render.time(row['due_at'], person)) + '</li>'
    body += '</ul><h1>' + e(row['question']) + '</h1><p class="ask">' + e(cards.first_line(data)) + '</p>'
    body += render.notice(message)
    if row['state'] == 'answered':
        if data['rule']:
            body += render.notice('A rule of yours answered: ' + data['rule']['text'])
        else:
            answer = next((o['label'] for o in row['options'] if o['id'] == row['answer_option']), row['answer_text'] or '')
            name = 'You' if mine else owner.get('name', 'Unknown')
            body += render.notice(f"{name} answered on {render.time(row['answered_at'], person)}: {answer}")
    elif row['state'] != 'open':
        body += render.notice('This question is closed. No answer is needed.')
    files = await outside.positions(conn, ask_id=row['id'])
    for position, block in enumerate(row.get('blocks', [])):
        body += await render.block_html(ctx, f"/web/a/{row['id']}/file", position, block, person, files=files, repo_images=False)
    material = row['proof']
    if text := proof.body(material):
        content = await value_html(conn, text, proof.label('text' if material.get('text') else 'body'), person)
        body += content if proof.saved_ref(text) else '<section class="block">' + content + '</section>'
    facts = []

    def facts_html():
        text = ('<section class="block"><dl class="facts">' + ''.join(facts) + '</dl></section>') if facts else ''
        facts.clear()
        return text

    for key, value in proof.entries(material, row.get('proof_order')):
        label = proof.label(key)
        if short(value):
            facts.append('<dt>' + e(label) + '</dt><dd>'
                         + (address_html(value) if proof.address(value) else e(_visible(proof.scalar(value)))) + '</dd>')
            continue
        body += facts_html()
        if proof.saved_ref(value):
            body += await value_html(conn, value, label, person)
            continue
        if key == 'links':
            content = '<ul class="links">' + ''.join('<li>' + markdown.link(url, title) + '</li>'
                                                    for url, title in proof.links(value))
            for ref in proof.references(value):
                content += '<li>' + await value_html(conn, ref, label, person) + '</li>'
            content += '</ul>'
        else:
            content = await value_html(conn, value, label, person)
        body += '<section class="block"><h2>' + e(label) + '</h2>' + content + '</section>'
    body += facts_html()
    details = [o for o in row['options'] if o.get('detail')]
    if details and row['state'] == 'open' and mine:
        body += '<h2>The options</h2><ul class="links">' + ''.join(
            '<li>' + e(o['label'] + ': ' + o['detail']) + '</li>' for o in details) + '</ul>'
    body += '</main>'
    if row['state'] == 'open' and mine:
        body += '<div class="decide"><div class="in">'
        for option in sorted(row['options'], key=lambda o: o['id'] != row['recommended_option']):
            body += form(row['id'], csrf, stamp, option=option['id'], label=option['label'],
                         primary=option['id'] == row['recommended_option'])
        body += ('<details><summary class="btn">Other answer…</summary>'
                 + form(row['id'], csrf, stamp, label='Send the answer', primary=True) + '</details></div></div>')
    return render.document(body, title=f"Request {row['id']} · Illo")


async def route(ctx, request, ask_id, *, file_position=None):
    from illo.tasks.pages.web import file_response, form_data, html, missing, origin_ok, redirect

    token = request.cookies.get(signin.SESSION_COOKIE, '')
    async with ctx.pool.connection() as conn:
        person = await signin.session(conn, token)
        if not person:
            if file_position is not None:
                return missing('The request was not found.')
            return redirect('/web/signin?' + urlencode({'next': f'/web/a/{ask_id}'}), 302)
        values = await form_data(request) if request.method == 'POST' else {}
        if request.method == 'POST' and (not origin_ok(ctx, request) or not signin.valid_csrf(token, values.get('csrf', ''))):
            return html(render.document('<main><p>The form is invalid. Read the page again.</p></main>'), 403)
        row = await ledger.page_read(conn, ask_id, person['id'], lock=request.method == 'POST')
        if not row:
            return missing('The request was not found.')
        if file_position is not None:
            file = await outside.read(conn, file_position, ask_id=ask_id)
            return file_response(file['data'], file['media_type']) if file else missing('The request was not found.')
        if request.method == 'GET':
            return html(await page(ctx, conn, row, person, token))
        if person['id'] != row['owner_person_id']:
            return html(render.document('<main><p>This action is not available.</p></main>'), 403)
        if not hmac.compare_digest(seen(row).encode(), values.get('seen', '').encode()):
            return html(await page(ctx, conn, row, person, token,
                                  message='The request changed after you opened the page. Read it again.'))
        if not await service.is_live(conn, row):
            row = await ledger.page_read(conn, ask_id, person['id'])
            return html(await page(ctx, conn, row, person, token, message='This question is closed. No answer is needed.'))
        option, text = values.get('option'), values.get('text', '')
        if option is not None and not any(o['id'] == option for o in row['options']):
            return html(render.document('<main><p>This action is not available.</p></main>'), 403)
        if option is None and (not text.strip() or len(text) > 4000):
            return html(await page(ctx, conn, row, person, token, message='Give an answer of 1 to 4,000 characters.'))
        answered = await service.answer(conn, ask_id, person['id'], option=option, text=text if option is None else None)
        if not answered:
            row = await ledger.page_read(conn, ask_id, person['id'])
            return html(await page(ctx, conn, row, person, token, message='This question is closed. No answer is needed.'))
        await audit.write(conn, principal='person:' + person['key'], tool='ask_answer', args={'id': ask_id},
                          decision='allow', reason='The person answers on the request page.', target=f'ask {ask_id}')
    await service.refresh_ask(ctx, ask_id)
    return redirect(f'/web/a/{ask_id}')

"""Agent requests share one card, private pages, and one owner answer."""

import asyncio

import httpx
import pytest
from pydantic import ValidationError, field_validator

from illo.asks import actions, proof, service
from illo.channels.mcp.schemas import AskCreate
from illo.db import all_rows, one
from illo.ledger import ask_workflow as ledger
from illo.ledger import asks, items, people, rules, tokens
from illo.ledger.tokens import digest
from illo.tasks.pages import ask_page, render, signin
from illo.tools.asks import CreateArgs
from illo.tools.registry import Arguments, schema_reason
from illo.workspace import PagesConfig
from tests.mcp.client import AgentClient
from tests.tasks.test_pages import BASE, Browser, fields, link_of

ASK = {'question': 'Can you review this plan?', 'kind': 'action', 'owner': 'ben',
       'options': [{'id': 'yes', 'label': 'Use the complete proposed plan', 'detail': 'Check each part.'},
                   {'id': 'no', 'label': 'Wait'}], 'recommended': 'yes'}
REPAIR = 'The question is one short sentence, 240 characters at most. Put the rest in proof.text.'


@pytest.fixture
async def ctx(app):
    ctx = app.state.ctx
    await ctx.scheduler.close()
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.config.pages = PagesConfig(enabled=True, url=BASE)
    async with ctx.pool.connection() as conn:
        await conn.execute("INSERT INTO people(key,name,role,timezone) VALUES ('david','David','owner','UTC')")
    return ctx


@pytest.fixture
async def browser(ctx, app, slack_client):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as http:
        yield Browser(app, http, slack_client)


async def login(ctx, http, person='ben'):
    token = 'test-browser-' + person
    async with ctx.pool.connection() as conn:
        who = await people.get(conn, person)
        await conn.execute('INSERT INTO web_sessions(token_hash,person_id) VALUES (%s,%s) ON CONFLICT DO NOTHING',
                           (digest(token), who['id']))
    http.cookies.set(signin.SESSION_COOKIE, token)


async def read(ctx, ask_id):
    async with ctx.pool.connection() as conn:
        return await ledger.lock_ask(conn, ask_id)


async def member(ctx, key='ben'):
    async with ctx.pool.connection() as conn:
        return await people.get(conn, key)


async def saved(ctx, *, visibility='team', owner='ada', body='The saved plan includes every step.'):
    async with ctx.pool.connection() as conn:
        who = await people.get(conn, owner)
        # A saved reference is a source reference or alias, not an items.id lookup.
        return await items.upsert(conn, source_ref='saved:77', title='The saved plan', body=body,
                                  owner_person_id=who['id'], visibility=visibility)


def posts(slack):
    return [call for call in slack.calls if call['method'] == 'chat_postMessage']


def click(row, verb='opt:yes'):
    return {'action_id': f"ask:{row['id']}:{verb}", 'channel': row['slack_channel'], 'message_ts': row['slack_ts']}


async def test_agent_repair_cancel_reuse_and_complete_page(ctx, app, http, browser, slack_client):
    agent = await AgentClient.create(app, http, label='Example agent')
    for question in ('First question? Second question?', 'x' * 241):
        error = await agent.call('ask_create', {**ASK, 'question': question}, error=True)
        assert REPAIR in error['error']['message']
    await saved(ctx)
    material = {'text': 'Read the whole plan.\nThe final condition matters.',
                'suggested_reply': 'I can review the plan tomorrow.',
                'picture': 'https://example.com/plan.png', 'brief': 'saved:77',
                'links': [f'https://example.com/{n}' for n in range(6)]}
    first = await agent.call('ask_create', {**ASK, 'proof': material, 'source_ref': 'request-1'})
    assert first['page_url'] == BASE + f"/web/a/{first['ask_id']}"
    assert first['card_text'] == '🔧 Ada, through Example agent, asks you to do something.\n' + ASK['question']
    await service.tick(ctx)
    original = await read(ctx, first['ask_id'])
    for n in (2, 3):
        result = await agent.call('ask_cancel', {'ask_id': original['id']})
        assert result['state'] == 'cancelled'
        await service.tick(ctx)
        second = await agent.call('ask_create', {**ASK, 'proof': material, 'source_ref': f'request-{n}'})
        await service.tick(ctx)
        moved = await read(ctx, second['ask_id'])
        assert moved['slack_ts'] == original['slack_ts'] and moved['slack_channel'] == original['slack_channel']
        assert (await read(ctx, original['id']))['slack_ts'] is None
        original = moved
    assert len(posts(slack_client)) == 1
    assert len([c for c in slack_client.calls if c['method'] == 'chat_update']) >= 2
    await login(ctx, browser.http)
    page = await browser.http.get(f"/web/a/{original['id']}")
    assert page.status_code == 200
    for text in ('The final condition matters.', '<dt>Suggested reply</dt>', 'I can review the plan tomorrow.',
                 'Picture: plan.png', 'The saved plan includes every step.', 'Saved item 77', 'https://example.com/5'):
        assert text in page.text
    assert '<img' not in page.text and '<script' not in page.text and 'style=' not in page.text
    assert page.text.index('Use the complete proposed plan') < page.text.index('>Wait<')
    assert 'always:' not in str(posts(slack_client)[0]['blocks'])
    assert posts(slack_client)[0]['text'] == first['card_text']
    assert posts(slack_client)[0]['blocks'][2]['text']['text'].startswith('Why:')
    assert 'suggested_reply' not in str(posts(slack_client)[0]['blocks'])


async def test_page_keeps_requester_order_and_folds_a_long_item(ctx, app, http, browser):
    agent = await AgentClient.create(app, http, label='Example agent')
    await saved(ctx, body='# The saved plan\n\n## Part 1\n' + 'A sentence of the plan.\n\n' * 200 + 'The last line.')
    material = {'zz_summary': 'The first entry is a text of more than 80 characters, so it gets a block of its own on the page.',
                'report': 'saved:77', 'ticket': 'https://example.com/issues/3331', 'a_number': 1225889,
                'note': 'OK <!-- hidden words -->'}
    made = await agent.call('ask_create', {**ASK, 'proof': material})
    assert (await read(ctx, made['ask_id']))['proof_order'] == list(material)
    await login(ctx, browser.http)
    text = (await browser.http.get(f"/web/a/{made['ask_id']}")).text
    places = [text.index(part) for part in ('<h2>Zz summary</h2>', '<h2>The saved plan</h2>', '<dt>Ticket</dt>', '<dt>A number</dt>')]
    assert places == sorted(places)
    assert '<h3>Part 1</h3>' in text and '# The saved plan' not in text
    assert '>example.com/issues/3331</a>' in text and '<dd>1225889</dd>' in text
    assert text.index('<details class="more"><summary>The rest of the text</summary>') < text.index('The last line.')
    assert '<dt>Note</dt><dd>OK' in text and 'hidden words' not in text
    assert '<svg class="mark"' in text and 'style=' not in text


@pytest.mark.parametrize('viewer,status,bar', [('ben', 200, True), ('ada', 200, False),
                                              ('chloe', 404, False), ('david', 404, False), (None, 302, False)])
async def test_reader_matrix(ctx, app, http, browser, viewer, status, bar):
    agent = await AgentClient.create(app, http)
    ask = await agent.call('ask_create', ASK)
    if viewer:
        await login(ctx, browser.http, viewer)
    page = await browser.http.get(f"/web/a/{ask['ask_id']}")
    assert page.status_code == status
    assert ('class="decide"' in page.text) is bar
    if status == 404:
        missing = await browser.http.get('/web/a/999999')
        assert page.text == missing.text and 'The request was not found.' in page.text
    if status == 302:
        assert page.headers['location'] == '/web/signin?next=%2Fweb%2Fa%2F' + str(ask['ask_id'])
    if viewer == 'ada':
        assert 'Waits for Ben · today' in page.text
    if viewer == 'ben':
        assert 'Waits for you · today' in page.text
        for key, value in render.HEADERS.items():
            assert page.headers[key] == value


async def test_saved_item_visibility_changes_and_aliases(ctx, app, http, browser):
    item = await saved(ctx)
    assert item['id'] != 77
    async with ctx.pool.connection() as conn:
        await conn.execute("INSERT INTO item_aliases(alias,item_id) VALUES ('saved:88',%s)", (item['id'],))
    agent = await AgentClient.create(app, http)
    ask = await agent.call('ask_create', {**ASK, 'proof': {'brief': ['saved:88']}})
    await login(ctx, browser.http)
    path = f"/web/a/{ask['ask_id']}"
    assert 'The saved plan includes every step.' in (await browser.http.get(path)).text
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE items SET visibility='private' WHERE id=%s", (item['id'],))
    page = await browser.http.get(path)
    assert 'You cannot see this saved item.' in page.text and 'every step' not in page.text
    await login(ctx, browser.http, 'ada')
    assert 'The saved plan includes every step.' in (await browser.http.get(path)).text
    async with ctx.pool.connection() as conn:
        await conn.execute('DELETE FROM items WHERE id=%s', (item['id'],))
    assert 'You cannot see this saved item.' in (await browser.http.get(path)).text


@pytest.mark.parametrize('owner,message', [('ada', 'The person who must answer cannot see saved:77.'),
                                           ('ben', 'You cannot see saved:77.')])
async def test_saved_ref_refusal_writes_nothing(ctx, app, http, owner, message):
    await saved(ctx, visibility='private', owner=owner)
    agent = await AgentClient.create(app, http)
    result = await agent.call('ask_create', {**ASK, 'proof': {'nested': {'brief': 'saved:77'}}}, error=True)
    assert result['error']['message'] == message
    async with ctx.pool.connection() as conn:
        assert not await one(conn, 'SELECT id FROM asks')
        assert not await one(conn, 'SELECT id FROM lanes')
    unknown = await agent.call('ask_create', {**ASK, 'proof': {'brief': 'saved:999999'}}, error=True)
    assert unknown['error']['message'] == 'You cannot see saved:999999.'


async def test_answer_form_guards_and_audit(ctx, app, http, browser, slack_client):
    agent = await AgentClient.create(app, http)
    ask = await agent.call('ask_create', ASK)
    await service.tick(ctx)
    path = f"/web/a/{ask['ask_id']}"
    assert (await browser.http.post(path + '/answer')).status_code == 302
    await login(ctx, browser.http)
    form = fields(await browser.http.get(path))
    for headers, changes in (({'Origin': 'https://evil.example'}, {}), ({'Sec-Fetch-Site': 'cross-site'}, {}),
                             ({}, {'csrf': 'wrong'}), ({}, {'csrf': 'é'})):
        invalid = await browser.http.post(path + '/answer', data={**form, 'option': 'yes', **changes}, headers=headers)
        assert invalid.status_code == 403 and 'The form is invalid. Read the page again.' in invalid.text
    for text in ('', ' ', 'x' * 4001):
        invalid = await browser.http.post(path + '/answer', data={**form, 'text': text})
        assert 'Give an answer of 1 to 4,000 characters.' in invalid.text
    invalid = await browser.http.post(path + '/answer', data={**form, 'option': 'unknown'})
    assert invalid.status_code == 403
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE asks SET question='Can you review the new plan?' WHERE id=%s", (ask['ask_id'],))
    stale = await browser.http.post(path + '/answer', data={**form, 'option': 'yes'})
    assert 'The request changed after you opened the page. Read it again.' in stale.text
    result = await browser.http.post(path + '/answer', data={**fields(stale), 'text': 'Use the shorter plan.'})
    assert result.status_code == 303 and result.headers['location'] == path
    page = await browser.http.get(path)
    assert 'You answered on' in page.text and 'Use the shorter plan.' in page.text and 'class="decide"' not in page.text
    async with ctx.pool.connection() as conn:
        audit = await one(conn, "SELECT * FROM audit WHERE tool='ask_answer'")
        assert audit['reason'] == 'The person answers on the request page.'
        assert await items.get(conn, f"ask:{ask['ask_id']}", principal='person:ada')
    assert any(c['method'] == 'chat_update' and 'Answered:' in str(c['blocks']) for c in slack_client.calls)


async def test_requester_cannot_answer_and_cancel_closes_page(ctx, app, http, browser):
    agent = await AgentClient.create(app, http)
    ask = await agent.call('ask_create', ASK)
    path = f"/web/a/{ask['ask_id']}"
    await login(ctx, browser.http, 'ada')
    token = browser.http.cookies.get(signin.SESSION_COOKIE)
    row = await read(ctx, ask['ask_id'])
    denied = await browser.http.post(path + '/answer', data={'csrf': signin.csrf(token), 'seen': ask_page.seen(row), 'option': 'yes'})
    assert denied.status_code == 403
    await login(ctx, browser.http)
    form = fields(await browser.http.get(path))
    await agent.call('ask_cancel', {'ask_id': ask['ask_id']})
    stale = await browser.http.post(path + '/answer', data={**form, 'option': 'yes'})
    assert 'The request changed' in stale.text
    closed = await browser.http.post(path + '/answer', data={**fields(await browser.http.get(path)), 'option': 'yes'})
    assert 'This question is closed. No answer is needed.' in closed.text
    assert (await read(ctx, ask['ask_id']))['answer_option'] is None


async def test_simultaneous_slack_and_page_answer_commit_once(ctx, app, http, browser):
    agent = await AgentClient.create(app, http)
    ask = await agent.call('ask_create', ASK)
    await service.tick(ctx)
    row = await read(ctx, ask['ask_id'])
    await login(ctx, browser.http)
    path = f"/web/a/{row['id']}"
    form = fields(await browser.http.get(path))
    await asyncio.gather(actions.handle(ctx, await member(ctx), click(row)),
                         browser.http.post(path + '/answer', data={**form, 'option': 'no'}))
    current = await read(ctx, row['id'])
    assert current['state'] == 'answered' and current['answer_option'] in {'yes', 'no'}
    async with ctx.pool.connection() as conn:
        decisions = await all_rows(conn, "SELECT * FROM items WHERE source_ref=%s", (f"ask:{row['id']}",))
        assert len(decisions) == 1
        expected = next(o['label'] for o in row['options'] if o['id'] == current['answer_option'])
        assert 'Answer: ' + expected in decisions[0]['body']


async def test_pages_off_complete_card_and_size_refusal(ctx, app, http, slack_client):
    ctx.workspace.config.pages.enabled = False
    await saved(ctx)
    agent = await AgentClient.create(app, http)
    links = [{'url': f'https://example.com/link-{n}', 'title': f'Link {n}'} for n in range(8)]
    material = {'body': 'Start.\n' + 'x' * 6000 + '\nThe last line.',
                'suggested_reply': 'Read all the details.', 'notes': {'steps': ['First', 'Last']},
                'picture': 'https://example.com/picture.jpg', 'brief': 'saved:77', 'links': links, 'area': 'hidden'}
    created = await agent.call('ask_create', {**ASK, 'proof': material})
    assert created['page_url'] is None
    await service.tick(ctx)
    card = posts(slack_client)[0]
    text = '\n'.join(b['text']['text'] for b in card['blocks'] if b['type'] == 'section')
    for expected in ('The last line.', 'Read all the details.', 'First', 'Last', 'Picture: picture.jpg',
                     'The saved plan includes every step.', 'link-7'):
        assert expected in text
    assert text.count('x') >= 6000 and 'hidden' not in text and 'web-open' not in str(card)
    assert all(len(b['text']['text']) <= 2800 for b in card['blocks'] if b['type'] == 'section')
    assert (await http.get(f"/web/a/{created['ask_id']}")).status_code == 404
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE items SET body=%s WHERE source_ref='saved:77'", ('x' * 130000,))
    result = await agent.call('ask_create', {**ASK, 'proof': {'brief': 'saved:77'}, 'source_ref': 'too-long'}, error=True)
    assert result['error']['message'] == proof.CARD_LIMIT
    async with ctx.pool.connection() as conn:
        assert (await one(conn, 'SELECT count(*) AS n FROM asks'))['n'] == 1


async def test_takeover_refuses_old_button_and_moves_thread(ctx, app, http, replay):
    agent = await AgentClient.create(app, http)
    first = await agent.call('ask_create', ASK)
    await service.tick(ctx)
    original = await read(ctx, first['ask_id'])
    await actions.handle(ctx, await member(ctx), click(original, 'other'))
    await agent.call('ask_cancel', {'ask_id': original['id']})
    second = await agent.call('ask_create', {**ASK, 'source_ref': 'new-request'})
    await service.tick(ctx)
    moved = await read(ctx, second['ask_id'])
    await actions.handle(ctx, await member(ctx), click(original))
    assert (await read(ctx, moved['id']))['state'] == 'open'
    assert (await read(ctx, original['id']))['state'] == 'cancelled'
    result = await replay(sender='U_ACME_BEN', text='Use this answer.',
                          payload={'channel_id': moved['slack_channel'], 'thread_ts': moved['slack_ts']})
    assert result['decision'] == 'claimed'
    assert (await read(ctx, moved['id']))['answer_text'] == 'Use this answer.'
    assert (await read(ctx, original['id']))['answer_text'] is None


@pytest.mark.parametrize('case', ['answered', 'old', 'other_token', 'other_owner'])
async def test_takeover_only_recent_cancel_of_same_requester_and_owner(ctx, app, http, slack_client, case):
    agent = await AgentClient.create(app, http)
    first = await agent.call('ask_create', ASK)
    await service.tick(ctx)
    row = await read(ctx, first['ask_id'])
    if case == 'answered':
        await actions.handle(ctx, await member(ctx), click(row))
    await agent.call('ask_cancel', {'ask_id': row['id']})
    async with ctx.pool.connection() as conn:
        if case == 'old':
            await conn.execute("UPDATE asks SET cancelled_at=now()-interval '11 minutes' WHERE id=%s", (row['id'],))
    if case == 'other_token':
        agent = await AgentClient.create(app, http)
    created = await agent.call('ask_create', {**ASK, 'owner': 'ada' if case == 'other_owner' else 'ben'})
    await service.tick(ctx)
    assert (await read(ctx, created['ask_id']))['slack_ts'] != row['slack_ts']
    assert (await read(ctx, row['id']))['slack_ts'] == row['slack_ts']
    assert len(posts(slack_client)) == 2
    if case == 'answered':
        assert (await agent.call('ask_pull'))['answers'][0]['ask_id'] == row['id']
        assert (await read(ctx, row['id']))['cancelled_at'] is None


async def test_open_ask_action_signin_and_participant_checks(ctx, browser, app, http, slack_client):
    agent = await AgentClient.create(app, http)
    ask = await agent.call('ask_create', ASK)
    for key, allowed in [('ada', True), ('ben', True), ('chloe', False), ('david', False)]:
        before = len(slack_client.calls)
        await ctx.slack_actions.dispatch(ctx, await member(ctx, key),
                                         {'action_id': f"web-open-ask:{ask['ask_id']}", 'trigger_id': 'T1'})
        views = [c for c in slack_client.calls[before:] if c['method'] == 'views_open']
        assert bool(views) is allowed
        if key == 'ben':
            text = views[0]['view']['blocks'][0]['text']['text']
            assert 'Open the page of request' in text
            path = link_of(text)
    assert signin.next_path('/web/a/17') == '/web/a/17'
    key = path.split('=', 1)[1]
    response = await browser.http.post('/web/signin', data={'key': key})
    assert response.status_code == 303 and response.headers['location'] == f"/web/a/{ask['ask_id']}"
    assert 'Waits for you' in (await browser.http.get(response.headers['location'])).text


@pytest.mark.parametrize('material', [
    {'text': 'x' * 20001}, {str(n): n for n in range(41)}, {'a': {'b': {'c': {'d': 1}}}},
    {'text': 'é' * 10000}, {'text': float('nan')},
])
def test_proof_limits_in_both_producers(material):
    for model, args in ((AskCreate, ASK), (CreateArgs, {'owner': 'ben', 'question': 'Read the plan?'})):
        with pytest.raises(ValidationError) as caught:
            model.model_validate({**args, 'proof': material})
        assert proof.PROOF_LIMIT in schema_reason(caught.value, model)


def test_proof_boundaries_and_safe_validator_messages():
    proof.check({str(n): n for n in range(40)})
    proof.check({'a': {'b': {'c': 'ok'}}})
    value = {'text': 'x' * (20000 - len(proof.compact({'text': ''}).encode()))}
    proof.check(value)
    with pytest.raises(ValidationError) as caught:
        AskCreate.model_validate({**ASK, 'question': 'x' * 241})
    assert REPAIR in schema_reason(caught.value, AskCreate)

    class InputError(Arguments):
        text: str

        @field_validator('text')
        @classmethod
        def unsafe(cls, value):
            raise ValueError('Cannot use ' + value)

    with pytest.raises(ValidationError) as caught:
        InputError.model_validate({'text': 'PRIVATE_INPUT'})
    reason = schema_reason(caught.value, InputError)
    assert 'The value is not valid' in reason and 'PRIVATE_INPUT' not in reason


async def test_action_always_and_standing_rule_refused_but_permission_kept(ctx, app, http):
    agent = await AgentClient.create(app, http)
    created = await agent.call('ask_create', ASK)
    await service.tick(ctx)
    row = await read(ctx, created['ask_id'])
    await actions.handle(ctx, await member(ctx), click(row, 'always:yes'))
    async with ctx.pool.connection() as conn:
        assert not await one(conn, 'SELECT id FROM rules')
        rule = await rules.create(conn, owner_person_id=row['owner_person_id'], text='Use the plan.',
                                  answer_option_label=ASK['options'][0]['label'])
        assert await rules.match_ask(conn, row) is None
        assert await asks.answer_with_rule(conn, row['id'], rule['id']) is None
        await conn.execute('UPDATE asks SET permission=true WHERE id=%s', (row['id'],))
    await actions.handle(ctx, await member(ctx), click(row, 'always:yes'))
    answered = await read(ctx, row['id'])
    assert answered['state'] == 'answered'
    async with ctx.pool.connection() as conn:
        assert await one(conn, "SELECT * FROM rules WHERE kind='always_allow'")


async def test_brain_and_collector_keep_short_proof(ctx, app, http, slack_client):
    ctx.workspace.config.pages.enabled = False
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, 'ada')
        brain = await asks.create(conn, owner_person_id=ada['id'], question='Review the plan?',
                                  proof={'text': 'The reason.\nThe second line.', 'extra': 'Hidden extra'})
        bearer = await tokens.create(conn, ada['id'], 'Example collector', scopes=['collector'])
    collector = AgentClient(app, http, bearer, 0, ada['id'])
    await collector.call('ask_create', {**ASK, 'owner': 'ada',
                                      'proof': {'text': 'The reason.\nThe second line.', 'extra': 'Hidden extra'}})
    await service.tick(ctx)
    assert len(posts(slack_client)) == 2
    for post in posts(slack_client):
        assert 'Why: The reason.' in str(post['blocks'])
        assert 'The second line.' not in str(post['blocks']) and 'Hidden extra' not in str(post['blocks'])
    assert posts(slack_client)[0]['text'] == '🔷 Illo needs a decision from you.\n' + brain['question']


@pytest.mark.parametrize('pages_on', [True, False])
async def test_saved_ref_inside_links_and_body(ctx, app, http, browser, slack_client, pages_on):
    ctx.workspace.config.pages.enabled = pages_on
    await saved(ctx)
    agent = await AgentClient.create(app, http)
    ask = await agent.call('ask_create', {**ASK, 'proof': {'body': 'saved:77', 'links': ['saved:77']}})
    if pages_on:
        await login(ctx, browser.http)
        page = await browser.http.get(f"/web/a/{ask['ask_id']}")
        assert page.text.count('The saved plan includes every step.') == 2
    else:
        await service.tick(ctx)
        assert str(posts(slack_client)[0]['blocks']).count('The saved plan includes every step.') == 2


async def test_inactive_person_and_team_lane_do_not_widen_request_page(ctx, app, http, browser):
    agent = await AgentClient.create(app, http)
    await agent.call('lane_report', {'source': 'manual', 'external_id': 'team-plan', 'visibility': 'team'})
    ask = await agent.call('ask_create', {**ASK, 'lane': {'source': 'manual', 'external_id': 'team-plan'}})
    await login(ctx, browser.http, 'david')
    assert (await browser.http.get(f"/web/a/{ask['ask_id']}")).status_code == 404
    await login(ctx, browser.http, 'ben')
    assert (await browser.http.get(f"/web/a/{ask['ask_id']}")).status_code == 200
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE people SET active=false WHERE key='ben'")
    assert (await browser.http.get(f"/web/a/{ask['ask_id']}")).status_code == 302


async def test_page_answer_with_option_rule_notice_and_closed_state(ctx, app, http, browser):
    agent = await AgentClient.create(app, http)
    ask = await agent.call('ask_create', {**ASK, 'kind': 'decision'})
    path = f"/web/a/{ask['ask_id']}"
    await login(ctx, browser.http)
    row = await read(ctx, ask['ask_id'])
    for key in ('question', 'state', 'options', 'answered_at'):
        value = 'changed' if key != 'options' else []
        assert ask_page.seen(row) != ask_page.seen({**row, key: value})
    async with ctx.pool.connection() as conn:
        rule = await rules.create(conn, owner_person_id=row['owner_person_id'], text='Use the reviewed plan.',
                                  answer_option_label=ASK['options'][0]['label'])
        assert await asks.answer_with_rule(conn, row['id'], rule['id'])
    shown = await browser.http.get(path)
    assert 'A rule of yours answered: Use the reviewed plan.' in shown.text
    assert 'class="decide"' not in shown.text
    another = await agent.call('ask_create', ASK)
    path = f"/web/a/{another['ask_id']}"
    form = fields(await browser.http.get(path))
    result = await browser.http.post(path + '/answer', data={**form, 'option': 'yes'})
    assert result.status_code == 303
    assert (await read(ctx, another['ask_id']))['answer_option'] == 'yes'
    await login(ctx, browser.http, 'ada')
    assert 'Ben answered on' in (await browser.http.get(path)).text


async def test_card_block_limit_rolls_back_new_lane(ctx, app, http):
    ctx.workspace.config.pages.enabled = False
    agent = await AgentClient.create(app, http)
    result = await agent.call('ask_create', {
        **ASK, 'owner': 'ada', 'proof': {f'entry_{n}': n for n in range(40)},
        'lane': {'source': 'manual', 'external_id': 'new-plan'}}, error=True)
    assert result['error']['message'] == proof.CARD_LIMIT
    async with ctx.pool.connection() as conn:
        assert not await one(conn, 'SELECT id FROM asks')
        assert not await one(conn, 'SELECT id FROM lanes')

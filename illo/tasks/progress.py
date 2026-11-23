"""Compute task progress from task facts and the existing lane liveness keys."""

import re
from datetime import UTC, datetime
from urllib.parse import unquote, urlsplit

from illo.db import all_rows, one

NOW = {
    'reads': 'reads the code', 'tests': 'runs the tests',
    'saves': 'saves the work to the branch', 'pull': 'opens the pull request',
    'command': 'runs a command', 'changes': 'changes files', 'thinks': 'thinks',
    'writes': 'writes', 'searches': 'searches the web', 'tool': 'uses a tool',
    'helper': 'works with a helper agent', 'waits': 'waits for an answer',
}
STAGES = [('waits', 'Waits for the agent'), ('works', 'Works'), ('pull', 'Pull request'),
          ('review', 'Review'), ('done', 'Done')]


def urls(links):
    return [link if isinstance(link, str) else link.get('url', '')
            for link in links if isinstance(link, (str, dict))]


def pull_link(url):
    return bool(re.fullmatch(r'https://github\.com/[^/]+/[^/]+/pull/\d+/?', url))


def stage(row, live, has_pull):
    if row['state'] == 'done':
        return 'done'
    if row['state'] == 'review':
        return 'review'
    if row['state'] in {'todo', 'working'}:
        if has_pull:
            return 'pull'
        if live:
            return 'works'
        if (row.get('holder') or '').startswith('agent:'):
            return 'waits'
    return None


async def linked_lanes(conn, row, viewer):
    """The caller first checks task access. Private lane text stays with its owner."""
    from illo.connectors.github_tasks import names_ticket

    task_urls = urls([link for link in row['links'] if link.get('kind') in {'ticket', 'change'}])
    prefix = f"task:lane-live:{row['id']}:"
    lanes = await all_rows(conn, """SELECT l.*,
        EXISTS (SELECT 1 FROM kv WHERE key=%s || l.id::text) AS live,
        EXISTS (SELECT 1 FROM kv WHERE key=%s || l.id::text) AS tied
        FROM lanes l WHERE (l.visibility='team' OR l.owner_person_id=(
            SELECT id FROM people WHERE key=%s AND active))
        AND (EXISTS (SELECT 1 FROM kv WHERE key=%s || l.id::text OR key=%s || l.id::text)
          OR EXISTS (SELECT 1 FROM jsonb_array_elements(l.links) v
            WHERE COALESCE(v->>'url',v#>>'{}')=ANY(%s))
          OR l.title ~ '#[0-9]+')
        ORDER BY l.last_signal_at DESC,l.id DESC""",
        (prefix, f"task:lane:{row['id']}:", viewer.person if not viewer.team_only else None,
         prefix, f"task:lane:{row['id']}:", task_urls))
    lanes = [lane for lane in lanes if lane['tied'] or lane['live']
             or set(urls(lane['links'])) & set(task_urls) or names_ticket(lane['title'], row)]
    return lanes


def current_lane(lanes):
    agents = [lane for lane in lanes if lane['source'] != 'github']
    return next((lane for lane in agents if lane['live']), next(iter(agents), None))


async def read(conn, row, viewer, *, lanes=None):
    lanes = await linked_lanes(conn, row, viewer) if lanes is None else lanes
    lane = current_lane(lanes)
    links = [*row['links'], *(link for lane in lanes for link in lane['links'])]
    has_pull = any(pull_link(url) for url in urls(links))
    # Release uses the existence of any key, even when no visible lane row exists.
    has_live = await one(conn, "SELECT key FROM kv WHERE key LIKE %s LIMIT 1",
                         (f"task:lane-live:{row['id']}:%",))
    result = {'stage': stage(row, bool(has_live), has_pull), 'at': None}
    if lane:
        value = lane.get('progress') or {}
        result.update({key: value[key] for key in ('steps', 'steps_total', 'steps_done', 'files_changed')
                       if value.get(key) is not None})
        if lane['live'] and value.get('now'):
            result['now'] = value['now']
        result['at'] = lane.get('progress_at')
    return result


async def facts(conn, row, viewer):
    """Keep page-only facts out of the agent result."""
    lanes = await linked_lanes(conn, row, viewer)
    value = await read(conn, row, viewer, lanes=lanes)
    lane = current_lane(lanes)
    links = [*row['links'], *(link for lane in lanes for link in lane['links'])]
    value.update(lane=lane, has_pull=any(pull_link(url) for url in urls(links)),
                 has_repo=any(link.get('kind') in {'repo', 'repository'} for link in row['links']),
                 branch=next((unquote(urlsplit(url).path.split('/tree/', 1)[1])
                              for url in urls(links) if '/tree/' in urlsplit(url).path), None))
    return value


def minutes(at, now=None):
    if isinstance(at, str):
        at = datetime.fromisoformat(at.replace('Z', '+00:00'))
    return max(0, int(((now or datetime.now(UTC)) - at).total_seconds() // 60)) if at else 0


def age(at):
    n = minutes(at)
    return ('a few seconds' if n == 0 else '1 minute' if n == 1 else f'{n} minutes' if n < 60
            else '1 hour' if n < 120 else f'{n // 60} hours')

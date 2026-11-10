"""Merge visible ledger text and live sources without copying source systems."""

import asyncio
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from illo.db import all_rows
from illo.ledger import items, lanes
from illo.sources import slack_index


@dataclass(frozen=True)
class Hit:
    source_ref: str
    kind: str
    source: str
    title: str
    snippet: str = ""
    link: str | None = None
    owner: str | None = None
    at: datetime | str | None = None
    score: float = 0
    personal: bool = False
    match_tier: Literal["strict", "fallback"] = "strict"


class Hits(list[Hit]):
    """Keep partial-source status and personal reads even when ranking drops a hit."""

    def __init__(self, values=(), *, failures=(), personal_read=False):
        super().__init__(values)
        self.failures = list(failures)
        self.partial = bool(self.failures)
        self.personal_read = personal_read


_providers: dict[str, tuple[Any, bool, bool]] = {}


def register_provider(name: str, fn, personal: bool = False, *, tainted: bool = True) -> None:
    """Providers receive (ctx, principal, query, kinds, limit) and return ordered Hits.

    Personal providers resolve credentials from the authenticated principal. They must
    not accept an owner from query text. Register each name once at setup.
    A team connector sets tainted=False unless its read tools explicitly allow tainted runs.
    """
    if name in _providers:
        raise ValueError(f"Duplicate search provider: {name}")
    _providers[name] = (fn, personal, tainted)


def first_link(links) -> str | None:
    for link in links or []:
        value = link.get("url") or link.get("href") if isinstance(link, dict) else link
        if isinstance(value, str) and value:
            return value
    return None


STRICT_QUERY = "websearch_to_tsquery('simple',%s) || websearch_to_tsquery('english',%s)"
# Any word may match. Postgres ranks an item higher when it has more of the words.
RELAXED_QUERY = " || ".join(
    "(SELECT coalesce(string_agg(quote_literal(lexeme), ' | '), '')::tsquery "
    f"FROM unnest(tsvector_to_array(to_tsvector('{config}',%s))) AS lexemes(lexeme))"
    for config in ("simple", "english")
)


async def _item_rows(conn, principal, query, kinds, limit, tsquery, weight):
    return await all_rows(
        conn,
        f"""
        WITH q AS (SELECT {tsquery} AS query)
        SELECT i.*,p.key AS owner,
          ts_headline('simple',concat_ws(' ',i.summary,i.body),q.query,
            'MaxWords=45,MinWords=15,StartSel="",StopSel=""') AS snippet,
          {weight} * ts_rank(i.tsv,q.query) * (1 + 0.05 / (1 + greatest(0,
            extract(epoch FROM (now()-coalesce(i.source_at,i.created_at)))/86400))) AS score
        FROM items i CROSS JOIN q LEFT JOIN people p ON p.id=i.owner_person_id
        WHERE i.tsv @@ q.query AND
          (i.visibility='team' OR i.owner_person_id=
            (SELECT id FROM people WHERE 'person:'||key=%s AND active))
          AND (%s::text[] IS NULL OR i.kind=ANY(%s))
        ORDER BY score DESC,coalesce(i.source_at,i.created_at) DESC,i.id DESC LIMIT %s
    """,
        (query, query, principal, kinds, kinds, limit),
    )


async def _items(conn, principal, query, kinds, limit):
    """All words first. If that finds too little, any word, ranked below the strict hits.

    A long question from a person or an agent must still find something.
    """
    rows = await _item_rows(conn, principal, query, kinds, limit, STRICT_QUERY, 1.0)
    for row in rows:
        row["match_tier"] = "strict"
    explicit_syntax = re.search(r'(?:^|\s)-\S|"|(?:^|\s)(?:OR|or)(?=\s|$)', query)
    if len(rows) < limit and not explicit_syntax:
        seen = {r["source_ref"] for r in rows}
        relaxed = await _item_rows(conn, principal, query, kinds, limit, RELAXED_QUERY, 0.5)
        for row in relaxed:
            row["match_tier"] = "fallback"
        rows += [r for r in relaxed if r["source_ref"] not in seen][: limit - len(rows)]
    return [
        Hit(
            r["source_ref"],
            r["kind"],
            r["source_ref"].split(":", 1)[0],
            r["title"],
            r["snippet"],
            first_link(r["links"]),
            r["owner"],
            r["source_at"] or r["created_at"],
            r["score"],
            r["visibility"] == "private",
            match_tier=r["match_tier"],
        )
        for r in rows
    ]


async def _lanes(conn, principal, query, kinds, limit):
    if kinds is not None and "lane" not in kinds:
        return []
    rows = await all_rows(
        conn,
        """
        WITH q AS (SELECT websearch_to_tsquery('simple',%s) ||
          websearch_to_tsquery('english',%s) AS query), texts AS (
          SELECT l.*,p.key AS owner,concat_ws(' ',l.summary,e.text) AS body,
            setweight(to_tsvector('simple',l.title)||to_tsvector('english',l.title),'A') ||
            setweight(to_tsvector('simple',concat_ws(' ',l.summary,e.text))||
              to_tsvector('english',concat_ws(' ',l.summary,e.text)),'C') AS vector
          FROM lanes l LEFT JOIN people p ON p.id=l.owner_person_id
          LEFT JOIN LATERAL (SELECT string_agg(recent.text,' ') AS text FROM
            (SELECT text FROM lane_events WHERE lane_id=l.id ORDER BY at DESC,id DESC LIMIT 20)
            recent) e ON true
          WHERE l.visibility='team' OR l.owner_person_id=
            (SELECT id FROM people WHERE 'person:'||key=%s AND active))
        SELECT t.*,ts_rank(t.vector,q.query) AS score,
          ts_headline('simple',t.body,q.query,
            'MaxWords=45,MinWords=15,StartSel="",StopSel=""') AS snippet
        FROM texts t,q WHERE t.vector @@ q.query ORDER BY score DESC,updated_at DESC,id DESC
        LIMIT %s
    """,
        (query, query, principal, limit),
    )
    return [
        Hit(
            f"lane:{r['id']}",
            "lane",
            r["source"],
            r["title"],
            f"State: {r['state']}. {r['snippet']}",
            first_link(r["links"]),
            r["owner"],
            r["updated_at"],
            r["score"],
            r["visibility"] == "private",
        )
        for r in rows
    ]


def github_ref(issue):
    url = issue.get("html_url", "")
    parts = urlsplit(url).path.strip("/").split("/")
    if len(parts) == 4 and parts[2] in {"issues", "pull"} and parts[3].isdigit():
        return f"github:{parts[0]}/{parts[1]}#{parts[3]}"
    return None


async def _github(ctx, query, kinds, limit):
    client = ctx.github_client
    repos = ctx.workspace.config.github.repositories
    if not client or not repos or len(re.sub(r"\W", "", query)) < 3:
        return []
    if kinds is not None and "github" not in kinds:
        return []
    scoped = f"({query}) " + " ".join(f"repo:{repo}" for repo in repos)
    rows = await client.search_issues(scoped)
    if isinstance(rows, dict):
        rows = rows.get("items", [])
    result = []
    for row in rows:
        ref = github_ref(row)
        if not ref or ref.split(":", 1)[1].split("#")[0].lower() not in {
            repo.lower() for repo in repos
        }:
            continue
        result.append(
            Hit(
                ref,
                "github",
                "github",
                row["title"],
                (row.get("body") or "")[:500],
                row.get("html_url"),
                at=row.get("updated_at") or row.get("created_at"),
            )
        )
    return result[:limit]


async def search(
    ctx, principal, query, kinds=None, limit=10, *, conn=None, tainted=False, personal_allowed=True
) -> list[Hit]:
    """Rank each source, then merge by reciprocal rank. Source failures stay partial."""
    limit = max(1, min(limit, 50))
    if not query.strip() or principal == "outsider":
        return Hits()
    if tainted:
        principal = "team"
    if conn is None:
        async with ctx.pool.connection() as connection:
            return await search(
                ctx,
                principal,
                query,
                kinds,
                limit,
                conn=connection,
                tainted=tainted,
                personal_allowed=personal_allowed,
            )
    groups = [
        await _items(conn, principal, query, kinds, limit),
        await _lanes(conn, principal, query, kinds, limit),
    ]
    failures = []
    if not tainted:
        try:
            async with conn.transaction():
                groups.append(await slack_index.search(ctx, conn, query, kinds, limit))
        except Exception:
            failures.append("slack_inbound")
    jobs = [("github", _github(ctx, query, kinds, limit), False)]
    for name, (fn, personal, allow_tainted) in tuple(_providers.items()):
        if tainted and not allow_tainted:
            continue
        if personal and (tainted or not principal.startswith("person:") or not personal_allowed):
            continue

        async def call(provider=fn):
            return await provider(ctx, principal, query, kinds, limit)

        jobs.append((name, call(), personal))

    async def bounded(job):
        async with asyncio.timeout(4):
            return await job

    answers = await asyncio.gather(*(bounded(job) for _, job, _ in jobs), return_exceptions=True)
    personal_read = any(hit.personal for group in groups for hit in group)
    for (name, _, personal), answer in zip(jobs, answers, strict=True):
        if isinstance(answer, BaseException):
            failures.append(name)
            continue
        group = [replace(hit, personal=personal or hit.personal) for hit in answer]
        personal_read |= any(hit.personal for hit in group)
        groups.append([hit for hit in group if kinds is None or hit.kind in kinds])
    merged = {}
    for group in groups:
        seen = set()
        ranks = {"strict": 0, "fallback": 0}
        # A duplicate's strict match takes precedence in every source.
        for hit in sorted(group, key=lambda h: h.match_tier != "strict"):
            if hit.source_ref in seen:
                continue
            seen.add(hit.source_ref)
            ranks[hit.match_tier] += 1
            score = 1 / (60 + ranks[hit.match_tier])
            previous, previous_score = merged.get(hit.source_ref, (hit, 0))
            personal = previous.personal or hit.personal
            if previous.match_tier == hit.match_tier:
                hit, score = previous, score + previous_score
            elif previous.match_tier == "strict":
                hit, score = previous, previous_score
            merged[hit.source_ref] = (replace(hit, personal=personal), score)
    ordered = sorted(
        merged.values(),
        key=lambda value: (value[0].match_tier != "strict", -value[1], value[0].source_ref),
    )
    return Hits(
        [replace(hit, score=score) for hit, score in ordered[:limit]],
        failures=failures,
        personal_read=personal_read,
    )


async def get(ctx, principal, source_ref, *, conn=None):
    """Resolve a stored ref before a live issue. Every ledger read checks visibility."""
    if principal == "outsider":
        return None
    if conn is None:
        async with ctx.pool.connection() as connection:
            return await get(ctx, principal, source_ref, conn=connection)
    if source_ref.startswith("slack-event:"):
        return await slack_index.get(ctx, conn, source_ref)
    item = await items.get(conn, source_ref, principal=principal)
    if item:
        return item
    if re.fullmatch(r"lane:[1-9][0-9]*", source_ref):
        lane = await lanes.get(
            conn,
            int(source_ref[5:]),
            principal=principal,
            stale_minutes=ctx.workspace.config.stale_minutes.model_dump(by_alias=True),
        )
        if lane:
            lane["source_ref"] = source_ref
            lane["events"] = await all_rows(
                conn,
                "SELECT at,kind,text,data FROM lane_events WHERE lane_id=%s ORDER BY at,id",
                (lane["id"],),
            )
        return lane
    match = re.fullmatch(r"github:([^/#]+/[^/#]+)#([1-9][0-9]*)", source_ref)
    if match and ctx.github_client:
        repo, number = match.groups()
        if repo.lower() not in {r.lower() for r in ctx.workspace.config.github.repositories}:
            return None
        try:
            async with asyncio.timeout(4):
                row = await ctx.github_client.get_issue(repo, int(number))
            return {**row, "source_ref": source_ref} if row else None
        except Exception:
            return {"source_ref": source_ref, "partial": True, "error": "GitHub is unavailable."}
    return None


async def mark_personal(ctx) -> None:
    """Mixed search tools set the same run flag as a personal connector tool."""
    await ctx.conn.execute("UPDATE runs SET personal_read=true WHERE id=%s", (ctx.run["id"],))
    ctx.run["personal_read"] = True


async def setup(ctx) -> None:
    """Providers register their search function in their own setup hook."""

"""Merge visible ledger text and live sources without copying source systems."""

import asyncio
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
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
    lexical: float | None = None
    recency: float | None = None
    rrf: float = 0


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


# Keep syntax in explicit queries. Plain queries also accept the parts of identifiers.
STRICT_QUERY = "illo_search_query('simple',%s) || illo_search_query('english',%s)"
RELAXED_QUERY = " || ".join(
    "(SELECT coalesce(string_agg(quote_literal(lexeme), ' | '), '')::tsquery "
    f"FROM unnest(tsvector_to_array(illo_search_tokens('{config}',%s))) AS lexemes(lexeme))"
    for config in ("simple", "english")
)
HALF_LIFE_DAYS = 21


def utc_now():
    return datetime.now(UTC)


def lexical_query(query):
    """Time words guide age ranking. Quoted text and search operators keep their meaning."""
    if explicit_query(query):
        return query
    return re.sub(r"(?<![\w.@+-])(?:today|yesterday|this\s+week)(?![\w.@+-])", " ", query, flags=re.I).strip()


def explicit_query(query):
    return bool(re.search(r'(?:^|\s)-\S|"|(?:^|\s)(?:OR|or)(?=\s|$)', query))


def query_sql(query):
    relaxed = "''::tsquery" if explicit_query(query) else RELAXED_QUERY
    params = (query, query) if explicit_query(query) else (query,) * 4
    return f"SELECT {STRICT_QUERY} AS strict, {relaxed} AS relaxed", params


# Rank all visible candidates before LIMIT, including partial matches. A strict match
# gets twice the weight. Age has a 21-day half-life and a 20% floor for old decisions.
# RRF adds only a small source-rank bonus later; it cannot erase match strength or age.
RANK_COLUMNS = f"""
    CASE WHEN vector @@ q.strict THEN 'strict' ELSE 'fallback' END AS match_tier,
    ts_rank(ARRAY[0.1,0.5,0.75,1.0]::real[], vector,
      CASE WHEN vector @@ q.strict THEN q.strict ELSE q.relaxed END,32) AS lexical,
    0.2 + 0.8 * power(0.5, floor(greatest(0,extract(epoch FROM (%s::timestamptz-at))) /
      86400) / {HALF_LIFE_DAYS}) AS recency
"""
RANK_ORDER = "lexical * recency * CASE match_tier WHEN 'strict' THEN 2 ELSE 1 END"


async def _items(conn, principal, query, kinds, limit, now, slack_channels):
    q, params = query_sql(query)
    rows = await all_rows(conn, f"""
        WITH q AS ({q}), texts AS (
          SELECT i.*,p.key AS owner,coalesce(i.source_at,i.created_at) AS at,
            illo_search_vector(i.title,i.summary,i.body) AS vector
          FROM items i LEFT JOIN people p ON p.id=i.owner_person_id
          WHERE (i.visibility='team' OR i.owner_person_id=
            (SELECT id FROM people WHERE 'person:'||key=%s AND active))
            AND (%s::text[] IS NULL OR i.kind=ANY(%s))
            AND (i.kind<>'slack' OR split_part(i.source_ref,':',2)=ANY(%s))
        ), ranked AS (
          SELECT t.*, {RANK_COLUMNS},
            ts_headline('simple',concat_ws(' ',t.summary,t.body),
              CASE WHEN vector @@ q.strict THEN q.strict ELSE q.relaxed END,
              'MaxWords=45,MinWords=15,StartSel="",StopSel=""') AS snippet
          FROM texts t,q WHERE vector @@ q.strict OR vector @@ q.relaxed
        ) SELECT * FROM ranked ORDER BY {RANK_ORDER} DESC,at DESC,id DESC LIMIT %s
    """, (*params, principal, kinds, kinds, list(slack_channels), now, limit))
    return [Hit(
        r["source_ref"], r["kind"], r["source_ref"].split(":", 1)[0], r["title"],
        r["snippet"], first_link(r["links"]), r["owner"], r["at"],
        personal=r["visibility"] == "private", match_tier=r["match_tier"],
        lexical=float(r["lexical"]), recency=float(r["recency"]),
    ) for r in rows]


async def _lanes(conn, principal, query, kinds, limit, now):
    if kinds is not None and "lane" not in kinds:
        return []
    q, params = query_sql(query)
    rows = await all_rows(conn, f"""
        WITH q AS ({q}), texts AS (
          SELECT l.*,p.key AS owner,concat_ws(' ',l.summary,e.text) AS body,
            l.updated_at AS at,
            illo_search_vector(l.title,'',concat_ws(' ',l.summary,e.text)) AS vector
          FROM lanes l LEFT JOIN people p ON p.id=l.owner_person_id
          LEFT JOIN LATERAL (SELECT string_agg(recent.text,' ') AS text FROM
            (SELECT text FROM lane_events WHERE lane_id=l.id ORDER BY at DESC,id DESC LIMIT 20)
            recent) e ON true
          WHERE l.visibility='team' OR l.owner_person_id=
            (SELECT id FROM people WHERE 'person:'||key=%s AND active)
        ), ranked AS (
          SELECT t.*, {RANK_COLUMNS},
            ts_headline('simple',t.body,
              CASE WHEN vector @@ q.strict THEN q.strict ELSE q.relaxed END,
              'MaxWords=45,MinWords=15,StartSel="",StopSel=""') AS snippet
          FROM texts t,q WHERE vector @@ q.strict OR vector @@ q.relaxed
        ) SELECT * FROM ranked ORDER BY {RANK_ORDER} DESC,at DESC,id DESC LIMIT %s
    """, (*params, principal, now, limit))
    return [Hit(
        f"lane:{r['id']}", "lane", r["source"], r["title"],
        f"State: {r['state']}. {r['snippet']}", first_link(r["links"]), r["owner"], r["at"],
        personal=r["visibility"] == "private", match_tier=r["match_tier"],
        lexical=float(r["lexical"]), recency=float(r["recency"]),
    ) for r in rows]


async def _provider_scores(conn, group, query, now):
    """Use the same field weights for live source text. Never trust a provider's score scale."""
    pending = [hit for hit in group if hit.lexical is None]
    if not pending:
        return group
    q, params = query_sql(query)
    rows = await all_rows(conn, f"""
        WITH q AS ({q}), texts AS (
          SELECT title,body,illo_search_vector(title,'',body) AS vector
          FROM unnest(%s::text[],%s::text[]) AS t(title,body)
        ) SELECT ts_rank(ARRAY[0.1,0.5,0.75,1.0]::real[],vector,
          CASE WHEN vector @@ q.strict THEN q.strict ELSE q.relaxed END,32) AS lexical
        FROM texts,q
    """, (*params, [h.title for h in pending], [h.snippet for h in pending]))
    scores = iter(rows)
    result = []
    for hit in group:
        if hit.lexical is not None:
            result.append(hit)
            continue
        at = hit.at
        if isinstance(at, str):
            try:
                at = datetime.fromisoformat(at.replace("Z", "+00:00"))
            except ValueError:
                at = None
        if at is not None and at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        age_days = max(0, (now-at).total_seconds()//86400) if at else None
        recency = 0.2 + 0.8 * 0.5 ** (age_days/HALF_LIFE_DAYS) if age_days is not None else 0.2
        result.append(replace(hit, lexical=float(next(scores)["lexical"]), recency=recency))
    return result


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
    """Merge match strength, age, and source rank. Source failures stay partial."""
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
    query = lexical_query(query)
    if not query:
        return Hits()
    now = utc_now()
    # A short page must not reduce the evidence available from any source.
    candidate_limit = 50
    failures = []
    try:
        allowed_slack = await slack_index.channels(ctx, conn)
    except Exception:
        allowed_slack = {}
        failures.append("slack_inbound")
    groups = [
        await _items(conn, principal, query, kinds, candidate_limit, now, allowed_slack),
        await _lanes(conn, principal, query, kinds, candidate_limit, now),
    ]
    if not tainted:
        try:
            async with conn.transaction():
                groups.append(await slack_index.search(ctx, conn, query, kinds, candidate_limit, now))
        except Exception:
            failures.append("slack_inbound")
    jobs = [("github", _github(ctx, query, kinds, candidate_limit), False)]
    for name, (fn, personal, allow_tainted) in tuple(_providers.items()):
        if tainted and not allow_tainted:
            continue
        if personal and (tainted or not principal.startswith("person:") or not personal_allowed):
            continue

        async def call(provider=fn):
            return await provider(ctx, principal, query, kinds, candidate_limit)

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
        group = await _provider_scores(conn, group, query, now)
        unique = {}
        for hit in group:
            previous = unique.get(hit.source_ref)
            if previous:
                best = max((previous, hit), key=lambda h: (_strength(h), h.match_tier == "strict"))
                hit = replace(best, personal=previous.personal or hit.personal)
            unique[hit.source_ref] = hit
        for rank, hit in enumerate(unique.values(), 1):
            rrf = 1 / (60 + rank)
            previous = merged.get(hit.source_ref)
            if previous:
                # A duplicate keeps its strongest evidence and all privacy flags.
                best = max((previous, hit), key=lambda h: (_strength(h), h.match_tier == "strict"))
                hit = replace(best, personal=previous.personal or hit.personal,
                              rrf=previous.rrf + rrf)
            else:
                hit = replace(hit, rrf=rrf)
            merged[hit.source_ref] = hit
    ordered = sorted(merged.values(), key=lambda h: (-(_strength(h) + h.rrf), h.source_ref))
    return Hits(
        [replace(hit, score=_strength(hit) + hit.rrf) for hit in ordered[:limit]],
        failures=failures,
        personal_read=personal_read,
    )


def _strength(hit):
    return (hit.lexical or 0) * (hit.recency or 0.2) * (2 if hit.match_tier == "strict" else 1)


async def get(ctx, principal, source_ref, *, conn=None):
    """Resolve a stored ref before a live issue. Every ledger read checks visibility."""
    if principal == "outsider":
        return None
    if conn is None:
        async with ctx.pool.connection() as connection:
            return await get(ctx, principal, source_ref, conn=connection)
    if source_ref.startswith("slack-event:"):
        return await slack_index.get(ctx, conn, source_ref)
    if source_ref.startswith("slack:"):
        channel_id = source_ref.split(":", 2)[1]
        channel = ctx.workspace.config.slack.channel(channel_id)
        if channel.mode == "off":
            return None
        from illo.channels.slack.access import channel_private

        try:
            if await channel_private(ctx, channel_id, conn=conn):
                return None
        except ValueError:
            return None
    item = await items.get(conn, source_ref, principal=principal)
    if item:
        if item["kind"] == "slack" and source_ref != item["source_ref"]:
            return await get(ctx, principal, item["source_ref"], conn=conn)
        if item["kind"] == "slack" and not source_ref.startswith("slack:"):
            return None
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

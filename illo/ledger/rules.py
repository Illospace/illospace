"""Keep standing answers visible and owned by a person."""

from fnmatch import fnmatchcase

from psycopg.types.json import Jsonb

from illo.db import Connection, all_rows, one


async def create(
    conn: Connection,
    *,
    owner_person_id: int,
    text: str,
    kind: str = "standing_answer",
    scope: str = "person",
    match: dict | None = None,
    answer_option_label: str | None = None,
    origin: str = "card",
) -> dict:
    """Store a rule. A question pattern uses shell-style wildcards, not regex."""
    return await one(
        conn,
        """
        INSERT INTO rules (owner_person_id,text,kind,scope,match,answer_option_label,origin)
        VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *
    """,
        (owner_person_id, text, kind, scope, Jsonb(match or {}), answer_option_label, origin),
    )


async def list_rules(conn: Connection, *, principal: str = "team") -> list[dict]:
    """Show team rules and the principal's own rules."""
    return await all_rows(
        conn,
        """
        SELECT * FROM rules WHERE removed_at IS NULL AND (scope = 'team' OR owner_person_id =
          (SELECT id FROM people WHERE 'person:' || key = %s AND active)) ORDER BY id
    """,
        (principal,),
    )


def matches(rule: dict, ask: dict, lane: dict | None = None) -> bool:
    """A rule can answer only its owner's ask. Unknown match fields fail closed."""
    if rule["removed_at"] or rule["owner_person_id"] != ask["owner_person_id"]:
        return False
    lane = lane or {}
    fields = {
        "ask_kind": ask["kind"],
        "lane_source": lane.get("source"),
        "repository": lane.get("repo"),
    }
    for key, value in rule["match"].items():
        if key == "question_pattern":
            if not isinstance(value, str) or not fnmatchcase(
                ask["question"].casefold(), value.casefold()
            ):
                return False
        elif key not in fields or fields[key] != value:
            return False
    return True


async def match_ask(conn: Connection, ask: dict) -> dict | None:
    """Find the first active rule that matches an option proposed by the agent."""
    lane = (
        await one(conn, "SELECT * FROM lanes WHERE id = %s", (ask["lane_id"],))
        if ask.get("lane_id")
        else None
    )
    rows = await all_rows(
        conn,
        "SELECT * FROM rules WHERE owner_person_id=%s AND removed_at IS NULL ORDER BY id",
        (ask["owner_person_id"],),
    )
    labels = {o["label"] for o in ask["options"]}
    return next(
        (r for r in rows if matches(r, ask, lane) and r["answer_option_label"] in labels), None
    )


async def count_use(conn: Connection, rule_id: int) -> None:
    """Count a use in the same transaction as its answer."""
    await conn.execute(
        "UPDATE rules SET uses=uses+1 WHERE id=%s AND removed_at IS NULL", (rule_id,)
    )


async def remove(conn: Connection, rule_id: int, owner_person_id: int) -> bool:
    """Only the owner can remove a rule."""
    return bool(
        await one(
            conn,
            """
        UPDATE rules SET removed_at=now() WHERE id=%s AND owner_person_id=%s AND removed_at IS NULL RETURNING id
    """,
            (rule_id, owner_person_id),
        )
    )

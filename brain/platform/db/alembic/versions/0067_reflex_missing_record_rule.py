"""Record open GitHub items whose latest webhook hides their opened event.

Revision ID: 0067_reflex_missing_record_rule
Revises: 0066_provider_alert_filing_claims
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Mapping

from alembic import op
import sqlalchemy as sa


revision = "0067_reflex_missing_record_rule"
down_revision = "0066_provider_alert_filing_claims"
branch_labels = None
depends_on = None

_RULE_MARKER = "Missing-record rule:"
_RULE = (
    "Missing-record rule: Domain 38 keeps only the latest webhook action for each "
    "issue or pull request, so a later bot comment, label or edit can hide the "
    "`opened` event. Before you classify a changed Domain 38 record as routine "
    "noise, look up its external id in Domain 1. If the item's `state` is `open` "
    "and Domain 1 has no record for it, treat it as tracker-worthy motion (issue "
    "opened or PR opened) and create the record, whatever the latest `event`, "
    "`action` or `author` is. Bot exception: when `event` is `issues` or "
    "`pull_request`, `author` is the account that opened the item; if that account "
    "is a bot, ignore the item as before. When `event` is `issue_comment`, `author` "
    "is only the commenter and says nothing about who opened the item, so create "
    "the record."
)
_REVISION_RATIONALE = (
    "Record an open issue or PR that has no tracker record, even when a bot "
    "comment or label is its latest webhook action (#920)."
)


def _schema(bind: sa.Connection) -> str | None:
    return "public" if bind.dialect.name == "postgresql" else None


def _table_exists(bind: sa.Connection, table_name: str) -> bool:
    return sa.inspect(bind).has_table(table_name, schema=_schema(bind))


def _table(bind: sa.Connection, metadata: sa.MetaData, table_name: str) -> sa.Table:
    return sa.Table(table_name, metadata, schema=_schema(bind), autoload_with=bind)


def _record_cycle_revision(
    bind: sa.Connection,
    revisions: sa.Table,
    cycle: Mapping[str, object],
    prompt: str,
) -> None:
    latest = bind.execute(
        sa.select(revisions)
        .where(revisions.c.cycle_id == cycle["id"])
        .order_by(revisions.c.revision_number.desc(), revisions.c.id.desc())
        .limit(1)
    ).mappings().first()
    if latest is not None and latest["prompt"] == prompt:
        return

    values = {
        "cycle_id": cycle["id"],
        "revision_number": int(latest["revision_number"]) + 1 if latest else 1,
        "source_type": "system",
        "source_id": None,
        "rationale": _REVISION_RATIONALE,
        "prompt": prompt,
        "context_policy": (latest.get("context_policy") or {}) if latest else {},
    }
    # Migration 0039 retired revision model pins; never copy model_override.
    for column in (
        "name",
        "schedule_expr",
        "timezone",
        "enabled",
        "thinking_override",
        "execution_policy_key",
        "target_idea_id",
    ):
        if column in cycle:
            values[column] = cycle[column]
    bind.execute(
        revisions.insert().values(
            **{column: value for column, value in values.items() if column in revisions.c}
        )
    )


def _upgrade(bind: sa.Connection) -> None:
    if not _table_exists(bind, "cycles"):
        return

    metadata = sa.MetaData()
    cycles = _table(bind, metadata, "cycles")
    cycle = bind.execute(
        sa.select(cycles).where(
            cycles.c.id == 8,
            cycles.c.name.startswith("GitHub Reflex"),
            cycles.c.prompt.contains("Domain id `38`"),
        )
    ).mappings().first()
    if cycle is None or _RULE_MARKER in cycle["prompt"]:
        return

    prompt = f"{cycle['prompt'].rstrip()}\n\n{_RULE}"
    update_values: dict[str, object] = {"prompt": prompt}
    if "updated_at" in cycles.c:
        update_values["updated_at"] = sa.func.now()
    bind.execute(cycles.update().where(cycles.c.id == cycle["id"]).values(**update_values))
    if _table_exists(bind, "cycle_revisions"):
        _record_cycle_revision(bind, _table(bind, metadata, "cycle_revisions"), cycle, prompt)


def upgrade() -> None:
    _upgrade(op.get_bind())


def downgrade() -> None:
    # Cycle revisions are an append-only audit trail.
    return None

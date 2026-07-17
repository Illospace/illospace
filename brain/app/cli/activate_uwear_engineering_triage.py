"""Activate the live Uwear engineering triage documents by stable slug.

This command intentionally has no implicit write mode. Use ``--apply`` to
perform the transactional activation and ``--check`` for read-only verification.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from brain.kernel import config


DOMAIN_ID = 37
CORE_RECORD_ID = 1155
CORE_SLUG = "uwear-engineering-triage"
CORE_REQUIRED_CURRENT_VERSION = 7
CORE_ACTIVATED_MINIMUM_VERSION = 8
BUNDLE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "systems"
    / "skills"
    / "builtin_skill_bundles"
    / "uwear-engineering-triage"
)


class ActivationError(RuntimeError):
    """The live records cannot be activated without ambiguity or data loss."""


@dataclass(frozen=True)
class DocumentSpec:
    slug: str
    title: str
    relative_path: str


@dataclass(frozen=True)
class ActivationResult:
    created: tuple[str, ...]
    updated: tuple[str, ...]
    unchanged: tuple[str, ...]


PLAYBOOKS = (
    DocumentSpec(
        "uwear-engineering-triage-customer-support",
        "Uwear Engineering Triage — Direct Customer Support",
        "references/customer-support.md",
    ),
    DocumentSpec(
        "uwear-engineering-triage-creating-work-items",
        "Uwear Engineering Triage — Creating Work Items",
        "references/creating-work-items.md",
    ),
    DocumentSpec(
        "uwear-engineering-triage-backlog-maintenance",
        "Uwear Engineering Triage — Backlog Maintenance",
        "references/backlog-maintenance.md",
    ),
    DocumentSpec(
        "uwear-engineering-triage-chantier-operations",
        "Uwear Engineering Triage — Chantier Operations",
        "references/chantier-operations.md",
    ),
    DocumentSpec(
        "uwear-engineering-triage-memory",
        "Uwear Engineering Triage — Memory Playbook",
        "references/memory.md",
    ),
)
PLAYBOOK_SLUGS = tuple(spec.slug for spec in PLAYBOOKS)
_ALL_SLUGS = (CORE_SLUG, *PLAYBOOK_SLUGS)


def _table(bind: sa.Connection, metadata: sa.MetaData, name: str) -> sa.Table:
    try:
        return sa.Table(name, metadata, autoload_with=bind)
    except sa.exc.NoSuchTableError as exc:
        raise ActivationError(f"Required table {name} is missing") from exc


def _record_slug(row: sa.RowMapping) -> str:
    data = row.get("data")
    return str(data.get("slug") or "") if isinstance(data, dict) else ""


def _is_archived(row: sa.RowMapping) -> bool:
    return "archived_at" in row and row["archived_at"] is not None


def _content(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ActivationError(f"Cannot read bundled activation asset {path}: {exc}") from exc


def _search_text(title: str, slug: str) -> str:
    return f"{title} {slug}".lower()


def _update_values(
    table: sa.Table,
    *,
    data: dict[str, Any],
    title: str | None,
    version: int,
    search_text: str | None,
) -> dict[str, Any]:
    values: dict[str, Any] = {"data": data, "version": version}
    if title is not None:
        values["title"] = title
    if search_text is not None and "search_text" in table.c:
        values["search_text"] = search_text
    if "updated_at" in table.c:
        values["updated_at"] = sa.func.now()
    return values


def _assert_single_target(
    rows: list[sa.RowMapping],
    *,
    slug: str,
    object_type_id: int,
    org_id: object,
) -> sa.RowMapping | None:
    matches = [row for row in rows if _record_slug(row) == slug]
    if len(matches) > 1:
        locations = ", ".join(
            f"id {row['id']} in Domain {row['domain_id']}" for row in matches
        )
        raise ActivationError(f"Target slug {slug} resolves to multiple records: {locations}")
    if not matches:
        return None

    row = matches[0]
    if int(row["domain_id"]) != DOMAIN_ID:
        raise ActivationError(
            f"Target slug {slug} is occupied by record {row['id']} in Domain "
            f"{row['domain_id']}"
        )
    if int(row["object_type_id"]) != object_type_id:
        raise ActivationError(
            f"Target slug {slug} is occupied by Domain 37 record {row['id']} "
            "with an object type other than doc_page"
        )
    if str(row["org_id"]) != str(org_id):
        raise ActivationError(
            f"Target slug {slug} belongs to org {row['org_id']}, expected {org_id}"
        )
    if _is_archived(row):
        raise ActivationError(f"Target slug {slug} is occupied by archived record {row['id']}")
    return row


def _verify_document(row: sa.RowMapping, spec: DocumentSpec, expected: str) -> None:
    data = row.get("data")
    if not isinstance(data, dict):
        raise ActivationError(f"Record {row['id']} for slug {spec.slug} has invalid data")
    if (
        row["title"] != spec.title
        or data.get("slug") != spec.slug
        or data.get("title") != spec.title
        or data.get("content") != expected
    ):
        raise ActivationError(
            f"Record {row['id']} for slug {spec.slug} failed byte-identity verification"
        )


def _activate(
    bind: sa.Connection,
    *,
    apply: bool,
    bundle_root: Path = BUNDLE_ROOT,
) -> ActivationResult:
    metadata = sa.MetaData()
    records = _table(bind, metadata, "domain_records")
    object_types = _table(bind, metadata, "domain_object_types")

    if apply and bind.dialect.name == "postgresql":
        # Slugs are JSON fields rather than a unique indexed column. This lock
        # closes the check/insert race for the short activation transaction.
        bind.execute(sa.text("LOCK TABLE domain_records IN SHARE ROW EXCLUSIVE MODE"))

    object_type_stmt = sa.select(object_types).where(
        object_types.c.domain_id == DOMAIN_ID,
        object_types.c.key == "doc_page",
    )
    if "archived_at" in object_types.c:
        object_type_stmt = object_type_stmt.where(object_types.c.archived_at.is_(None))
    if apply:
        object_type_stmt = object_type_stmt.with_for_update()
    doc_types = bind.execute(object_type_stmt).mappings().all()
    if len(doc_types) != 1:
        raise ActivationError(
            f"Expected exactly one active Domain 37 doc_page object type, found {len(doc_types)}"
        )
    object_type_id = int(doc_types[0]["id"])

    slug_expression = records.c.data["slug"].as_string()
    target_stmt = sa.select(records).where(
        sa.or_(
            records.c.id == CORE_RECORD_ID,
            slug_expression.in_(_ALL_SLUGS),
        )
    )
    if apply:
        target_stmt = target_stmt.with_for_update()
    target_rows = bind.execute(target_stmt).mappings().all()

    core_rows = [row for row in target_rows if int(row["id"]) == CORE_RECORD_ID]
    if not core_rows:
        raise ActivationError("Core target record 1155 is missing")
    core = core_rows[0]
    if int(core["domain_id"]) != DOMAIN_ID:
        raise ActivationError(
            f"Core target id 1155 is occupied by a record from Domain {core['domain_id']}"
        )
    if int(core["object_type_id"]) != object_type_id:
        raise ActivationError("Core target record 1155 is not a Domain 37 doc_page")
    if _is_archived(core):
        raise ActivationError("Core target record 1155 is archived")
    if _record_slug(core) != CORE_SLUG:
        raise ActivationError(
            f"Core target record 1155 has slug {_record_slug(core)!r}, expected {CORE_SLUG!r}"
        )
    duplicate_core = [
        row
        for row in target_rows
        if _record_slug(row) == CORE_SLUG and int(row["id"]) != CORE_RECORD_ID
    ]
    if duplicate_core:
        raise ActivationError(
            f"Core slug {CORE_SLUG} is duplicated by record {duplicate_core[0]['id']}"
        )

    # Preserve the reflected driver's native UUID representation for inserts.
    org_id = core["org_id"]
    expected_by_slug = {
        spec.slug: _content(bundle_root / spec.relative_path) for spec in PLAYBOOKS
    }
    core_expected = _content(bundle_root / "SKILL.md")
    existing = {
        spec.slug: _assert_single_target(
            target_rows,
            slug=spec.slug,
            object_type_id=object_type_id,
            org_id=org_id,
        )
        for spec in PLAYBOOKS
    }

    changes_needed = []
    for spec in PLAYBOOKS:
        row = existing[spec.slug]
        if row is None:
            changes_needed.append(f"create {spec.slug}")
            continue
        data = row.get("data")
        if (
            row["title"] != spec.title
            or not isinstance(data, dict)
            or data.get("title") != spec.title
            or data.get("content") != expected_by_slug[spec.slug]
        ):
            changes_needed.append(f"update {spec.slug}")

    core_data = core.get("data")
    core_content_matches = (
        isinstance(core_data, dict) and core_data.get("content") == core_expected
    )
    core_version = int(core["version"])
    if not core_content_matches or core_version < CORE_ACTIVATED_MINIMUM_VERSION:
        changes_needed.append(f"update {CORE_SLUG}")

    if not core_content_matches and core_version != CORE_REQUIRED_CURRENT_VERSION:
        raise ActivationError(
            "Core record 1155 content differs from the bundle at version "
            f"{core_version}; expected v7 before activation. Reconcile the newer edit first."
        )
    if core_content_matches and core_version < CORE_REQUIRED_CURRENT_VERSION:
        raise ActivationError(
            f"Core record 1155 is unexpectedly version {core_version}; expected v7 or newer."
        )
    if changes_needed and not apply:
        raise ActivationError(
            "Activation is not current: " + ", ".join(changes_needed) + ". Run with --apply."
        )

    created: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    ids_by_slug: dict[str, int] = {}

    for spec in PLAYBOOKS:
        expected = expected_by_slug[spec.slug]
        row = existing[spec.slug]
        if row is None:
            data = {"slug": spec.slug, "title": spec.title, "content": expected}
            values: dict[str, Any] = {
                "org_id": org_id,
                "domain_id": DOMAIN_ID,
                "object_type_id": object_type_id,
                "title": spec.title,
                "data": data,
                "version": 1,
            }
            if "search_text" in records.c:
                values["search_text"] = _search_text(spec.title, spec.slug)
            result = bind.execute(records.insert().values(**values))
            ids_by_slug[spec.slug] = int(result.inserted_primary_key[0])
            created.append(spec.slug)
            continue

        ids_by_slug[spec.slug] = int(row["id"])
        data = dict(row["data"] or {})
        if (
            row["title"] == spec.title
            and data.get("title") == spec.title
            and data.get("content") == expected
        ):
            unchanged.append(spec.slug)
            continue
        data.update({"slug": spec.slug, "title": spec.title, "content": expected})
        expected_version = int(row["version"])
        result = bind.execute(
            records.update()
            .where(
                records.c.id == row["id"],
                records.c.version == expected_version,
            )
            .values(
                **_update_values(
                    records,
                    data=data,
                    title=spec.title,
                    version=expected_version + 1,
                    search_text=_search_text(spec.title, spec.slug),
                )
            )
        )
        if result.rowcount != 1:
            raise ActivationError(f"Concurrent update detected for slug {spec.slug}")
        updated.append(spec.slug)

    if not core_content_matches or core_version < CORE_ACTIVATED_MINIMUM_VERSION:
        next_data = dict(core_data or {})
        next_data["content"] = core_expected
        result = bind.execute(
            records.update()
            .where(
                records.c.id == CORE_RECORD_ID,
                records.c.version == core_version,
            )
            .values(
                **_update_values(
                    records,
                    data=next_data,
                    title=None,
                    version=core_version + 1,
                    search_text=None,
                )
            )
        )
        if result.rowcount != 1:
            raise ActivationError("Concurrent update detected for core record 1155")
        updated.append(CORE_SLUG)
    else:
        unchanged.append(CORE_SLUG)

    verify_ids = [CORE_RECORD_ID, *ids_by_slug.values()]
    verified_rows = {
        int(row["id"]): row
        for row in bind.execute(
            sa.select(records).where(records.c.id.in_(verify_ids))
        ).mappings()
    }
    for spec in PLAYBOOKS:
        _verify_document(verified_rows[ids_by_slug[spec.slug]], spec, expected_by_slug[spec.slug])
    verified_core = verified_rows[CORE_RECORD_ID]
    if (
        _record_slug(verified_core) != CORE_SLUG
        or verified_core["data"].get("content") != core_expected
        or int(verified_core["version"]) < CORE_ACTIVATED_MINIMUM_VERSION
    ):
        raise ActivationError("Core record 1155 failed post-write v8 byte verification")

    return ActivationResult(tuple(created), tuple(updated), tuple(unchanged))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true", help="Apply the activation transaction")
    mode.add_argument("--check", action="store_true", help="Verify without writing")
    return parser


async def _run(*, apply: bool) -> ActivationResult:
    engine = create_async_engine(config.DB_URL)
    try:
        if apply:
            async with engine.begin() as connection:
                return await connection.run_sync(lambda bind: _activate(bind, apply=True))
        async with engine.connect() as connection:
            return await connection.run_sync(lambda bind: _activate(bind, apply=False))
    finally:
        await engine.dispose()


def main() -> int:
    args = _parser().parse_args()
    try:
        result = asyncio.run(_run(apply=args.apply))
    except ActivationError as exc:
        raise SystemExit(f"Activation failed: {exc}") from exc

    print(
        "Activation verified: "
        f"created={len(result.created)} updated={len(result.updated)} "
        f"unchanged={len(result.unchanged)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

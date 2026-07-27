"""The migration graph must converge on exactly one head.

Two branches that chain off the same revision each pass CI on their own and
merge cleanly, then break `alembic upgrade head` the moment both are on main.
It has happened twice (0036, then 0042). This test is the guard.
"""

from __future__ import annotations

from brain.app.ops.health import _alembic_head_revisions


def test_migration_graph_has_a_single_head() -> None:
    heads = _alembic_head_revisions()
    assert len(heads) == 1, (
        "Alembic has diverged into multiple heads: "
        f"{sorted(heads)}. Two migrations share a down_revision. Re-chain the "
        "newer one onto the other (fix BOTH the `Revises:` docstring line and "
        "`down_revision`), so `alembic upgrade head` stays unambiguous."
    )

import brain.platform.db.models  # noqa: F401
from brain.platform.db.base import Base


def test_every_registered_table_has_a_primary_key():
    tables_without_primary_key = sorted(
        table.name
        for table in Base.metadata.sorted_tables
        if not list(table.primary_key.columns)
    )

    assert tables_without_primary_key == []


def test_org_scoped_tables_index_their_tenant_column():
    missing_indexes = []
    for table in Base.metadata.sorted_tables:
        org_id = table.columns.get("org_id")
        if org_id is None:
            continue
        indexed_columns = {
            column.name
            for index in table.indexes
            for column in index.columns
        }
        if not org_id.index and "org_id" not in indexed_columns:
            missing_indexes.append(table.name)

    assert missing_indexes == []

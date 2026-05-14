from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_metrics_module():
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / "async_db_debt_metrics.py"
    spec = importlib.util.spec_from_file_location("async_db_debt_metrics", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_metric_counts_ast_db_shapes_not_string_mentions(tmp_path):
    metrics = _load_metrics_module()
    path = tmp_path / "sample.py"
    path.write_text(
        '''
# open_unit_of_work in a comment is documentation, not a code reference.
TEXT = "run_unit_of_work_task, run_async_from_sync, asyncio.run, and DB_SYNC_URL in a string"
import asyncio
from concurrent.futures import ThreadPoolExecutor
from brain.platform.async_bridge import run_async_from_sync
from brain.platform.db.repositories.unit_of_work import UnitOfWork, open_unit_of_work

def load():
    with open_unit_of_work(UnitOfWork):
        pass

def sync_bridge(awaitable):
    return run_async_from_sync(awaitable)

def cli_boundary(awaitable):
    return asyncio.run(awaitable)

def thread_bridge():
    return ThreadPoolExecutor(max_workers=1)

async def inspect(session):
    await session.run_sync(lambda sync_session: sync_session)
''',
        encoding="utf-8",
    )

    matches = list(metrics.ast_matches(path, "brain/example.py", "systems", metrics.async_function_lines(path)))
    categories = [match.category for match in matches]

    assert categories.count("open_unit_of_work") == 2
    assert categories.count("run_async_from_sync") == 2
    assert "asyncio_run_bridge" not in categories
    assert "thread_pool_bridge" not in categories
    assert categories.count("session_run_sync") == 1
    assert "run_unit_of_work_task" not in categories
    assert "sync_db_url" not in categories
    assert sum(match.in_async_function for match in matches if match.category == "session_run_sync") == 1


def test_metric_counts_sync_helper_definitions_and_legacy_import_surfaces(tmp_path):
    metrics = _load_metrics_module()
    path = tmp_path / "compat.py"
    path.write_text(
        '''
from brain.platform.db.legacy import get_conn, legacy_session_factory

async def run_unit_of_work_task(fn):
    return fn()

def get_cursor():
    yield None
''',
        encoding="utf-8",
    )

    matches = list(metrics.ast_matches(path, "brain/platform/db/example.py", "platform", metrics.async_function_lines(path)))
    categories = [match.category for match in matches]

    assert "raw_sync_cursor" in categories
    assert "sqlalchemy_sessionmaker" in categories
    assert "run_unit_of_work_task" in categories
    assert "psycopg2" not in categories


def test_metric_counts_sync_session_construction_not_type_only_imports(tmp_path):
    metrics = _load_metrics_module()
    type_only_path = tmp_path / "type_only.py"
    type_only_path.write_text(
        '''
from sqlalchemy.orm import Session

VALUE = Session
''',
        encoding="utf-8",
    )
    annotated_path = tmp_path / "annotated.py"
    annotated_path.write_text(
        '''
from sqlalchemy.orm import Session

def read(session: Session):
    return session
''',
        encoding="utf-8",
    )
    runtime_path = tmp_path / "runtime.py"
    runtime_path.write_text(
        '''
from sqlalchemy.orm import Session

def make_session(engine):
    return Session(engine)
''',
        encoding="utf-8",
    )

    type_only_categories = [
        match.category
        for match in metrics.ast_matches(
            type_only_path,
            "tests/type_only.py",
            "tests",
            metrics.async_function_lines(type_only_path),
        )
    ]
    annotated_categories = [
        match.category
        for match in metrics.ast_matches(
            annotated_path,
            "brain/annotated.py",
            "systems",
            metrics.async_function_lines(annotated_path),
        )
    ]
    runtime_categories = [
        match.category
        for match in metrics.ast_matches(
            runtime_path,
            "tests/runtime.py",
            "tests",
            metrics.async_function_lines(runtime_path),
        )
    ]

    assert "sqlalchemy_sync_session" not in type_only_categories
    assert annotated_categories.count("sync_session_annotation") == 1
    assert runtime_categories.count("sqlalchemy_sync_session") == 1


def test_metric_counts_sync_session_method_calls_but_not_async_session_calls(tmp_path):
    metrics = _load_metrics_module()
    path = tmp_path / "sync_calls.py"
    path.write_text(
        '''
def sync_write(session, db, connection):
    session.add(object())
    session.flush()
    rows = db.execute("select 1")
    return connection.scalar("select 1")

def sync_fixture_write(db_session, rollback_cursor, cur):
    db_session.execute("select 1")
    rollback_cursor.execute("select 1")
    cur.fetchall()

async def async_write(session):
    await session.execute("select 1")
    await session.flush()
''',
        encoding="utf-8",
    )

    categories = [
        match.category
        for match in metrics.ast_matches(
            path,
            "brain/sync_calls.py",
            "systems",
            metrics.async_function_lines(path),
        )
    ]

    assert categories.count("sync_session_method_call") == 7


def test_metric_does_not_count_nested_async_db_calls_inside_sync_factory(tmp_path):
    metrics = _load_metrics_module()
    path = tmp_path / "nested_async.py"
    path.write_text(
        '''
def build_override(session):
    async def override_db():
        await session.commit()
        await session.rollback()
        return session
    return override_db
''',
        encoding="utf-8",
    )

    categories = [
        match.category
        for match in metrics.ast_matches(
            path,
            "tests/nested_async.py",
            "tests",
            metrics.async_function_lines(path),
        )
    ]

    assert "sync_session_method_call" not in categories


def test_metric_counts_sync_sqlalchemy_factory_calls_not_import_only(tmp_path):
    metrics = _load_metrics_module()
    import_only_path = tmp_path / "import_only.py"
    import_only_path.write_text(
        '''
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
''',
        encoding="utf-8",
    )
    runtime_path = tmp_path / "runtime_factories.py"
    runtime_path.write_text(
        '''
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

def make_session():
    engine = create_engine("sqlite://")
    return sessionmaker(bind=engine)()
''',
        encoding="utf-8",
    )

    import_only_categories = [
        match.category
        for match in metrics.ast_matches(
            import_only_path,
            "tests/import_only.py",
            "tests",
            metrics.async_function_lines(import_only_path),
        )
    ]
    runtime_categories = [
        match.category
        for match in metrics.ast_matches(
            runtime_path,
            "tests/runtime_factories.py",
            "tests",
            metrics.async_function_lines(runtime_path),
        )
    ]

    assert "sqlalchemy_create_engine" not in import_only_categories
    assert "sqlalchemy_sessionmaker" not in import_only_categories
    assert runtime_categories.count("sqlalchemy_create_engine") == 1
    assert runtime_categories.count("sqlalchemy_sessionmaker") == 1


def test_metric_reports_but_exempts_required_sqlalchemy_run_sync():
    metrics = _load_metrics_module()
    matches = [
        metrics.Match(
            path="brain/platform/db/alembic/env.py",
            line_number=2,
            category="session_run_sync",
            scope="migrations",
            in_async_function=True,
            line="await connection.run_sync(do_run_migrations)",
        ),
        metrics.Match(
            path="tests/example.py",
            line_number=3,
            category="session_run_sync",
            scope="tests",
            in_async_function=True,
            line="await connection.run_sync(inspect_schema)",
        ),
    ]

    summary = metrics.summarize(matches, top=10)

    assert summary["metrics"]["repo_wide_sync_shaped_refs"] == 1
    assert summary["metrics"]["migration_refs"] == 0
    assert summary["metrics"]["required_sqlalchemy_run_sync_refs"] == 1
    payload = metrics.matches_payload(matches)
    assert [item["zero_target_exempt"] for item in payload] == [True, False]

"""Tests for Alembic migration configuration."""

import ast
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_DIR = ROOT / "brain" / "platform" / "db" / "alembic"
VERSIONS_DIR = ALEMBIC_DIR / "versions"

PUBLIC_BASELINE = "0001_public_schema_baseline.py"
LEGACY_STAMP_BRIDGE = "0000_legacy_notification_preferences_bridge.py"
BROAD_DESTRUCTIVE_SQL_PATTERNS = (
    r"\bDROP\s+DATABASE\b",
    r"\bDROP\s+SCHEMA\b",
    r"\bDROP\s+TABLE\b",
    r"\bDROP\s+OWNED\b",
    r"\bTRUNCATE\s+TABLE\b",
    r"\bREASSIGN\s+OWNED\b",
)
REVIEWED_DESTRUCTIVE_MIGRATIONS = {
    "0003_schema_simplification.py",
}


def _migration_files() -> list[Path]:
    return [
        path
        for path in sorted(VERSIONS_DIR.glob("*.py"))
        if path.name != "__init__.py"
    ]


def _material_schema_migration_files() -> list[Path]:
    return [
        path
        for path in _migration_files()
        if path.name != LEGACY_STAMP_BRIDGE
    ]


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    return ""


def _literal_strings(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        values: list[str] = []
        for value in node.values:
            values.extend(_literal_strings(value))
        return values
    if isinstance(node, ast.FormattedValue):
        return _literal_strings(node.value)
    if isinstance(node, ast.Call):
        values: list[str] = []
        for arg in list(node.args) + [keyword.value for keyword in node.keywords]:
            values.extend(_literal_strings(arg))
        return values
    return []


def _literal_sql_strings(node: ast.Call) -> list[str]:
    values: list[str] = []
    for arg in list(node.args) + [keyword.value for keyword in node.keywords]:
        values.extend(_literal_strings(arg))
    return values


def _function_body_calls(module: ast.Module, function_name: str) -> list[ast.Call]:
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            return [child for child in ast.walk(node) if isinstance(child, ast.Call)]
    return []


def _literal_create_table_names(calls: list[ast.Call]) -> list[str]:
    names: list[str] = []
    for call in calls:
        if _call_name(call.func) != "op.create_table" or not call.args:
            continue
        first = call.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.append(first.value)
    return names


def _normalized_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().upper()


def test_literal_sql_strings_unwraps_common_sql_wrappers():
    module = ast.parse(
        """
op.execute(sa.text("DROP TABLE memories"))
op.execute(text("TRUNCATE TABLE memories"))
"""
    )
    calls = [node for node in ast.walk(module) if isinstance(node, ast.Call) and _call_name(node.func) == "op.execute"]

    assert [_literal_sql_strings(call) for call in calls] == [
        ["DROP TABLE memories"],
        ["TRUNCATE TABLE memories"],
    ]


def test_alembic_ini_exists():
    """alembic.ini must be in the project root."""
    root = os.path.join(os.path.dirname(__file__), "..")
    assert os.path.isfile(os.path.join(root, "alembic.ini"))


def test_env_py_widens_alembic_version_column_for_long_revision_ids():
    """Postgres Alembic version tables must handle repo revision ids over 32 chars."""
    long_revision_ids: list[str] = []
    for path in _migration_files():
        module = ast.parse(path.read_text(), filename=str(path))
        for node in module.body:
            if not isinstance(node, ast.Assign):
                continue
            if any(isinstance(target, ast.Name) and target.id == "revision" for target in node.targets):
                revision_id = ast.literal_eval(node.value)
                if len(revision_id) > 32:
                    long_revision_ids.append(revision_id)

    assert long_revision_ids

    env_content = (ALEMBIC_DIR / "env.py").read_text()
    assert "ALEMBIC_VERSION_NUM_MAX_LENGTH = 255" in env_content
    assert "CREATE TABLE IF NOT EXISTS alembic_version" in env_content
    assert "ALTER TABLE alembic_version" in env_content
    assert "version_num TYPE VARCHAR" in env_content


def test_alembic_history_shows_baseline():
    """Alembic must include the canonical baseline revision in its history."""
    root = os.path.join(os.path.dirname(__file__), "..")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "history"],
        capture_output=True, text=True, cwd=root,
    )
    assert result.returncode == 0, f"alembic history failed: {result.stderr}"
    assert "Public schema baseline" in result.stdout


def test_alembic_revision_chain_has_single_head():
    """There must be exactly one head (no branching)."""
    root = os.path.join(os.path.dirname(__file__), "..")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        capture_output=True, text=True, cwd=root,
    )
    assert result.returncode == 0, f"alembic heads failed: {result.stderr}"
    lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
    assert len(lines) == 1, f"Expected 1 head, got {len(lines)}: {lines}"


def test_alembic_revision_headers_match_identifiers():
    """Migration docstring headers must match actual revision metadata."""
    for path in _migration_files():
        module = ast.parse(path.read_text(), filename=str(path))
        docstring = ast.get_docstring(module) or ""
        header = {}
        for line in docstring.splitlines():
            if line.startswith("Revision ID:"):
                header["revision"] = line.split(":", 1)[1].strip()
            elif line.startswith("Revises:"):
                header["down_revision"] = line.split(":", 1)[1].strip()

        assignments = {}
        for node in module.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                    assignments[target.id] = ast.literal_eval(node.value)

        expected_down_revision = assignments["down_revision"]
        if isinstance(expected_down_revision, tuple):
            expected_down_revision = ", ".join(expected_down_revision)
        elif expected_down_revision is None:
            expected_down_revision = ""

        assert header.get("revision") == assignments["revision"], (
            f"{path.name} has stale Revision ID header: {header.get('revision')} "
            f"!= {assignments['revision']}"
        )
        assert header.get("down_revision", "") == expected_down_revision, (
            f"{path.name} has stale Revises header: {header.get('down_revision', '')} "
            f"!= {expected_down_revision}"
        )


def test_public_tree_has_single_schema_baseline():
    """Fresh public releases keep one current-state schema baseline."""
    migration_files = _material_schema_migration_files()
    assert migration_files[0].name == PUBLIC_BASELINE
    assert [path.name for path in migration_files if "baseline" in path.name] == [
        PUBLIC_BASELINE
    ]

    content = (VERSIONS_DIR / PUBLIC_BASELINE).read_text()
    assert "CREATE EXTENSION IF NOT EXISTS vector" in content


def test_only_public_baseline_may_materialize_model_metadata():
    """Only the public baseline may use model-wide metadata DDL."""
    migration_files = _migration_files()

    violations: list[str] = []
    for path in migration_files:
        module = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(module):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"create_all", "drop_all"}:
                continue
            if path.name == PUBLIC_BASELINE:
                continue
            violations.append(f"{path.name}: {_call_name(node.func)}")

    assert violations == [], (
        "New migrations must use explicit Alembic operations, not metadata-wide "
        f"create_all/drop_all calls: {violations}"
    )


def test_post_baseline_model_table_migrations_guard_fresh_baseline_replay():
    """Fresh installs run the baseline first, which materializes current models."""
    from brain.platform.db.base import Base
    import brain.platform.db.models  # noqa: F401

    model_tables = set(Base.metadata.tables)
    violations: list[str] = []
    for path in _material_schema_migration_files():
        if path.name == PUBLIC_BASELINE:
            continue
        module = ast.parse(path.read_text(), filename=str(path))
        created_model_tables = [
            table
            for table in _literal_create_table_names(_function_body_calls(module, "upgrade"))
            if table in model_tables
        ]
        if not created_model_tables:
            continue

        content = path.read_text()
        if "get_table_names" not in content and "has_table" not in content:
            violations.append(f"{path.name}: {created_model_tables}")

    assert violations == [], (
        "Post-baseline migrations that create model-owned tables must guard fresh "
        f"baseline replay because Base.metadata.create_all already created them: {violations}"
    )


def test_future_migrations_do_not_use_broad_destructive_drops():
    """New migrations must not hide table/schema/database drops in Alembic or raw SQL."""
    violations: list[str] = []
    for path in _migration_files():
        if path.name == PUBLIC_BASELINE or path.name in REVIEWED_DESTRUCTIVE_MIGRATIONS:
            continue

        module = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(module):
            if not isinstance(node, ast.Call):
                continue

            call_name = _call_name(node.func)
            if call_name.endswith(".drop_table"):
                violations.append(f"{path.name}: {call_name}")

            if call_name != "op.execute":
                continue

            for sql in _literal_sql_strings(node):
                normalized = _normalized_sql(sql)
                for pattern in BROAD_DESTRUCTIVE_SQL_PATTERNS:
                    if re.search(pattern, normalized):
                        violations.append(f"{path.name}: op.execute({normalized!r})")

    assert violations == [], (
        "New migrations may add/alter/repair schema, but broad destructive drops "
        f"need an explicit review exception: {violations}"
    )


def test_deploy_script_uses_alembic():
    """deploy.sh must call alembic, not the old psql loop."""
    deploy_path = os.path.join(os.path.dirname(__file__), "..", "ops", "deploy.sh")
    content = open(deploy_path).read()
    assert "alembic upgrade head" in content
    assert "for sql_file in" not in content


def test_canonical_tables_are_owned_by_models_not_runtime_ddl():
    from brain.platform.db.base import Base
    from brain.platform.db.models import AgentRun, BrowserSession, SkillInstallation, WorkspaceAppVersion

    assert "agent_runs" in Base.metadata.tables
    assert "agent_run_events" in Base.metadata.tables
    assert "agent_run_artifacts" in Base.metadata.tables
    assert BrowserSession.__table__.c.run_id.foreign_keys
    assert AgentRun.__table__.c.metadata.name == "metadata"
    assert SkillInstallation.__tablename__ == "skill_installations"
    assert WorkspaceAppVersion.__table__.c.renderer_key.default is not None

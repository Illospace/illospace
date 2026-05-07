"""Tests for project-bound vault token availability."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from brain.platform.db.models.environment import TargetRegistry  # noqa: F401 - resolves project binding FK target
from brain.platform.db.models.org import Org, User
from brain.platform.db.models.run import AgentRun  # noqa: F401 - resolves vault grant FK target
from brain.platform.db.models.vault import (
    Secret,
    VaultAccessLog,
    VaultAgentGrant,
    VaultProjectBinding,
)


USER_ID = "aaaaaaaa-0000-4000-8000-000000000001"
OTHER_USER_ID = "aaaaaaaa-0000-4000-8000-000000000002"
ORG_ID = "bbbbbbbb-0000-4000-8000-000000000001"


def _register_sqlite_functions(dbapi_conn, connection_record):
    dbapi_conn.create_function("NOW", 0, lambda: datetime.now(timezone.utc).isoformat())
    dbapi_conn.create_function("gen_random_uuid", 0, lambda: str(uuid.uuid4()))


class _TestUoW:
    def __init__(self, session: Session):
        self.session = session

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.session.rollback()
        else:
            self.session.commit()
        return False


@pytest.fixture(autouse=True)
def vault_key(monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", Fernet.generate_key().decode())


@pytest.fixture
def session():
    engine = create_engine("sqlite://", echo=False)
    event.listen(engine, "connect", _register_sqlite_functions)
    Org.__table__.create(engine, checkfirst=True)
    User.__table__.create(engine, checkfirst=True)
    Secret.__table__.create(engine, checkfirst=True)
    VaultAccessLog.__table__.create(engine, checkfirst=True)
    VaultAgentGrant.__table__.create(engine, checkfirst=True)
    VaultProjectBinding.__table__.create(engine, checkfirst=True)
    s = Session(engine)
    s.add(Org(id=ORG_ID, name="Test Org", slug="test-org"))
    s.add(User(id=USER_ID, org_id=ORG_ID, name="Alex", email="alex@test.com"))
    s.add(User(id=OTHER_USER_ID, org_id=ORG_ID, name="Bob", email="bob@test.com"))
    s.commit()
    yield s
    s.close()


@pytest.fixture
def patch_uow(session, monkeypatch):
    monkeypatch.setattr("brain.systems.vault.UnitOfWork", lambda: _TestUoW(session))


def _secret(session: Session, key_name: str, value: str, *, user_id=USER_ID, access_level="ask") -> Secret:
    from brain.systems.vault import _encrypt

    secret = Secret(
        key_name=key_name,
        encrypted_value=_encrypt(value),
        user_id=user_id,
        agent_access_level=access_level,
    )
    session.add(secret)
    session.flush()
    return secret


def _binding(
    session: Session,
    secret: Secret,
    *,
    project_slug="example-repo",
    env_name="GITHUB_TOKEN",
    user_id=USER_ID,
):
    binding = VaultProjectBinding(
        secret_id=secret.id,
        user_id=user_id,
        org_id=ORG_ID,
        project_slug=project_slug,
        env_name=env_name,
        active=True,
    )
    session.add(binding)
    session.flush()
    return binding


def test_available_secret_bypasses_run_grant(patch_uow, session):
    from brain.systems.vault import authorize_agent_secret_read

    _secret(session, "OPENAI_API_KEY", "sk-test", access_level="available")
    result = authorize_agent_secret_read(
        "OPENAI_API_KEY",
        user_id=USER_ID,
        org_id=ORG_ID,
        run_id=None,
        reason=None,
    )

    assert result["allowed"] is True
    assert result["status"] == "available"
    assert session.query(VaultAgentGrant).count() == 0


def test_manual_secret_cannot_be_auto_read(patch_uow, session):
    from brain.systems.vault import authorize_agent_secret_read

    _secret(session, "STRIPE_WEBHOOK_SECRET", "whsec-test", access_level="manual")
    result = authorize_agent_secret_read(
        "STRIPE_WEBHOOK_SECRET",
        user_id=USER_ID,
        org_id=ORG_ID,
        run_id=42,
        reason="Need webhook secret for this task",
    )

    assert result["allowed"] is False
    assert result["status"] == "denied"
    assert "manual" in result["reason"]
    assert session.query(VaultAgentGrant).count() == 0


def test_ask_secret_bound_to_project_is_allowed_without_prompt(patch_uow, session):
    from brain.systems.vault import authorize_agent_secret_read

    secret = _secret(session, "GITHUB_TOKEN", "ghp-test", access_level="ask")
    _binding(session, secret, project_slug="example-repo")

    result = authorize_agent_secret_read(
        "GITHUB_TOKEN",
        user_id=USER_ID,
        org_id=ORG_ID,
        run_id=None,
        reason=None,
        project_slug="example-repo",
    )

    assert result["allowed"] is True
    assert result["status"] == "project_bound"
    assert result["binding"]["env_name"] == "GITHUB_TOKEN"
    assert session.query(VaultAgentGrant).count() == 0


def test_ask_secret_without_matching_project_creates_pending_grant(patch_uow, session):
    from brain.systems.vault import authorize_agent_secret_read

    secret = _secret(session, "GITHUB_TOKEN", "ghp-test", access_level="ask")
    _binding(session, secret, project_slug="other-project")

    result = authorize_agent_secret_read(
        "GITHUB_TOKEN",
        user_id=USER_ID,
        org_id=ORG_ID,
        run_id=42,
        reason="Need GitHub access for this project task",
        project_slug="example-repo",
    )

    assert result["allowed"] is False
    assert result["status"] == "pending"
    assert result["grant"]["key_name"] == "GITHUB_TOKEN"


def test_resolve_project_bound_env_tokens_returns_only_matching_user_tokens(patch_uow, session):
    from brain.systems.vault import resolve_project_bound_env_tokens

    github = _secret(session, "GITHUB_TOKEN", "ghp-test", access_level="ask")
    _binding(session, github, project_slug="example-repo", env_name="GITHUB_TOKEN")
    manual = _secret(session, "MANUAL_TOKEN", "manual-test", access_level="manual")
    _binding(session, manual, project_slug="example-repo", env_name="MANUAL_TOKEN")
    other_project = _secret(session, "OTHER_TOKEN", "other-test", access_level="ask")
    _binding(session, other_project, project_slug="other-project", env_name="OTHER_TOKEN")
    other_user = _secret(session, "USER2_TOKEN", "user2-test", user_id=OTHER_USER_ID, access_level="ask")
    _binding(session, other_user, project_slug="example-repo", env_name="USER2_TOKEN", user_id=OTHER_USER_ID)

    env = resolve_project_bound_env_tokens(
        user_id=USER_ID,
        org_id=ORG_ID,
        project_slug="example-repo",
    )

    assert env == {"GITHUB_TOKEN": "ghp-test"}
    session.refresh(github)
    assert github.access_count == 1


def test_bind_project_secret_by_key_binds_only_current_users_owned_secret(patch_uow, session):
    from brain.systems.vault import bind_project_secret_by_key

    _secret(session, "OTHER_GITHUB_TOKEN", "ghp-other", user_id=OTHER_USER_ID, access_level="ask")
    owned = _secret(session, "GITHUB_TOKEN", "ghp-test", access_level="ask")

    missing = bind_project_secret_by_key(
        "OTHER_GITHUB_TOKEN",
        user_id=USER_ID,
        org_id=ORG_ID,
        project_slug="Example-Org/Example-Repo",
        env_name="GH_TOKEN",
    )
    binding = bind_project_secret_by_key(
        "GITHUB_TOKEN",
        user_id=USER_ID,
        org_id=ORG_ID,
        project_slug="Example-Org/Example-Repo",
        env_name="GH_TOKEN",
    )

    assert missing is None
    assert binding["secret_id"] == owned.id
    assert binding["project_slug"] == "example-org/example-repo"
    assert binding["env_name"] == "GH_TOKEN"


def test_project_bound_env_tokens_match_project_slug_aliases(patch_uow, session):
    from brain.systems.vault import resolve_project_bound_env_tokens

    github = _secret(session, "GITHUB_TOKEN", "ghp-test", access_level="ask")
    _binding(session, github, project_slug="example-org/example-repo", env_name="GH_TOKEN")

    env = resolve_project_bound_env_tokens(
        user_id=USER_ID,
        org_id=ORG_ID,
        project_slug="example-repo",
        project_slugs=["example-repo", "example-org/example-repo"],
    )

    assert env == {"GH_TOKEN": "ghp-test"}


def test_project_token_context_includes_github_repo_from_project_context_snapshot(monkeypatch):
    from brain.systems.runs.tool_catalog.handlers import common

    monkeypatch.setattr(
        common,
        "_current_run_target_context",
        lambda: {
            "registry": {"id": 7, "slug": "example-repo"},
            "binding": {
                "raw_target_metadata": {
                    "project_context_snapshot": {
                        "resources": [
                            {
                                "kind": "repo",
                                "repo": "example-org/example-repo",
                                "uri": "https://github.com/example-org/example-repo",
                            }
                        ]
                    }
                }
            },
        },
    )

    context = common._current_project_token_context()

    assert context["project_slug"] == "example-repo"
    assert context["project_slugs"] == ["example-repo", "example-org/example-repo"]
    assert context["target_registry_id"] == 7


def test_exec_command_injects_project_bound_env_names_without_returning_values(monkeypatch, tmp_path):
    from brain.systems.runs.tool_catalog.handlers.files import _handle_exec_command

    monkeypatch.setattr(
        "brain.systems.runs.tool_catalog.handlers.files._current_project_bound_env",
        lambda: {"GITHUB_TOKEN": "ghp-secret-value"},
    )
    proc = SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", return_value=proc) as run:
        result = _handle_exec_command("gh auth status", working_dir=str(tmp_path))

    assert result["injected_env"] == ["GITHUB_TOKEN"]
    assert "ghp-secret-value" not in str(result)
    assert run.call_args.kwargs["env"]["GITHUB_TOKEN"] == "ghp-secret-value"

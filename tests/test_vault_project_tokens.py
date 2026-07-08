"""Tests for project-bound vault token availability."""
from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, select

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
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type:
            await self.session.rollback()
        else:
            await self.session.commit()
        return False


@pytest.fixture(autouse=True)
def vault_key(monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", Fernet.generate_key().decode())


@pytest.fixture
async def session(async_sqlite_session_factory):
    s = await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            Secret.__table__,
            VaultAccessLog.__table__,
            VaultAgentGrant.__table__,
            VaultProjectBinding.__table__,
        ],
        connect_listener=_register_sqlite_functions,
    )
    s.add(Org(id=ORG_ID, name="Test Org", slug="test-org"))
    s.add(User(id=USER_ID, org_id=ORG_ID, name="Alex", email="alex@test.com"))
    s.add(User(id=OTHER_USER_ID, org_id=ORG_ID, name="Bob", email="bob@test.com"))
    await s.commit()
    return s


@pytest.fixture
def patch_uow(session, monkeypatch):
    monkeypatch.setattr("brain.systems.vault.UnitOfWork", lambda: _TestUoW(session))


async def _secret(
    session,
    key_name: str,
    value: str,
    *,
    actor_user_id=USER_ID,
    org_id: str | None = None,
    access_level="ask",
    category="general",
) -> Secret:
    from brain.systems.vault import _encrypt

    secret = Secret(
        key_name=key_name,
        encrypted_value=_encrypt(value),
        category=category,
        org_id=org_id or ORG_ID,
        created_by_user_id=actor_user_id,
        updated_by_user_id=actor_user_id,
        agent_access_level=access_level,
    )
    session.add(secret)
    await session.flush()
    return secret


async def _binding(
    session,
    secret: Secret,
    *,
    project_slug="example-repo",
    env_name="GITHUB_TOKEN",
    actor_user_id=USER_ID,
    org_id=ORG_ID,
):
    binding = VaultProjectBinding(
        secret_id=secret.id,
        org_id=org_id,
        created_by_user_id=actor_user_id,
        project_slug=project_slug,
        env_name=env_name,
        active=True,
    )
    session.add(binding)
    await session.flush()
    return binding


class _MockGitHubClient:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, method, url, *, headers=None, params=None, json=None):
        self.requests.append({
            "method": method,
            "url": url,
            "headers": dict(headers or {}),
            "json": json,
            "params": params,
        })
        if url.endswith("/app/installations/456/access_tokens"):
            return httpx.Response(
                201,
                json={
                    "token": "minted-issue-token",
                    "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                },
            )
        if url.endswith("/repos/uwear-ai/uwear-backend/issues"):
            return httpx.Response(
                201,
                json={
                    "number": 321,
                    "html_url": "https://github.com/uwear-ai/uwear-backend/issues/321",
                    "title": json["title"],
                    "state": "open",
                },
            )
        return httpx.Response(404, json={"message": "unexpected mocked URL"})


def _test_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode("ascii")


async def _grant_count(session) -> int:
    return int(await session.scalar(select(func.count()).select_from(VaultAgentGrant)) or 0)


async def test_available_secret_bypasses_run_grant(patch_uow, session):
    from brain.systems.vault import authorize_agent_secret_read

    await _secret(session, "OPENAI_API_KEY", "sk-test", access_level="available")
    result = await authorize_agent_secret_read(
        "OPENAI_API_KEY",
        actor_user_id=USER_ID,
        org_id=ORG_ID,
        run_id=None,
        reason=None,
    )

    assert result["allowed"] is True
    assert result["status"] == "available"
    assert await _grant_count(session) == 0


async def test_manual_secret_cannot_be_auto_read(patch_uow, session):
    from brain.systems.vault import authorize_agent_secret_read

    await _secret(session, "STRIPE_WEBHOOK_SECRET", "whsec-test", access_level="manual")
    result = await authorize_agent_secret_read(
        "STRIPE_WEBHOOK_SECRET",
        actor_user_id=USER_ID,
        org_id=ORG_ID,
        run_id=42,
        reason="Need webhook secret for this task",
    )

    assert result["allowed"] is False
    assert result["status"] == "denied"
    assert "manual" in result["reason"]
    assert await _grant_count(session) == 0


async def test_ask_secret_bound_to_project_is_allowed_without_prompt(patch_uow, session):
    from brain.systems.vault import authorize_agent_secret_read

    secret = await _secret(session, "GITHUB_TOKEN", "ghp-test", access_level="ask")
    await _binding(session, secret, project_slug="example-repo")

    result = await authorize_agent_secret_read(
        "GITHUB_TOKEN",
        actor_user_id=USER_ID,
        org_id=ORG_ID,
        run_id=None,
        reason=None,
        project_slug="example-repo",
    )

    assert result["allowed"] is True
    assert result["status"] == "project_bound"
    assert result["binding"]["env_name"] == "GITHUB_TOKEN"
    assert await _grant_count(session) == 0


async def test_ask_secret_without_matching_project_creates_pending_grant(patch_uow, session):
    from brain.systems.vault import authorize_agent_secret_read

    secret = await _secret(session, "GITHUB_TOKEN", "ghp-test", access_level="ask")
    await _binding(session, secret, project_slug="other-project")

    result = await authorize_agent_secret_read(
        "GITHUB_TOKEN",
        actor_user_id=USER_ID,
        org_id=ORG_ID,
        run_id=42,
        reason="Need GitHub access for this project task",
        project_slug="example-repo",
    )

    assert result["allowed"] is False
    assert result["status"] == "pending"
    assert result["grant"]["key_name"] == "GITHUB_TOKEN"


async def test_secret_reference_does_not_consume_approved_grant(patch_uow, session):
    from brain.systems.vault import authorize_agent_secret_read, authorize_agent_secret_reference

    await _secret(session, "GITHUB_TOKEN", "ghp-test", access_level="ask")
    grant = VaultAgentGrant(
        key_name="GITHUB_TOKEN",
        org_id=ORG_ID,
        requested_by_user_id=USER_ID,
        run_id=42,
        requested_by="agent",
        reason="Need GitHub access for this project task",
        status="approved",
        requested_at=datetime.now(timezone.utc),
        decided_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        max_reads=1,
        read_count=0,
    )
    session.add(grant)
    await session.flush()

    reference = await authorize_agent_secret_reference(
        "GITHUB_TOKEN",
        actor_user_id=USER_ID,
        org_id=ORG_ID,
        run_id=42,
        reason="Need GitHub access for this project task",
    )
    await session.refresh(grant)

    assert reference["allowed"] is True
    assert reference["status"] == "approved"
    assert grant.read_count == 0
    assert grant.status == "approved"

    read = await authorize_agent_secret_read(
        "GITHUB_TOKEN",
        actor_user_id=USER_ID,
        org_id=ORG_ID,
        run_id=42,
        reason="Need GitHub access for this project task",
    )
    await session.refresh(grant)

    assert read["allowed"] is True
    assert grant.read_count == 1
    assert grant.status == "used"


async def test_resolve_project_bound_env_tokens_returns_org_bound_tokens_for_members(patch_uow, session):
    from brain.systems.vault import resolve_project_bound_env_tokens

    github = await _secret(session, "GITHUB_TOKEN", "ghp-test", org_id=ORG_ID, access_level="ask")
    await _binding(session, github, project_slug="example-repo", env_name="GITHUB_TOKEN")
    manual = await _secret(session, "MANUAL_TOKEN", "manual-test", org_id=ORG_ID, access_level="manual")
    await _binding(session, manual, project_slug="example-repo", env_name="MANUAL_TOKEN")
    other_project = await _secret(session, "OTHER_TOKEN", "other-test", org_id=ORG_ID, access_level="ask")
    await _binding(session, other_project, project_slug="other-project", env_name="OTHER_TOKEN")

    env = await resolve_project_bound_env_tokens(
        actor_user_id=OTHER_USER_ID,
        org_id=ORG_ID,
        project_slug="example-repo",
    )

    assert env == {"GITHUB_TOKEN": "ghp-test"}
    await session.refresh(github)
    assert github.access_count == 1


async def test_github_app_project_binding_mints_before_manual_skip(patch_uow, session, monkeypatch):
    from brain.systems.vault import async_resolve_project_bound_env_tokens

    github_app = await _secret(
        session,
        "GITHUB_APP__ILLO",
        "github-app-blob",
        access_level="manual",
        category="github_app",
    )
    await _binding(
        session,
        github_app,
        project_slug="uwear-ai/uwear-backend",
        env_name="GITHUB_TOKEN",
    )
    normal = await _secret(session, "NORMAL_TOKEN", "plain-token", access_level="ask")
    await _binding(
        session,
        normal,
        project_slug="uwear-ai/uwear-backend",
        env_name="NORMAL_TOKEN",
    )
    manual = await _secret(session, "MANUAL_TOKEN", "manual-token", access_level="manual")
    await _binding(
        session,
        manual,
        project_slug="uwear-ai/uwear-backend",
        env_name="MANUAL_TOKEN",
    )
    transaction_state = {"open": False}
    mint_calls = []

    class TrackingUoW(_TestUoW):
        async def __aenter__(self):
            transaction_state["open"] = True
            return await super().__aenter__()

        async def __aexit__(self, exc_type, exc, tb):
            try:
                return await super().__aexit__(exc_type, exc, tb)
            finally:
                transaction_state["open"] = False

    async def async_mint_installation_token(decrypted_blob, *, repositories, permissions):
        mint_calls.append({
            "decrypted_blob": decrypted_blob,
            "repositories": repositories,
            "permissions": permissions,
            "transaction_open": transaction_state["open"],
        })
        return "minted-installation-token"

    monkeypatch.setattr("brain.systems.vault.UnitOfWork", lambda: TrackingUoW(session))
    monkeypatch.setattr(
        "brain.systems.vault.github_app_mint.async_mint_installation_token",
        async_mint_installation_token,
    )

    env = await async_resolve_project_bound_env_tokens(
        actor_user_id=OTHER_USER_ID,
        org_id=ORG_ID,
        project_slug="uwear-ai/uwear-backend",
    )

    assert env == {
        "GITHUB_TOKEN": "minted-installation-token",
        "NORMAL_TOKEN": "plain-token",
    }
    assert mint_calls == [
        {
            "decrypted_blob": "github-app-blob",
            "repositories": ["uwear-backend"],
            "permissions": {"issues": "write", "contents": "read", "pull_requests": "read"},
            "transaction_open": False,
        }
    ]
    await session.refresh(github_app)
    await session.refresh(normal)
    await session.refresh(manual)
    assert github_app.access_count == 1
    assert normal.access_count == 1
    assert manual.access_count == 0
    logs = (await session.scalars(select(VaultAccessLog).order_by(VaultAccessLog.key_name))).all()
    assert {
        log.key_name: log.accessed_by
        for log in logs
        if log.action == "read"
    } == {
        "GITHUB_APP__ILLO": "agent",
        "NORMAL_TOKEN": "agent",
    }


async def test_resolve_project_bound_env_tokens_can_filter_to_github_app_only(patch_uow, session, monkeypatch):
    from brain.systems.vault import async_resolve_project_bound_env_tokens

    github_app = await _secret(
        session,
        "GITHUB_APP__ILLO",
        "github-app-blob",
        access_level="manual",
        category="github_app",
    )
    await _binding(
        session,
        github_app,
        project_slug="uwear-ai/uwear-backend",
        env_name="GITHUB_TOKEN",
    )
    personal = await _secret(
        session,
        "AXEL_LEGACY",
        "personal-token",
        access_level="ask",
        category="github",
    )
    await _binding(
        session,
        personal,
        project_slug="uwear-ai/uwear-backend",
        env_name="GH_TOKEN",
    )

    async def async_mint_installation_token(decrypted_blob, *, repositories, permissions):
        assert decrypted_blob == "github-app-blob"
        assert repositories == ["uwear-backend"]
        assert permissions == {"issues": "write", "contents": "read", "pull_requests": "read"}
        return "minted-installation-token"

    monkeypatch.setattr(
        "brain.systems.vault.github_app_mint.async_mint_installation_token",
        async_mint_installation_token,
    )

    app_only = await async_resolve_project_bound_env_tokens(
        actor_user_id=OTHER_USER_ID,
        org_id=ORG_ID,
        project_slug="uwear-ai/uwear-backend",
        github_app_only=True,
    )
    all_bound = await async_resolve_project_bound_env_tokens(
        actor_user_id=OTHER_USER_ID,
        org_id=ORG_ID,
        project_slug="uwear-ai/uwear-backend",
        github_app_only=False,
    )

    assert app_only == {"GITHUB_TOKEN": "minted-installation-token"}
    assert all_bound == {
        "GITHUB_TOKEN": "minted-installation-token",
        "GH_TOKEN": "personal-token",
    }


async def test_create_github_issue_uses_minted_github_app_project_binding(
    patch_uow,
    session,
    monkeypatch,
):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.github import _handle_create_github_issue
    from brain.systems.vault import github_app_mint

    github_app_mint._TOKEN_CACHE.clear()
    github_app_mint._MINT_LOCKS.clear()
    app_blob = json.dumps({
        "app_id": "123",
        "client_id": "Iv23.client",
        "installation_id": "456",
        "private_key_pem": _test_private_key_pem(),
    })
    github_app = await _secret(
        session,
        "GITHUB_APP__ILLO",
        app_blob,
        access_level="manual",
        category="github_app",
    )
    await _binding(
        session,
        github_app,
        project_slug="uwear-ai/uwear-backend",
        env_name="GITHUB_TOKEN",
    )
    client = _MockGitHubClient()
    monkeypatch.setattr(
        "brain.systems.vault.github_app_mint.async_http_client",
        lambda **_kwargs: client,
    )
    monkeypatch.setattr(
        "brain.systems.cortex.project_context.github.async_http_client",
        lambda **_kwargs: client,
    )

    with bind_agent_context({"user_id": USER_ID, "org_id": ORG_ID}):
        result = await _handle_create_github_issue(
            repo="uwear-ai/uwear-backend",
            title="Backend incident",
            body="Details",
            labels=["bug"],
        )

    payload = json.loads(result)
    assert payload["repo"] == "uwear-ai/uwear-backend"
    assert payload["issue"]["number"] == 321
    assert payload["token_source"] == "project_binding:GITHUB_TOKEN"
    assert "token_key_name" in payload
    assert payload.get("no_write_token") is not True
    access_request = next(request for request in client.requests if request["url"].endswith("/access_tokens"))
    issue_request = next(request for request in client.requests if request["url"].endswith("/issues"))
    assert access_request["json"]["repositories"] == ["uwear-backend"]
    assert access_request["json"]["permissions"] == {"issues": "write", "contents": "read", "pull_requests": "read"}
    assert issue_request["headers"]["Authorization"] == "Bearer minted-issue-token"


async def test_create_github_issue_does_not_use_personal_project_binding(
    patch_uow,
    session,
    monkeypatch,
):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.github import _handle_create_github_issue

    personal = await _secret(
        session,
        "AXEL_LEGACY",
        "personal-token",
        access_level="ask",
        category="github",
    )
    await _binding(
        session,
        personal,
        project_slug="uwear-ai/uwear-backend",
        env_name="GH_TOKEN",
    )

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("personal token must not be used for GitHub writes")

    monkeypatch.setattr(
        "brain.systems.runs.tool_catalog.handlers.github.async_create_repo_issue",
        fail_if_called,
    )

    with bind_agent_context({"user_id": USER_ID, "org_id": ORG_ID}):
        result = await _handle_create_github_issue(
            repo="uwear-ai/uwear-backend",
            title="Backend incident",
        )

    payload = json.loads(result)
    assert payload["repo"] == "uwear-ai/uwear-backend"
    assert payload["status_code"] == 401
    assert payload["no_write_token"] is True
    assert "No GitHub App identity is connected for uwear-ai/uwear-backend" in payload["error"]


async def test_create_github_issue_does_not_use_canonical_inventory_personal_token(
    patch_uow,
    session,
    monkeypatch,
):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.github import _handle_create_github_issue

    await _secret(
        session,
        "GITHUB_TOKEN",
        "personal-inventory-token",
        access_level="available",
        category="github",
    )

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("inventory token must not be used for GitHub writes")

    monkeypatch.setattr(
        "brain.systems.runs.tool_catalog.handlers.github.async_create_repo_issue",
        fail_if_called,
    )

    with bind_agent_context({"user_id": USER_ID, "org_id": ORG_ID}):
        result = await _handle_create_github_issue(
            repo="uwear-ai/uwear-backend",
            title="Backend incident",
        )

    payload = json.loads(result)
    assert payload["repo"] == "uwear-ai/uwear-backend"
    assert payload["status_code"] == 401
    assert payload["no_write_token"] is True
    assert "No GitHub App identity is connected for uwear-ai/uwear-backend" in payload["error"]


async def test_create_github_issue_honors_explicit_token_secret_key_for_write(
    patch_uow,
    session,
    monkeypatch,
):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.github import _handle_create_github_issue

    await _secret(
        session,
        "CUSTOM_WRITE_TOKEN",
        "explicit-write-token",
        access_level="available",
        category="github",
    )
    issued_with = {}

    async def fake_create_repo_issue(repo_slug, *, title, body, labels, assignees, token):
        issued_with["repo_slug"] = repo_slug
        issued_with["token"] = token
        return {
            "repo": repo_slug,
            "issue": {
                "number": 123,
                "html_url": "https://github.com/uwear-ai/uwear-backend/issues/123",
                "title": title,
                "state": "open",
            },
        }

    monkeypatch.setattr(
        "brain.systems.runs.tool_catalog.handlers.github.async_create_repo_issue",
        fake_create_repo_issue,
    )

    with bind_agent_context({"user_id": USER_ID, "org_id": ORG_ID}):
        result = await _handle_create_github_issue(
            repo="uwear-ai/uwear-backend",
            title="Backend incident",
            token_secret_key="CUSTOM_WRITE_TOKEN",
        )

    payload = json.loads(result)
    assert payload["repo"] == "uwear-ai/uwear-backend"
    assert payload["issue"]["number"] == 123
    assert payload["token_secret_key_used"] is True
    assert payload["token_source"] == "explicit"
    assert payload["token_key_name"] == "CUSTOM_WRITE_TOKEN"
    assert payload.get("no_write_token") is not True
    assert issued_with == {
        "repo_slug": "uwear-ai/uwear-backend",
        "token": "explicit-write-token",
    }


async def test_read_token_candidates_still_use_personal_binding_and_inventory(
    patch_uow,
    session,
):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.github import _github_token_candidates

    personal = await _secret(
        session,
        "AXEL_LEGACY",
        "personal-bound-token",
        access_level="ask",
        category="github",
    )
    await _binding(
        session,
        personal,
        project_slug="uwear-ai/uwear-backend",
        env_name="GH_TOKEN",
    )
    await _secret(
        session,
        "GITHUB_TOKEN",
        "personal-inventory-token",
        access_level="available",
        category="github",
    )

    with bind_agent_context({"user_id": USER_ID, "org_id": ORG_ID}):
        candidates = await _github_token_candidates(
            repo_slug="uwear-ai/uwear-backend",
            token_secret_key=None,
            for_write=False,
        )

    assert {
        (candidate["source"], candidate["token"])
        for candidate in candidates
    } == {
        ("project_binding:GH_TOKEN", "personal-bound-token"),
        ("vault_inventory", "personal-inventory-token"),
    }


async def test_project_bindings_require_org_scope(patch_uow, session):
    from brain.systems.vault import resolve_project_bound_env_tokens

    secret = await _secret(session, "ORG_TOKEN", "org-test", access_level="ask")
    await _binding(session, secret, project_slug="example-repo", env_name="ORG_TOKEN")

    with pytest.raises(ValueError, match="org_id is required"):
        await resolve_project_bound_env_tokens(
            actor_user_id=OTHER_USER_ID,
            org_id=None,
            project_slug="example-repo",
        )


async def test_bind_project_secret_by_key_binds_org_secret_for_any_member(patch_uow, session):
    from brain.systems.vault import bind_project_secret_by_key

    org_owned = await _secret(session, "GITHUB_TOKEN", "ghp-test", org_id=ORG_ID, access_level="ask")

    missing = await bind_project_secret_by_key(
        "OTHER_GITHUB_TOKEN",
        actor_user_id=USER_ID,
        org_id=ORG_ID,
        project_slug="Example-Org/Example-Repo",
        env_name="GH_TOKEN",
    )
    binding = await bind_project_secret_by_key(
        "GITHUB_TOKEN",
        actor_user_id=OTHER_USER_ID,
        org_id=ORG_ID,
        project_slug="Example-Org/Example-Repo",
        env_name="GH_TOKEN",
    )

    assert missing is None
    assert binding["secret_id"] == org_owned.id
    assert binding["created_by_user_id"] == OTHER_USER_ID
    assert binding["org_id"] == ORG_ID
    assert binding["project_slug"] == "example-org/example-repo"
    assert binding["env_name"] == "GH_TOKEN"


async def test_get_secret_reads_org_secret_for_member(patch_uow, session):
    from brain.systems.vault import get_secret

    await _secret(session, "ORG_GITHUB_TOKEN", "ghp-org", actor_user_id=USER_ID, org_id=ORG_ID)

    value = await get_secret("ORG_GITHUB_TOKEN", actor_user_id=OTHER_USER_ID, org_id=ORG_ID)

    assert value == "ghp-org"


async def test_list_secrets_includes_org_secret_for_member(patch_uow, session):
    from brain.systems.vault import list_secrets

    org_secret = await _secret(session, "ORG_GITHUB_TOKEN", "ghp-org", actor_user_id=USER_ID, org_id=ORG_ID)

    rows = await list_secrets(OTHER_USER_ID, org_id=ORG_ID)

    assert [
        {
            "id": row["id"],
            "key_name": row["key_name"],
            "org_id": row["org_id"],
            "created_by_user_id": row["created_by_user_id"],
        }
        for row in rows
    ] == [
        {
            "id": org_secret.id,
            "key_name": "ORG_GITHUB_TOKEN",
            "org_id": ORG_ID,
            "created_by_user_id": USER_ID,
        }
    ]


async def test_project_bound_env_tokens_match_project_slug_aliases(patch_uow, session):
    from brain.systems.vault import resolve_project_bound_env_tokens

    github = await _secret(session, "GITHUB_TOKEN", "ghp-test", access_level="ask")
    await _binding(session, github, project_slug="example-org/example-repo", env_name="GH_TOKEN")

    env = await resolve_project_bound_env_tokens(
        actor_user_id=USER_ID,
        org_id=ORG_ID,
        project_slug="example-repo",
        project_slugs=["example-repo", "example-org/example-repo"],
    )

    assert env == {"GH_TOKEN": "ghp-test"}


def test_project_token_context_includes_github_repo_from_project_context_snapshot(monkeypatch):
    from brain.systems.runs.tool_catalog.handlers import common
    from brain.systems.runs import project_execution_env

    monkeypatch.setattr(
        project_execution_env,
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


async def test_project_bound_env_uses_materialized_project_context_without_run_binding(
    patch_uow,
    session,
    tmp_path,
):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs import project_execution_env
    from brain.systems.vault import async_resolve_project_bound_env_tokens

    github = await _secret(session, "GITHUB_TOKEN", "ghp-test", access_level="ask")
    await _binding(session, github, project_slug="example-org/example-repo", env_name="GH_TOKEN")

    workspace_root = tmp_path / "example-repo"
    run = SimpleNamespace(
        target_ref={"kind": "cortex_idea"},
        workspace_ref={
            "workspace_root": str(workspace_root),
            "project_context_snapshot": {
                "resources": [
                    {
                        "kind": "repo",
                        "repo": "example-org/example-repo",
                        "uri": "https://github.com/example-org/example-repo",
                    }
                ]
            },
        },
    )

    with bind_agent_context({
        "run": run,
        "run_id": 123,
        "user_id": USER_ID,
        "org_id": ORG_ID,
        "workspace_root": str(workspace_root),
    }):
        project_context = project_execution_env._current_project_token_context()
        env = await async_resolve_project_bound_env_tokens(
            actor_user_id=USER_ID,
            org_id=ORG_ID,
            project_slug=project_context.get("project_slug"),
            project_slugs=project_context.get("project_slugs"),
            target_registry_id=project_context.get("target_registry_id"),
        )

    assert env == {"GH_TOKEN": "ghp-test"}


async def test_async_project_bound_env_resolves_from_materialized_project_context(
    patch_uow,
    session,
    tmp_path,
):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs import project_execution_env

    github = await _secret(session, "GITHUB_TOKEN", "ghp-test", access_level="ask")
    await _binding(session, github, project_slug="example-org/example-repo", env_name="GH_TOKEN")

    workspace_root = tmp_path / "example-repo"
    run = SimpleNamespace(
        target_ref={"kind": "cortex_idea"},
        workspace_ref={
            "workspace_root": str(workspace_root),
            "project_context_snapshot": {
                "resources": [
                    {
                        "kind": "repo",
                        "repo": "example-org/example-repo",
                        "uri": "https://github.com/example-org/example-repo",
                    }
                ]
            },
        },
    )

    with bind_agent_context({
        "run": run,
        "run_id": 123,
        "user_id": USER_ID,
        "org_id": ORG_ID,
        "workspace_root": str(workspace_root),
    }):
        env = await project_execution_env.async_current_project_bound_env()

    assert env == {"GH_TOKEN": "ghp-test"}


def test_exec_command_injects_project_bound_env_names_without_returning_values(monkeypatch, tmp_path):
    from brain.systems.runs.tool_catalog.handlers.files import _handle_exec_command

    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    monkeypatch.setattr(
        "brain.systems.runs.project_execution_env.current_project_bound_env",
        lambda: {"GITHUB_TOKEN": "ghp-secret-value"},
    )
    proc = SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", return_value=proc) as run:
        result = _handle_exec_command("gh auth status", working_dir=str(tmp_path))

    assert result["injected_env"] == ["GITHUB_TOKEN"]
    assert result["git_auth_configured"] == ["github.com"]
    assert "ghp-secret-value" not in str(result)
    run_env = run.call_args.kwargs["env"]
    encoded = base64.b64encode(b"x-access-token:ghp-secret-value").decode("ascii")
    assert run_env["GITHUB_TOKEN"] == "ghp-secret-value"
    assert run_env["GIT_TERMINAL_PROMPT"] == "0"
    assert run_env["GCM_INTERACTIVE"] == "never"
    assert run_env["GIT_CONFIG_COUNT"] == "1"
    assert run_env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert run_env["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {encoded}"


def test_exec_command_attributes_project_bound_git_to_requesting_user(monkeypatch, tmp_path):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.files import _handle_exec_command

    monkeypatch.setattr(
        "brain.systems.runs.project_execution_env.current_project_bound_env",
        lambda: {"GITHUB_TOKEN": "ghp-secret-value"},
    )
    proc = SimpleNamespace(returncode=0, stdout="", stderr="")
    actor = {
        "id": USER_ID,
        "org_id": ORG_ID,
        "name": "Alex Example",
        "email": "alex@example.com",
    }

    with bind_agent_context({
        "user_id": USER_ID,
        "org_id": ORG_ID,
        "execution_metadata": {"illo_trigger": {"actor": actor}},
    }):
        with patch("subprocess.run", return_value=proc) as run:
            result = _handle_exec_command("git commit -m test", working_dir=str(tmp_path))

    run_env = run.call_args.kwargs["env"]
    assert result["git_auth_configured"] == ["github.com"]
    assert run_env["GIT_AUTHOR_NAME"] == "Alex Example"
    assert run_env["GIT_AUTHOR_EMAIL"] == "alex@example.com"
    assert run_env["GIT_COMMITTER_NAME"] == "Alex Example"
    assert run_env["GIT_COMMITTER_EMAIL"] == "alex@example.com"


def test_exec_command_redacts_project_bound_git_auth_from_output(monkeypatch, tmp_path):
    from brain.systems.runs.tool_catalog.handlers.files import _handle_exec_command

    token = "ghp-secret-value"
    encoded = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    auth_header = f"AUTHORIZATION: basic {encoded}"
    monkeypatch.setattr(
        "brain.systems.runs.project_execution_env.current_project_bound_env",
        lambda: {"GH_TOKEN": token, "STRIPE_API_KEY": "sk-live-secret"},
    )
    proc = SimpleNamespace(
        returncode=0,
        stdout=f"token={token}\nheader={auth_header}\nstripe=sk-live-secret\n",
        stderr=f"encoded={encoded}\n",
    )

    with patch("subprocess.run", return_value=proc):
        result = _handle_exec_command("git push origin HEAD:test", working_dir=str(tmp_path))

    assert token not in result["stdout"]
    assert encoded not in result["stdout"]
    assert "sk-live-secret" not in result["stdout"]
    assert encoded not in result["stderr"]
    assert result["stdout"].count("[secret redacted]") == 3
    assert result["stderr"].count("[secret redacted]") == 1


def test_exec_command_uses_resolved_secret_env_without_returning_values(tmp_path):
    from brain.systems.runs.tool_catalog.handlers.files import _handle_exec_command

    token = "ghp-secret-value"
    encoded = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    proc = SimpleNamespace(
        returncode=0,
        stdout=f"token={token}\nencoded={encoded}\n",
        stderr="",
    )

    with patch("subprocess.run", return_value=proc) as run:
        result = _handle_exec_command(
            "gh auth status",
            working_dir=str(tmp_path),
            _resolved_secret_env={"GH_TOKEN": token},
        )

    run_env = run.call_args.kwargs["env"]
    assert run_env["GH_TOKEN"] == token
    assert result["injected_env"] == ["GH_TOKEN"]
    assert result["git_auth_configured"] == ["github.com"]
    assert token not in str(result)
    assert encoded not in str(result)
    assert result["stdout"].count("[secret redacted]") == 2


def test_exec_command_uses_workspace_tool_runtime_env_and_redacts_file_secrets(tmp_path):
    from brain.systems.runs.tool_catalog.handlers.files import _handle_exec_command

    token = "codex-access-token-secret"
    proc = SimpleNamespace(
        returncode=0,
        stdout=f"token={token}\n",
        stderr="",
    )

    with patch("subprocess.run", return_value=proc) as run:
        result = _handle_exec_command(
            "codex --version",
            working_dir=str(tmp_path),
            _resolved_workspace_tool_env={"CODEX_HOME": str(tmp_path / "codex-home")},
            _resolved_workspace_tool_sensitive_values=[token],
        )

    run_env = run.call_args.kwargs["env"]
    assert run_env["CODEX_HOME"] == str(tmp_path / "codex-home")
    assert result["injected_env"] == ["CODEX_HOME"]
    assert token not in str(result)
    assert result["stdout"].count("[secret redacted]") == 1


def test_exec_command_rejects_non_string_resolved_secret_env_values(tmp_path):
    from brain.systems.runs.tool_catalog.handlers.files import _handle_exec_command

    with pytest.raises(ValueError, match="resolved string value"):
        _handle_exec_command(
            "gh auth status",
            working_dir=str(tmp_path),
            _resolved_secret_env={"GH_TOKEN": {"vault_key": "GITHUB_TOKEN"}},
        )


def test_run_script_uses_project_bound_env_and_redacts_values(monkeypatch, tmp_path):
    from brain.systems.runs.tool_catalog.handlers.files import _handle_run_script

    monkeypatch.setattr(
        "brain.systems.runs.project_execution_env.current_project_bound_env",
        lambda: {"GH_TOKEN": "ghp-secret-value", "STRIPE_API_KEY": "sk-live-secret"},
    )
    proc = SimpleNamespace(
        returncode=0,
        stdout="github=ghp-secret-value\nstripe=sk-live-secret\n",
        stderr="",
    )

    with patch("subprocess.run", return_value=proc) as run:
        result = _handle_run_script("print('ok')", _workspace=str(tmp_path))

    run_env = run.call_args.kwargs["env"]
    assert run_env["GH_TOKEN"] == "ghp-secret-value"
    assert run_env["STRIPE_API_KEY"] == "sk-live-secret"
    assert result["injected_env"] == ["GH_TOKEN", "STRIPE_API_KEY"]
    assert result["git_auth_configured"] == ["github.com"]
    assert "ghp-secret-value" not in result["stdout"]
    assert "sk-live-secret" not in result["stdout"]
    assert result["stdout"].count("[secret redacted]") == 2


def test_run_script_keeps_temp_script_outside_workspace(tmp_path):
    from brain.systems.runs.tool_catalog.handlers.files import _handle_run_script

    proc = SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", return_value=proc) as run:
        result = _handle_run_script("print('ok')", _workspace=str(tmp_path))

    script_path = run.call_args.args[0][1]
    assert result["exit_code"] == 0
    assert run.call_args.kwargs["cwd"] == str(tmp_path)
    assert not str(script_path).startswith(str(tmp_path))


def test_exec_commands_keep_project_bound_env_isolated(monkeypatch, tmp_path):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs import project_execution_env
    from brain.systems.runs.tool_catalog.handlers import files

    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    captured_envs: dict[str, dict[str, str]] = {}

    def project_env():
        workspace_root = str(getattr(files._agent_context, "workspace_root", ""))
        project_name = workspace_root.rsplit("/", 1)[-1]
        return {"GH_TOKEN": f"token-for-{project_name}"}

    def fake_run(*args, **kwargs):
        del args
        captured_envs[str(kwargs["cwd"])] = dict(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def run_for_project(project_root):
        with bind_agent_context({
            "workspace_root": str(project_root),
            "user_id": USER_ID,
            "org_id": ORG_ID,
        }):
            return files._handle_exec_command("git status", working_dir=str(project_root))

    monkeypatch.setattr(project_execution_env, "current_project_bound_env", project_env)
    with patch("subprocess.run", side_effect=fake_run):
        results = [run_for_project(project_a), run_for_project(project_b)]

    assert captured_envs[str(project_a)]["GH_TOKEN"] == "token-for-project-a"
    assert captured_envs[str(project_b)]["GH_TOKEN"] == "token-for-project-b"
    assert captured_envs[str(project_a)]["GIT_CONFIG_VALUE_0"] != captured_envs[str(project_b)]["GIT_CONFIG_VALUE_0"]
    assert all(result["injected_env"] == ["GH_TOKEN"] for result in results)
    assert "token-for-project-a" not in str(results)
    assert "token-for-project-b" not in str(results)


def test_test_runner_uses_project_bound_env_and_redacts_values(monkeypatch, tmp_path):
    from brain.systems.runs import project_execution_env
    from brain.systems.tools.handlers import handle_test_runner

    token = "ghp-secret-value"
    encoded = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    monkeypatch.setattr(
        project_execution_env,
        "current_project_bound_env",
        lambda: {"GH_TOKEN": token, "SERVICE_TOKEN": "service-secret"},
    )
    proc = SimpleNamespace(
        returncode=1,
        stdout=(
            f"FAILED tests/test_example.py::test_token - {token}\n"
            f"=========================== FAILURES ===========================\n"
            f"service-secret\n"
        ),
        stderr=f"encoded={encoded}\n",
    )

    with patch("subprocess.run", return_value=proc) as run:
        result = handle_test_runner("tests/test_example.py", workspace_root=str(tmp_path))

    run_env = run.call_args.kwargs["env"]
    assert run_env["GH_TOKEN"] == token
    assert run_env["SERVICE_TOKEN"] == "service-secret"
    assert result["injected_env"] == ["GH_TOKEN", "SERVICE_TOKEN"]
    assert result["git_auth_configured"] == ["github.com"]
    assert token not in str(result)
    assert encoded not in str(result)
    assert "service-secret" not in str(result)

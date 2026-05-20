from sqlalchemy import inspect
from brain.platform.db.models.org import Org, OrgApiKey, User, UserCodexConnection


def test_org_columns():
    cols = {c.name for c in inspect(Org).columns}
    assert cols >= {"id", "name", "slug", "created_at"}


def test_org_tablename():
    assert Org.__tablename__ == "orgs"


def test_user_columns():
    cols = {c.name for c in inspect(User).columns}
    assert cols >= {
        "id", "org_id", "name", "email", "role", "password_hash",
        "approved", "created_at", "color", "vault_salt",
        "attribution_enabled",
    }


def test_user_tablename():
    assert User.__tablename__ == "users"


def test_org_api_key_columns():
    cols = {c.name for c in inspect(OrgApiKey).columns}
    assert cols >= {
        "id", "org_id", "provider", "encrypted_key",
        "label", "total_tokens_used", "estimated_cost_usd",
    }


def test_user_codex_connection_columns():
    cols = {c.name for c in inspect(UserCodexConnection).columns}
    assert cols >= {
        "id", "user_id", "encrypted_credential",
        "is_active", "label", "total_tokens_used", "estimated_cost_usd",
    }
